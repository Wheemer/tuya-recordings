"""One-shot synchronous adapter for APK-native IMM SD-card playback."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import date
from functools import partial
from typing import Any

from .commands import CAMERA_COMMANDS, ON_DEMAND_OFFER_RETRIES
from .native_frames import NATIVE_PLAYBACK_EVENT_VERSION, NativePlaybackEventSink
from .native_media_crypto import (
    NativeMediaCryptoError,
    NativeMediaSecretStore,
    decrypt_playback_frame,
)
from .native_relay_media import playback_stream_frame
from .native_relay_playback import (
    NativePlaybackCompleted, NativeRelayPlaybackSession, WAKE_SLOT_TIMEOUT_SECONDS,
)
from .native_session import NativeCameraSessionConfig, NativePlaybackRequest
from .native_thumbnail import NativeThumbnailReady


class NativeRelayRuntimeError(RuntimeError):
    """The bounded APK-native playback transaction failed."""


_PLAYBACK_IDLE_SECONDS = 8.0
_MAX_CONSECUTIVE_EMPTY_MEDIA_FRAMES = 128
_POOL_CLOSE_TIMEOUT_SECONDS = 2.0
_PLAYBACK_STOP_TIMEOUT_SECONDS = 5.0
_THUMBNAIL_DEADLINE_GRACE_SECONDS = 1.0
_STOP_WORKER = object()
_LOGGER = logging.getLogger(__name__)


async def _close_worker_transport(worker, playback, *, abort=False):
    """Retain failed transports and lock out further camera commands."""
    owner = getattr(worker, "_cleanup_owner", worker)
    try:
        await (playback.abort() if abort else playback.close())
    except BaseException:
        worker._unfinished_transport = playback
        CAMERA_COMMANDS.quarantine(owner)
        raise
    else:
        if getattr(worker, "_unfinished_transport", None) is playback:
            worker._unfinished_transport = None
            CAMERA_COMMANDS.clear_quarantine(owner)


async def _thread_job(function, *args):
    """Keep cancellation from abandoning a still-running bounded pipe write."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(Exception):
            await task
        raise


class _WorkerQueue(queue.Queue):
    """Thread-safe submissions that wake the transport loop without blocking it."""

    def __init__(self):
        super().__init__()
        self._loop = None
        self._available = None

    def put(self, item, block=True, timeout=None):
        super().put(item, block, timeout)
        if self._loop is not None and self._available is not None:
            with suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._available.set)

    async def get_async(self, timeout=None):
        if self._available is None:
            self._loop = asyncio.get_running_loop()
            self._available = asyncio.Event()
        try:
            async with asyncio.timeout(timeout):
                while True:
                    self._available.clear()
                    try:
                        return self.get_nowait()
                    except queue.Empty:
                        await self._available.wait()
        except TimeoutError as err:
            raise queue.Empty from err


class _CancellationGroup:
    """Expose several cancellation sources through the Event-style API."""

    def __init__(self, *events: Any) -> None:
        self._events = events

    def is_set(self) -> bool:
        return any(event is not None and event.is_set() for event in self._events)


def _offer_retry_options(enabled: bool) -> dict[str, bool]:
    return {"retry_offer": True} if enabled else {}


@dataclass(slots=True)
class _PlaybackWork:
    request: NativePlaybackRequest
    event_handler: Callable[[dict[str, Any]], None]
    cancel_event: Any
    retry_offer: bool = field(default_factory=ON_DEMAND_OFFER_RETRIES.get)
    done: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None


@dataclass(slots=True)
class _InteractivePlaybackWork:
    request: NativePlaybackRequest
    event_handler: Callable[[dict[str, Any]], None]
    commands: queue.Queue[tuple[str, NativePlaybackRequest | None]]
    cancel_event: Any
    retry_offer: bool = field(default_factory=ON_DEMAND_OFFER_RETRIES.get)
    done: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None


@dataclass(slots=True)
class _ThumbnailWork:
    start: int
    end: int
    cancel_event: Any
    done: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None
    image: bytes | None = None


@dataclass(slots=True)
class _CatalogWork:
    days: list[date]
    cancel_event: Any
    events: bool = False
    months: bool = False
    allow_wake: bool = False
    retry_offer: bool = field(default_factory=ON_DEMAND_OFFER_RETRIES.get)
    done: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None
    results: list[tuple[date, list[dict[str, Any]]]] | None = None


@dataclass(slots=True)
class _DisconnectWork:
    done: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None


class NativeRelayStreamWorker:
    """Run playback on the transport sequence proven against real cameras."""

    def __init__(
        self,
        config: NativeCameraSessionConfig,
        app_session: dict[str, Any],
        *,
        media_secrets: NativeMediaSecretStore | None = None,
        playback_factory: Callable[..., Any] = NativeRelayPlaybackSession,
        end_idle_timeout: float = 2.0,
    ) -> None:
        self._config = config.validate()
        self._app_session = app_session
        self._media_secrets = media_secrets
        self._playback_factory = playback_factory
        self._end_idle_timeout = end_idle_timeout
        self._requests: queue.Queue[object] = _WorkerQueue()
        self._state_lock = threading.Lock()
        self._closed = False
        self._shutdown_event = threading.Event()
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"tuya-recordings-stream-{config.dev_id}",
            daemon=True,
        )
        self._thread.start()

    def stream(
        self,
        request: NativePlaybackRequest,
        event_handler: Callable[[dict[str, Any]], None],
        cancel_event: Any,
    ) -> None:
        work = _PlaybackWork(request, event_handler, cancel_event)
        with self._state_lock:
            if self._closed:
                raise NativeRelayRuntimeError("Native playback worker is closed")
            self._requests.put(work)
        work.done.wait()
        if work.error is not None:
            raise work.error

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._shutdown_event.set()
            while True:
                try:
                    queued = self._requests.get_nowait()
                except queue.Empty:
                    break
                if isinstance(queued, _PlaybackWork | _DisconnectWork):
                    queued.error = NativeRelayRuntimeError(
                        "Native playback worker was closed"
                    )
                    queued.done.set()
            self._requests.put(_STOP_WORKER)

    def disconnect(self) -> None:
        """Close the idle playback transport without retiring the worker."""
        work = _DisconnectWork()
        with self._state_lock:
            if self._closed:
                return
            self._requests.put(work)
        work.done.wait()
        if work.error is not None:
            raise work.error

    def wait_closed(self, timeout: float = 2.0) -> bool:
        if threading.current_thread() is self._thread:
            return False
        self._thread.join(max(0.0, timeout))
        return not self._thread.is_alive() and not getattr(self, "_unfinished_transport", None)

    def uses_config(self, config: NativeCameraSessionConfig) -> bool:
        candidate = config.validate()
        return self._config == candidate

    def _thread_main(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        playback: Any = None
        last_work = time.monotonic()
        while True:
            try:
                work = (
                    await self._requests.get_async()
                    if playback is None
                    else await self._requests.get_async(0.5)
                )
            except queue.Empty:
                if time.monotonic() - last_work >= _PLAYBACK_IDLE_SECONDS:
                    with suppress(Exception):
                        await _close_worker_transport(self, playback)
                    playback = None
                continue
            if work is _STOP_WORKER:
                playback = playback or getattr(self, "_unfinished_transport", None)
                if playback is not None:
                    with suppress(Exception):
                        await _close_worker_transport(self, playback)
                return
            if isinstance(work, _DisconnectWork):
                try:
                    playback = playback or getattr(self, "_unfinished_transport", None)
                    if playback is not None:
                        await _close_worker_transport(self, playback)
                        playback = None
                except BaseException as err:  # noqa: BLE001 - cross-thread handoff
                    work.error = err
                finally:
                    work.done.set()
                continue
            assert isinstance(work, _PlaybackWork)
            failed = False
            try:
                cancellation = _CancellationGroup(
                    work.cancel_event, self._shutdown_event
                )
                if cancellation.is_set():
                    raise NativeRelayRuntimeError("Native playback was cancelled")
                if getattr(self, "_unfinished_transport", None) is not None:
                    raise NativeRelayRuntimeError("Previous native transport did not close")
                runtime = NativeRelayRuntimeSession(
                    self._config,
                    app_session=self._app_session,
                    cancel_event=cancellation,
                    playback_factory=self._playback_factory,
                    media_secrets=self._media_secrets,
                    end_idle_timeout=self._end_idle_timeout,
                )
                if playback is None:
                    playback = self._playback_factory(self._config, self._app_session)
                    await runtime._wait_cancelable(
                        playback.connect(allow_wake=True, **_offer_retry_options(work.retry_offer)),
                        timeout=runtime.signaling_timeout + 60 + WAKE_SLOT_TIMEOUT_SECONDS,
                    )
                await runtime._stream_connected(
                    playback,
                    work.request,
                    work.event_handler,
                )
            except BaseException as err:  # noqa: BLE001 - cross-thread handoff
                failed = True
                work.error = err
            finally:
                if playback is not None and getattr(
                    playback, "_playback_started", False
                ):
                    try:
                        await asyncio.wait_for(playback.stop(), timeout=_PLAYBACK_STOP_TIMEOUT_SECONDS)
                    except Exception:
                        with suppress(Exception):
                            await _close_worker_transport(self, playback)
                        playback = None
                if failed and playback is not None:
                    with suppress(Exception):
                        await _close_worker_transport(self, playback)
                    playback = None
                last_work = time.monotonic()
                work.done.set()


class NativeRelayPlaybackWorker:
    """Serialize catalog and thumbnail commands over one camera transport."""

    def __init__(
        self,
        config: NativeCameraSessionConfig,
        app_session: dict[str, Any],
        *,
        media_secrets: NativeMediaSecretStore | None = None,
        playback_factory: Callable[..., Any] = NativeRelayPlaybackSession,
        end_idle_timeout: float = 2.0,
        command_timeout: float = 15.0,
    ) -> None:
        self._config = config.validate()
        self._app_session = app_session
        self._media_secrets = media_secrets
        self._playback_factory = playback_factory
        self._end_idle_timeout = end_idle_timeout
        self._command_timeout = command_timeout
        self._requests: queue.Queue[object] = _WorkerQueue()
        self._state_lock = threading.Lock()
        self._closed = False
        self._shutdown_event = threading.Event()
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"tuya-recordings-{config.dev_id}",
            daemon=True,
        )
        self._thread.start()

    def stream(
        self,
        request: NativePlaybackRequest,
        event_handler: Callable[[dict[str, Any]], None],
        cancel_event: Any,
    ) -> None:
        work = _PlaybackWork(request, event_handler, cancel_event)
        self._submit(work)
        work.done.wait()
        if work.error is not None:
            raise work.error

    def interactive_stream(
        self,
        request: NativePlaybackRequest,
        event_handler: Callable[[dict[str, Any]], None],
        commands: queue.Queue[tuple[str, NativePlaybackRequest | None]],
        cancel_event: Any,
    ) -> None:
        """Keep one APK camera object active for timeline controls."""
        work = _InteractivePlaybackWork(
            request,
            event_handler,
            commands,
            cancel_event,
        )
        self._submit(work)
        work.done.wait()
        if work.error is not None:
            raise work.error

    def thumbnail(self, start: int, end: int, cancel_event: Any) -> bytes:
        """Download one thumbnail through the same sequential camera queue."""
        work = _ThumbnailWork(start, end, cancel_event)
        self._submit(work)
        work.done.wait()
        if work.error is not None:
            raise work.error
        if work.image is None:
            raise NativeRelayRuntimeError("Native thumbnail returned no image")
        return work.image

    def catalog(
        self, days: list[date], cancel_event: Any, *, events: bool = False, months: bool = False
    ) -> list[tuple[date, list[dict[str, Any]]]]:
        """Query recording days through the shared sequential camera session."""
        work = _CatalogWork(
            days, cancel_event, events=events, months=months,
            # SD readiness is a connection prerequisite, not a manual retry.
            allow_wake=True,
        )
        self._submit(work)
        work.done.wait()
        if work.error is not None:
            raise work.error
        if work.results is None:
            raise NativeRelayRuntimeError("Native catalog returned no result")
        return work.results

    def close(self) -> None:
        """Cancel queued work, stop accepting requests, and close transport."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._shutdown_event.set()
            while True:
                try:
                    queued = self._requests.get_nowait()
                except queue.Empty:
                    break
                if isinstance(
                    queued,
                    _PlaybackWork
                    | _InteractivePlaybackWork
                    | _ThumbnailWork
                    | _CatalogWork
                    | _DisconnectWork,
                ):
                    queued.error = NativeRelayRuntimeError(
                        "Native camera worker was closed"
                    )
                    queued.done.set()
            self._requests.put(_STOP_WORKER)

    def disconnect(self) -> None:
        """Close the idle metadata transport without retiring the worker."""
        work = _DisconnectWork()
        with self._state_lock:
            if self._closed:
                return
            self._requests.put(work)
        work.done.wait()
        if work.error is not None:
            raise work.error

    def wait_closed(self, timeout: float = 2.0) -> bool:
        """Wait briefly for the worker thread after cancellation was requested."""
        if threading.current_thread() is self._thread:
            return False
        self._thread.join(max(0.0, timeout))
        return not self._thread.is_alive() and not getattr(self, "_unfinished_transport", None)

    def _submit(
        self,
        work: _PlaybackWork | _InteractivePlaybackWork | _ThumbnailWork | _CatalogWork,
    ) -> None:
        with self._state_lock:
            if self._closed:
                raise NativeRelayRuntimeError("Native camera worker is closed")
            self._requests.put(work)

    def uses_config(self, config: NativeCameraSessionConfig) -> bool:
        """Return whether this worker owns the current APK connection data."""
        candidate = config.validate()
        # Smart Life issues the trace id with the camera configuration and then
        # passes that same value into connectV3. A worker may reuse a transport
        # while its complete configuration remains current, but it must rotate
        # after the provider refreshes that configuration. This consistency
        # rule is not evidence that trace reuse caused a missing IMM answer.
        return self._config == candidate

    def _thread_main(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        playback: Any = None
        connection_mode: str | None = None
        last_work = time.monotonic()
        while True:
            try:
                # Wake periodically only while a transport needs idle expiry.
                # Once disconnected, block until real work or the stop marker.
                work = (
                    await self._requests.get_async()
                    if playback is None
                    else await self._requests.get_async(0.5)
                )
            except queue.Empty:
                if (
                    playback is not None
                    and time.monotonic() - last_work >= _PLAYBACK_IDLE_SECONDS
                ):
                    with suppress(Exception):
                        await _close_worker_transport(self, playback)
                    playback = None
                    connection_mode = None
                elif isinstance(work, _InteractivePlaybackWork) and playback is not None:
                    # Leaving the APK viewer destroys its camera object. Keep no
                    # hidden ICE/MQTT transport after the browser session ends.
                    with suppress(Exception):
                        await _close_worker_transport(self, playback)
                    playback = None
                    connection_mode = None
                continue
            if work is _STOP_WORKER:
                playback = playback or getattr(self, "_unfinished_transport", None)
                if playback is not None:
                    with suppress(Exception):
                        await _close_worker_transport(self, playback)
                return
            if isinstance(work, _DisconnectWork):
                try:
                    playback = playback or getattr(self, "_unfinished_transport", None)
                    if playback is not None:
                        await _close_worker_transport(self, playback)
                        playback = None
                        connection_mode = None
                except BaseException as err:  # noqa: BLE001 - cross-thread handoff
                    work.error = err
                finally:
                    work.done.set()
                continue
            assert isinstance(
                work,
                _PlaybackWork | _InteractivePlaybackWork | _ThumbnailWork | _CatalogWork,
            )
            try:
                failed = False
                if self._work_cancelled(work):
                    raise NativeRelayRuntimeError("Native camera work was cancelled")
                if getattr(self, "_unfinished_transport", None) is not None:
                    raise NativeRelayRuntimeError("Previous native transport did not close")
                wanted_mode = "metadata" if isinstance(work, _CatalogWork | _ThumbnailWork) else "media"
                if playback is not None and connection_mode != wanted_mode:
                    try:
                        await _close_worker_transport(self, playback)
                    finally:
                        # Failed closure remains owned by _unfinished_transport.
                        # Do not retry implicitly or connect in this request.
                        playback = None
                        connection_mode = None
                if playback is None:
                    playback = self._playback_factory(
                        self._config,
                        self._app_session,
                    )
                    # Every newly opened SD connection enters the same APK
                    # readiness gate, regardless of the operation that needs it.
                    connect_options = {"allow_wake": True}
                    if not isinstance(work, _ThumbnailWork):
                        connect_options.update(_offer_retry_options(work.retry_offer))
                    await self._wait_for_work(
                        playback.connect(media=wanted_mode == "media", **connect_options),
                        work,
                        timeout=self._command_timeout + 60 + (
                            WAKE_SLOT_TIMEOUT_SECONDS if connect_options.get("allow_wake") else 0
                        ),
                        # A cancelled connect must close before priority work.
                        cancellable=True,
                    )
                    connection_mode = wanted_mode
                if isinstance(work, _InteractivePlaybackWork):
                    runtime = NativeRelayRuntimeSession(
                        self._config,
                        app_session=self._app_session,
                        cancel_event=self._cancellation_for(work),
                        playback_factory=self._playback_factory,
                        media_secrets=self._media_secrets,
                        end_idle_timeout=self._end_idle_timeout,
                    )
                    await runtime._interactive_connected(
                        playback,
                        work.request,
                        work.event_handler,
                        work.commands,
                    )
                elif isinstance(work, _PlaybackWork):
                    runtime = NativeRelayRuntimeSession(
                        self._config,
                        app_session=self._app_session,
                        cancel_event=self._cancellation_for(work),
                        playback_factory=self._playback_factory,
                        media_secrets=self._media_secrets,
                        end_idle_timeout=self._end_idle_timeout,
                    )
                    await self._wait_for_work(
                        runtime._stream_connected(
                            playback,
                            work.request,
                            work.event_handler,
                        ),
                        work,
                    )
                elif isinstance(work, _ThumbnailWork):
                    if work.cancel_event is not None and work.cancel_event.is_set():
                        raise NativeRelayRuntimeError("Native thumbnail was cancelled")
                    work.image = await self._wait_for_work(
                        playback.download_thumbnail(
                            start=work.start,
                            end=work.end,
                        ),
                        work,
                        # The transport owns the SDK-matched response timeout.
                        # Let it report its diagnostics before the worker's
                        # cancellation boundary wins the race.
                        timeout=(
                            self._command_timeout + _THUMBNAIL_DEADLINE_GRACE_SECONDS
                        ),
                    )
                else:
                    results: list[tuple[date, list[dict[str, Any]]]] = []
                    for day in work.days:
                        if work.cancel_event is not None and work.cancel_event.is_set():
                            work.error = NativeRelayRuntimeError(
                                "Native catalog was cancelled"
                            )
                            break
                        query = (
                            playback.query_event_recordings
                            if work.events
                            else playback.query_recordings
                        )
                        if work.months:
                            recorded = await self._wait_for_work(playback.query_recording_days(day), work,
                                                                  timeout=self._command_timeout)
                            results.append((day, [{"date": value.isoformat()} for value in recorded]))
                            continue
                        page = await self._wait_for_work(
                            query(day),
                            work,
                            timeout=self._command_timeout,
                        )
                        if self._media_secrets is not None:
                            await asyncio.to_thread(
                                self._media_secrets.prefetch,
                                [item.uuid for item in page.items if item.encrypt and item.uuid],
                            )
                        results.append(
                            (day, [item.as_recording() for item in page.items])
                        )
                    work.results = results
            except BaseException as err:  # noqa: BLE001 - cross-thread handoff
                failed = True
                work.error = err
            finally:
                if playback is not None and getattr(
                    playback, "_playback_started", False
                ):
                    try:
                        await asyncio.wait_for(
                            playback.stop(),
                            timeout=_PLAYBACK_STOP_TIMEOUT_SECONDS,
                        )
                    except Exception:
                        abort = getattr(playback, "abort", None)
                        if callable(abort):
                            with suppress(Exception):
                                await asyncio.wait_for(
                                    _close_worker_transport(self, playback, abort=True),
                                    timeout=_POOL_CLOSE_TIMEOUT_SECONDS,
                                )
                        else:
                            # Test doubles and alternate transports may not
                            # expose abort. Prevent close() from retrying the
                            # same unresponsive ordered stop operation.
                            playback._playback_started = False
                            with suppress(Exception):
                                await asyncio.wait_for(
                                    _close_worker_transport(self, playback),
                                    timeout=_POOL_CLOSE_TIMEOUT_SECONDS,
                                )
                        playback = None
                        connection_mode = None
                if failed and playback is not None:
                    # Cancellation or a failed command can leave the camera's
                    # native playback transaction half-open. Do not hand that
                    # transport to the next clip.
                    with suppress(Exception):
                        await _close_worker_transport(self, playback)
                    playback = None
                    connection_mode = None
                last_work = time.monotonic()
                work.done.set()

    async def _wait_for_work(
        self,
        operation: Any,
        work: _PlaybackWork | _InteractivePlaybackWork | _ThumbnailWork | _CatalogWork,
        *,
        timeout: float | None = None,
        cancellable: bool = True,
    ) -> Any:
        """Await one operation while honoring worker close and request cancellation."""
        task = asyncio.create_task(operation)
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        try:
            while True:
                if (
                    self._work_cancelled(work)
                    if cancellable
                    else self._shutdown_event.is_set()
                ):
                    raise NativeRelayRuntimeError("Native camera work was cancelled")
                if deadline is not None:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise NativeRelayRuntimeError(
                            "Native camera operation exceeded its bounded deadline"
                        )
                else:
                    remaining = 0.1
                done, _ = await asyncio.wait({task}, timeout=min(0.1, remaining))
                if task in done:
                    return task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def _cancellation_for(
        self,
        work: _PlaybackWork | _InteractivePlaybackWork | _ThumbnailWork | _CatalogWork,
    ) -> _CancellationGroup:
        return _CancellationGroup(work.cancel_event, self._shutdown_event)

    def _work_cancelled(
        self,
        work: _PlaybackWork | _InteractivePlaybackWork | _ThumbnailWork | _CatalogWork,
    ) -> bool:
        return self._cancellation_for(work).is_set()


class NativeRelayRuntimePool:
    """Own one sequential APK-native transport worker per camera."""

    def __init__(
        self,
        app_session: dict[str, Any],
        *,
        media_secrets: NativeMediaSecretStore | None = None,
        call_api: Any = None,
    ) -> None:
        self._app_session = app_session
        self._media_secrets = media_secrets
        self._playback_factory = partial(
            NativeRelayPlaybackSession, call_api=call_api,
        )
        self._workers: dict[str, NativeRelayPlaybackWorker] = {}
        self._lock = threading.Lock()
        self._closed = False

    def session(
        self,
        config: NativeCameraSessionConfig,
        cancel_event: Any,
    ) -> NativeRelayRuntimeSession:
        config = config.validate()
        if config.p2p_type != 4:
            raise NativeRelayRuntimeError("Native playback currently supports P2P type 4 cameras")
        with self._lock:
            if self._closed:
                raise NativeRelayRuntimeError("Native runtime pool is closed")
            worker = self._workers.get(config.dev_id)
            if worker is not None and not worker.uses_config(config):
                worker.close()
                if not worker.wait_closed(_POOL_CLOSE_TIMEOUT_SECONDS):
                    raise NativeRelayRuntimeError(
                        "Previous native camera worker did not close cleanly"
                    )
                self._workers.pop(config.dev_id, None)
                worker = None
            if worker is None:
                worker = NativeRelayPlaybackWorker(
                    config,
                    self._app_session,
                    media_secrets=self._media_secrets,
                    playback_factory=self._playback_factory,
                )
                self._workers[config.dev_id] = worker
        return NativeRelayRuntimeSession(
            config,
            app_session=self._app_session,
            cancel_event=cancel_event,
            media_secrets=self._media_secrets,
            playback_worker=worker,
            command_worker=worker,
        )

    def close(self) -> None:
        """Close every lazily-created camera worker."""
        with self._lock:
            if self._closed and not self._workers:
                return
            self._closed = True
            workers = tuple(self._workers.items())
        # Cancel every camera first so one slow transport cannot delay
        # cancellation of the remaining workers.
        for _, worker in workers:
            worker.close()
        deadline = time.monotonic() + _POOL_CLOSE_TIMEOUT_SECONDS
        unfinished = 0
        for dev_id, worker in workers:
            if not worker.wait_closed(max(0.0, deadline - time.monotonic())):
                unfinished += 1
            else:
                with self._lock:
                    self._workers.pop(dev_id, None)
        if unfinished:
            raise NativeRelayRuntimeError(
                f"{unfinished} native camera worker(s) did not stop within the cleanup deadline"
            )


@dataclass(slots=True)
class NativeRelayRuntimeSession:
    """Expose one camera as the backend's synchronous native-session contract."""

    config: NativeCameraSessionConfig
    app_session: dict[str, Any] = field(repr=False)
    cancel_event: Any = field(default=None, repr=False)
    protocol: int = NATIVE_PLAYBACK_EVENT_VERSION
    signaling_timeout: float = 15.0
    first_frame_timeout: float = 12.0
    end_idle_timeout: float = 2.0
    playback_factory: Callable[..., Any] = field(
        default=NativeRelayPlaybackSession, repr=False
    )
    clock: Callable[[], float] = field(default=time.time, repr=False)
    media_secrets: NativeMediaSecretStore | None = field(default=None, repr=False)
    playback_worker: NativeRelayPlaybackWorker | None = field(default=None, repr=False)
    command_worker: NativeRelayPlaybackWorker | None = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _unfinished_transport: Any = field(default=None, init=False, repr=False)
    _cleanup_owner: object = field(default_factory=object, init=False, repr=False)

    def __post_init__(self) -> None:
        self.config = self.config.validate()
        if not 0 < self.signaling_timeout <= 60:
            raise NativeRelayRuntimeError("Native signaling timeout is invalid")
        if not 0 < self.first_frame_timeout <= 60:
            raise NativeRelayRuntimeError("Native first-frame timeout is invalid")
        if not 0 < self.end_idle_timeout <= 10:
            raise NativeRelayRuntimeError("Native end idle timeout is invalid")

    def close(self) -> None:
        if self._closed:
            return
        if self._unfinished_transport is not None:
            raise NativeRelayRuntimeError("One-shot native transport cleanup incomplete")
        # The backend still owns CAMERA_COMMANDS here. Release the transport
        # before that ownership passes to another camera.
        workers = {id(worker): worker for worker in (self.command_worker, self.playback_worker) if worker is not None}
        for worker in workers.values():
            worker.disconnect()
        self._closed = True

    def start_playback_stream(
        self,
        request: NativePlaybackRequest,
        event_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if self._closed:
            raise NativeRelayRuntimeError("Native runtime session is closed")
        request = replace(
            request,
            play_mode_supported=self.config.playback_mode_supported,
        ).validate()
        if event_handler is None:
            raise NativeRelayRuntimeError("Native playback requires an event handler")
        if request.encrypted and (
            not request.encryption_uuid.strip() or self.media_secrets is None
        ):
            raise NativeRelayRuntimeError(
                "Encrypted SD-card playback has no recording secret source"
            )
        if self._cancelled():
            raise NativeRelayRuntimeError("Native playback was cancelled")
        if self.playback_worker is not None:
            if (
                self.command_worker is not None
                and self.command_worker is not self.playback_worker
            ):
                self.command_worker.disconnect()
            self.playback_worker.stream(
                request,
                event_handler,
                self.cancel_event,
            )
            return
        try:
            asyncio.run(self._stream(request, event_handler))
        except NativeRelayRuntimeError:
            raise
        except Exception as err:
            raise NativeRelayRuntimeError(
                f"Native playback failed: {type(err).__name__}"
            ) from err

    def start_interactive_playback_stream(
        self,
        request: NativePlaybackRequest,
        commands: queue.Queue[tuple[str, NativePlaybackRequest | None]],
        event_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Run the APK timeline on one retained camera session."""
        if self._closed:
            raise NativeRelayRuntimeError("Native runtime session is closed")
        request = replace(
            request,
            play_mode_supported=self.config.playback_mode_supported,
        ).validate()
        if event_handler is None:
            raise NativeRelayRuntimeError("Interactive playback requires an event handler")
        if self._cancelled():
            raise NativeRelayRuntimeError("Native playback was cancelled")
        if self.playback_worker is None:
            raise NativeRelayRuntimeError("Interactive playback requires a pooled camera worker")
        self.playback_worker.interactive_stream(
            request,
            event_handler,
            commands,
            self.cancel_event,
        )

    def download_thumbnail(self, start: int, end: int) -> bytes:
        """Download one APK-native recording JPEG without opening media."""
        if self._closed:
            raise NativeRelayRuntimeError("Native runtime session is closed")
        if type(start) is not int or type(end) is not int or start < 0 or end <= start:
            raise NativeRelayRuntimeError("Native thumbnail bounds are invalid")
        if self._cancelled():
            raise NativeRelayRuntimeError("Native thumbnail was cancelled")
        worker = self.command_worker or self.playback_worker
        if worker is not None and hasattr(worker, "thumbnail"):
            if self.playback_worker is not None and self.playback_worker is not worker:
                self.playback_worker.disconnect()
            return worker.thumbnail(start, end, self.cancel_event)
        try:
            return asyncio.run(self._download_thumbnail(start, end))
        except NativeRelayRuntimeError:
            raise
        except Exception as err:
            raise NativeRelayRuntimeError(
                f"Native thumbnail failed: {type(err).__name__}"
            ) from err

    def recordings_for_day(self, day: date) -> list[dict[str, Any]]:
        """Query one camera day through one bounded native IMM transaction."""
        if self._closed:
            raise NativeRelayRuntimeError("Native runtime session is closed")
        if not isinstance(day, date):
            raise NativeRelayRuntimeError("Native catalog day must be a date")
        if self._cancelled():
            raise NativeRelayRuntimeError("Native catalog was cancelled")
        worker = self.command_worker or self.playback_worker
        if worker is not None and hasattr(worker, "catalog"):
            if self.playback_worker is not None and self.playback_worker is not worker:
                self.playback_worker.disconnect()
            return worker.catalog(
                [day],
                self.cancel_event,
            )[0][1]
        try:
            return asyncio.run(self._recordings_for_day(day))
        except NativeRelayRuntimeError:
            raise
        except Exception as err:
            raise NativeRelayRuntimeError(
                f"Native catalog failed: {type(err).__name__}"
            ) from err

    def recording_days_for_month(self, month: date) -> list[date]:
        if self._closed or self._cancelled():
            raise NativeRelayRuntimeError("Native month query was cancelled")
        worker = self.command_worker or self.playback_worker
        if worker is None:
            raise NativeRelayRuntimeError("Native month query requires the serialized worker")
        rows = worker.catalog([month], self.cancel_event, months=True)[0][1]
        return [date.fromisoformat(row["date"]) for row in rows]

    def event_recordings_for_day(self, day: date) -> list[dict[str, Any]]:
        """Query the APK event timeline when the camera advertises it."""
        if self._closed:
            raise NativeRelayRuntimeError("Native runtime session is closed")
        if not isinstance(day, date):
            raise NativeRelayRuntimeError("Native event catalog day must be a date")
        if self._cancelled():
            raise NativeRelayRuntimeError("Native event catalog was cancelled")
        if not self.config.event_catalog_supported:
            raise NativeRelayRuntimeError(
                "Smart Life event-catalog capability bit is absent"
            )
        worker = self.command_worker or self.playback_worker
        if worker is not None and hasattr(worker, "catalog"):
            if self.playback_worker is not None and self.playback_worker is not worker:
                self.playback_worker.disconnect()
            return worker.catalog([day], self.cancel_event, events=True)[0][1]
        try:
            return asyncio.run(self._event_recordings_for_day(day))
        except NativeRelayRuntimeError:
            raise
        except Exception as err:
            raise NativeRelayRuntimeError(
                f"Native event catalog failed: {type(err).__name__}"
            ) from err

    def recordings_for_days(
        self, days: list[date]
    ) -> list[tuple[date, list[dict[str, Any]]]]:
        """Query a newest-first day batch through one MQTT/P2P connection."""
        if self._closed:
            raise NativeRelayRuntimeError("Native runtime session is closed")
        if not days or any(not isinstance(day, date) for day in days):
            raise NativeRelayRuntimeError("Native catalog days must be dates")
        if days != sorted(set(days), reverse=True):
            raise NativeRelayRuntimeError(
                "Native catalog days must be unique and newest first"
            )
        if self._cancelled():
            raise NativeRelayRuntimeError("Native catalog was cancelled")
        worker = self.command_worker or self.playback_worker
        if worker is not None and hasattr(worker, "catalog"):
            if self.playback_worker is not None and self.playback_worker is not worker:
                self.playback_worker.disconnect()
            return worker.catalog(days, self.cancel_event)
        try:
            return asyncio.run(self._recordings_for_days(days))
        except NativeRelayRuntimeError:
            raise
        except Exception as err:
            raise NativeRelayRuntimeError(
                f"Native catalog failed: {type(err).__name__}"
            ) from err

    async def _stream(
        self,
        request: NativePlaybackRequest,
        event_handler: Callable[[dict[str, Any]], None],
    ) -> None:
        playback = self.playback_factory(
            self.config,
            self.app_session,
        )
        try:
            await self._wait_cancelable(
                playback.connect(media=True, allow_wake=True, **_offer_retry_options(ON_DEMAND_OFFER_RETRIES.get())),
                timeout=self.signaling_timeout + 60 + WAKE_SLOT_TIMEOUT_SECONDS,
            )
            await self._stream_connected(playback, request, event_handler)
        finally:
            await _close_worker_transport(self, playback)

    async def _download_thumbnail(self, start: int, end: int) -> bytes:
        playback = self.playback_factory(
            self.config,
            self.app_session,
        )
        try:
            await self._wait_cancelable(
                playback.connect(media=True, allow_wake=True),
                timeout=self.signaling_timeout + 60 + WAKE_SLOT_TIMEOUT_SECONDS,
            )
            return await self._wait_cancelable(
                playback.download_thumbnail(start=start, end=end),
                timeout=self.signaling_timeout,
            )
        finally:
            await _close_worker_transport(self, playback)

    async def _stream_connected(
        self,
        playback: Any,
        request: NativePlaybackRequest,
        event_handler: Callable[[dict[str, Any]], None],
    ) -> None:
        """Play one clip over an already connected native transport."""
        request = await self._prepare_playback_request(playback, request, {})
        playback_secrets, _ = await asyncio.to_thread(self._playback_secrets, request)
        sink = NativePlaybackEventSink(event_handler, protocol=self.protocol)
        started = False
        try:
            await self._wait_cancelable(
                playback.start(
                    start=request.start,
                    end=request.end,
                    play_time=request.position,
                    encrypted=request.encrypted,
                    fragments_json=request.fragments_json,
                    play_mode_supported=request.play_mode_supported,
                ),
                timeout=self.signaling_timeout + 15,
            )
            started = True
            # Do not expose a browser stream until the camera has accepted the
            # APK playback command. A signed URL or an open transport is not a
            # started playback session.
            sink.started(
                {"transport": "tuya-imm-simple", "audio": True, "video": True}
            )
            await self._receive_media(
                playback,
                sink,
                request.end - request.position,
                playback_secrets=playback_secrets,
                recording_uuid=request.encryption_uuid,
            )
        except NativeThumbnailReady:
            return
        finally:
            if started:
                with suppress(Exception):
                    await asyncio.wait_for(playback.stop(), timeout=5)
        sink.finished({"reason": "camera-stream-complete"})

    async def _interactive_connected(
        self,
        playback: Any,
        request: NativePlaybackRequest,
        event_handler: Callable[[dict[str, Any]], None],
        commands: queue.Queue[tuple[str, NativePlaybackRequest | None]],
    ) -> None:
        """Mirror the APK timeline lifecycle on one connected camera object."""
        sink = NativePlaybackEventSink(event_handler, protocol=self.protocol)
        current = replace(
            request,
            play_mode_supported=self.config.playback_mode_supported,
        ).validate()
        catalogs: dict[date, Any] = {}
        current = await self._prepare_playback_request(playback, current, catalogs)
        playback_secrets, recording_uuid = await asyncio.to_thread(self._playback_secrets, current)
        receive_task: asyncio.Task[Any] | None = None
        paused = False
        started = False
        generation = 1
        audio_codec_info = None
        loop = asyncio.get_running_loop()
        video_deadline = loop.time() + self.first_frame_timeout
        saw_audio = False
        saw_video = False
        empty_media_frames = 0
        deferred_command: tuple[str, NativePlaybackRequest | None] | None = None
        finished_reason = "user-stop"
        try:
            await self._wait_cancelable(
                playback.start(
                    start=current.start,
                    end=current.end,
                    play_time=current.position,
                    encrypted=current.encrypted,
                    fragments_json=current.fragments_json,
                    play_mode_supported=current.play_mode_supported,
                ),
                timeout=self.signaling_timeout + 15,
            )
            started = True
            sink.started(
                {
                    "transport": "tuya-imm-simple",
                    "generation": generation,
                    "position": current.position,
                }
            )
            video_deadline = loop.time() + self.first_frame_timeout
            while not self._cancelled():
                command = deferred_command
                deferred_command = None
                if command is None:
                    try:
                        command = commands.get_nowait()
                    except queue.Empty:
                        pass
                if command is not None:
                    action, next_request = command
                    if action == "seek":
                        # A timeline is positional rather than a queue of
                        # clips. Keep only the latest contiguous seek, while
                        # preserving pause/resume/stop ordering behind it.
                        while True:
                            try:
                                candidate = commands.get_nowait()
                            except queue.Empty:
                                break
                            if candidate[0] == "seek":
                                action, next_request = candidate
                                continue
                            deferred_command = candidate
                            break
                    if action == "stop":
                        break
                    if action == "pause":
                        await self._wait_cancelable(
                            playback.pause(), timeout=self.signaling_timeout
                        )
                        paused = True
                        continue
                    if action == "resume":
                        await self._wait_cancelable(
                            playback.resume(), timeout=self.signaling_timeout
                        )
                        paused = False
                        video_deadline = loop.time() + self.first_frame_timeout
                        continue
                    if action != "seek" or next_request is None:
                        raise NativeRelayRuntimeError(
                            f"Unknown interactive playback command: {action}"
                        )
                    if receive_task is not None:
                        receive_task.cancel()
                        await asyncio.gather(receive_task, return_exceptions=True)
                        receive_task = None
                    current = replace(
                        next_request,
                        play_mode_supported=self.config.playback_mode_supported,
                    ).validate()
                    needs_catalog = current.catalog_day is not None and (
                        current.catalog_day not in catalogs or not any(
                            item.start_time <= current.position < item.end_time
                            for item in catalogs[current.catalog_day].items
                        )
                    )
                    if needs_catalog:
                        # The SDK cannot query a day during active playback.
                        await self._wait_cancelable(playback.stop(), timeout=self.signaling_timeout)
                        catalogs.pop(current.catalog_day, None)
                    current = await self._prepare_playback_request(playback, current, catalogs)
                    playback_secrets, recording_uuid = await asyncio.to_thread(self._playback_secrets, current)
                    generation += 1
                    audio_codec_info = None
                    saw_audio = False
                    saw_video = False
                    empty_media_frames = 0
                    video_deadline = loop.time() + self.first_frame_timeout
                    await self._wait_cancelable(
                        (playback.start if needs_catalog else playback.seek)(
                            start=current.start,
                            end=current.end,
                            play_time=current.position,
                            encrypted=current.encrypted,
                            fragments_json=current.fragments_json,
                            play_mode_supported=current.play_mode_supported,
                        ),
                        timeout=self.signaling_timeout + 15,
                    )
                    video_deadline = loop.time() + self.first_frame_timeout
                    sink.started(
                        {
                            "transport": "tuya-imm-simple",
                            "generation": generation,
                            "position": current.position,
                        }
                    )
                    paused = False
                    continue
                if paused:
                    await asyncio.sleep(0.05)
                    continue
                if loop.time() >= video_deadline:
                    media_seen = "video stalled" if saw_video else "audio but no video" if saw_audio else "no media (no usable video)"
                    raise NativeRelayRuntimeError(
                        f"Camera returned {media_seen} before the playback deadline "
                        f"({getattr(playback, 'transport_diagnostics', 'transport diagnostics unavailable')})"
                    )
                if receive_task is None:
                    receive_task = asyncio.create_task(playback.receive_frame())
                done, _ = await asyncio.wait({receive_task}, timeout=0.05)
                if receive_task not in done:
                    continue
                try:
                    frame = receive_task.result()
                except NativePlaybackCompleted as err:
                    if not (saw_video and saw_audio):
                        raise NativeRelayRuntimeError(
                            "Camera completed SD playback before delivering both video and audio "
                            f"(video={saw_video}, audio={saw_audio}; {playback.transport_diagnostics})"
                        ) from err
                    finished_reason = "camera-stream-complete"
                    break
                receive_task = None
                if frame.is_audio:
                    if frame.codec_info is not None:
                        audio_codec_info = frame.codec_info
                    elif audio_codec_info is not None:
                        frame = replace(frame, codec_info=audio_codec_info)
                converted = self._convert_frame(
                    frame,
                    playback_secrets,
                    recording_uuid,
                )
                if converted is None:
                    empty_media_frames += 1
                    # Do not let an active camera stream only empty frames forever.
                    if empty_media_frames > _MAX_CONSECUTIVE_EMPTY_MEDIA_FRAMES:
                        raise NativeRelayRuntimeError(
                            "Camera sent too many consecutive empty playback frames"
                        )
                    continue
                empty_media_frames = 0
                accepted = await _thread_job(sink.frame, converted)
                if frame.is_audio:
                    saw_audio = True
                elif accepted is not False:
                    saw_video = True
                    video_deadline = loop.time() + self.first_frame_timeout
        finally:
            if receive_task is not None:
                receive_task.cancel()
                await asyncio.gather(receive_task, return_exceptions=True)
            if started:
                with suppress(Exception):
                    await asyncio.wait_for(
                        playback.stop(),
                        timeout=_PLAYBACK_STOP_TIMEOUT_SECONDS,
                    )
        sink.finished({"reason": finished_reason})

    async def _prepare_playback_request(
        self, playback: Any, request: NativePlaybackRequest, catalogs: dict[date, Any]
    ) -> NativePlaybackRequest:
        """Select a real day fragment before starting, retaining it for seeks."""
        day = request.catalog_day
        if day is None:
            return request
        if day not in catalogs:
            try:
                page = await self._wait_cancelable(
                    playback.query_recordings(day), timeout=self.signaling_timeout
                )
            except TimeoutError as err:
                raise NativeRelayRuntimeError(
                    f"Playback catalog timed out ({playback.transport_diagnostics})"
                ) from err
            if page.status != 0:
                raise NativeRelayRuntimeError("Camera rejected the playback catalog query")
            catalogs[day] = page
            _LOGGER.debug("Native playback catalog loaded: day=%s fragments=%s", day, len(page.items))
        matches = [
            item for item in catalogs[day].items
            if item.start_time <= request.position < item.end_time
        ]
        if not matches:
            raise NativeRelayRuntimeError(
                "Camera's fresh SD catalog does not contain the selected playback position "
                f"(day={day}, position={request.position}, fragments={len(catalogs[day].items)})"
            )
        item = max(matches, key=lambda item: min(item.end_time, request.end) - max(item.start_time, request.start))
        start, end = max(request.start, item.start_time), min(request.end, item.end_time)
        encrypted = bool(item.encrypt or (item.uuid and item.encrypt_md5))
        _LOGGER.debug(
            "Native playback fragment selected: requested=%s..%s catalog=%s..%s selected=%s..%s position=%s encrypted=%s",
            request.start, request.end, item.start_time, item.end_time, start, end, request.position, encrypted,
        )
        return replace(
            request, start=start, end=end, encrypted=encrypted,
            encryption_uuid=item.uuid if encrypted else "",
            fragments_json=json.dumps({"fragments": [{"start": start, "end": end}]}),
        ).validate()

    def _playback_secrets(
        self,
        request: NativePlaybackRequest,
    ) -> tuple[dict[str, str], str]:
        secrets: dict[str, str] = {}
        if request.encrypted:
            if self.media_secrets is None or not request.encryption_uuid.strip():
                raise NativeRelayRuntimeError(
                    "Encrypted SD-card playback has no recording secret source"
                )
            secrets[request.encryption_uuid.strip()] = self.media_secrets.secret(
                request.encryption_uuid.strip()
            )
        return secrets, request.encryption_uuid

    @staticmethod
    def _convert_frame(
        frame: Any,
        playback_secrets: dict[str, str],
        recording_uuid: str,
    ) -> Any:
        try:
            decrypted = decrypt_playback_frame(
                frame,
                playback_secrets,
                recording_uuid=recording_uuid,
            )
        except NativeMediaCryptoError as err:
            raise NativeRelayRuntimeError(
                f"Native playback media decryption failed: {err}"
            ) from err
        # A zero-length wire payload or a valid all-padding encrypted payload
        # contains no media. Leave other frame types to the converter to reject.
        if not decrypted.payload and frame.frame_type in (1, 3):
            _LOGGER.debug(
                "Ignoring empty native playback media frame: origin=%s "
                "frame_type=%d wire_payload_length=%d",
                "decrypted" if frame.payload else "wire",
                frame.frame_type,
                len(frame.payload),
            )
            return None
        return playback_stream_frame(decrypted)

    async def _recordings_for_day(self, day: date) -> list[dict[str, Any]]:
        results = await self._recordings_for_days([day])
        return results[0][1]

    async def _event_recordings_for_day(self, day: date) -> list[dict[str, Any]]:
        playback = self.playback_factory(self.config, self.app_session)
        try:
            await self._wait_cancelable(
                playback.connect(media=False, allow_wake=True, **_offer_retry_options(ON_DEMAND_OFFER_RETRIES.get())),
                timeout=self.signaling_timeout + 60 + WAKE_SLOT_TIMEOUT_SECONDS,
            )
            try:
                page = await self._wait_cancelable(
                    playback.query_event_recordings(day),
                    timeout=self.signaling_timeout,
                )
            except TimeoutError as err:
                raise NativeRelayRuntimeError(
                    f"Native event catalog timed out ({playback.transport_diagnostics})"
                ) from err
            if self.media_secrets is not None:
                await asyncio.to_thread(self.media_secrets.prefetch,
                    [item.uuid for item in page.items if item.encrypt and item.uuid])
            return [item.as_recording() for item in page.items]
        finally:
            await _close_worker_transport(self, playback)

    async def _recordings_for_days(
        self, days: list[date]
    ) -> list[tuple[date, list[dict[str, Any]]]]:
        playback = self.playback_factory(
            self.config,
            self.app_session,
        )
        try:
            await self._wait_cancelable(
                playback.connect(media=False, allow_wake=True, **_offer_retry_options(ON_DEMAND_OFFER_RETRIES.get())),
                timeout=self.signaling_timeout + 60 + WAKE_SLOT_TIMEOUT_SECONDS,
            )
            results: list[tuple[date, list[dict[str, Any]]]] = []
            for day in days:
                if self._cancelled():
                    raise NativeRelayRuntimeError("Native catalog was cancelled")
                try:
                    page = await self._wait_cancelable(
                        playback.query_recordings(day),
                        timeout=self.signaling_timeout,
                    )
                except TimeoutError as err:
                    raise NativeRelayRuntimeError(
                        f"Native catalog timed out ({playback.transport_diagnostics})"
                    ) from err
                if self.media_secrets is not None:
                    await asyncio.to_thread(self.media_secrets.prefetch,
                        [item.uuid for item in page.items if item.encrypt and item.uuid])
                results.append((day, [item.as_recording() for item in page.items]))
            return results
        finally:
            await _close_worker_transport(self, playback)

    async def _receive_media(
        self,
        playback: Any,
        sink: NativePlaybackEventSink,
        duration: int,
        *,
        playback_secrets: dict[str, str],
        recording_uuid: str,
    ) -> None:
        loop = asyncio.get_running_loop()
        startup_deadline = loop.time() + self.first_frame_timeout
        media_deadline: float | None = None
        saw_audio = False
        saw_video = False
        audio_codec_info = None
        while True:
            if self._cancelled():
                raise NativeRelayRuntimeError("Native playback was cancelled")
            remaining = (media_deadline or startup_deadline) - loop.time()
            if remaining <= 0:
                if media_deadline is not None and saw_audio and saw_video:
                    return
                raise NativeRelayRuntimeError(
                    "Native playback exceeded its bounded deadline"
                )
            timeout = (
                self.end_idle_timeout
                if saw_audio and saw_video
                else self.first_frame_timeout
            )
            try:
                frame = await self._wait_cancelable(
                    playback.receive_frame(), timeout=min(timeout, remaining)
                )
            except NativePlaybackCompleted as err:
                if saw_audio and saw_video:
                    return
                raise NativeRelayRuntimeError(
                    "Camera completed SD playback before delivering both video and audio "
                    f"(video={saw_video}, audio={saw_audio}; {playback.transport_diagnostics})"
                ) from err
            except TimeoutError as err:
                if saw_audio and saw_video:
                    return
                raise NativeRelayRuntimeError(
                    "Native playback did not deliver both video and audio "
                    f"(video={saw_video}, audio={saw_audio}; "
                    f"{playback.transport_diagnostics})"
                ) from err
            if frame.is_audio:
                if frame.codec_info is not None:
                    audio_codec_info = frame.codec_info
                elif audio_codec_info is not None:
                    frame = replace(frame, codec_info=audio_codec_info)
            converted = self._convert_frame(frame, playback_secrets, recording_uuid)
            if converted is None:
                continue
            if media_deadline is None:
                media_deadline = loop.time() + max(float(duration), 0.1)
            info = converted.info or {}
            if info.get("avChannel") == 1:
                saw_audio = True
            else:
                saw_video = True
            await _thread_job(sink.frame, converted)

    async def _wait_cancelable(self, operation: Any, *, timeout: float) -> Any:
        """Run one camera operation while honoring a browser handoff promptly."""
        task = asyncio.create_task(operation)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            while True:
                if self._cancelled():
                    raise NativeRelayRuntimeError("Native playback was cancelled")
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                done, _ = await asyncio.wait({task}, timeout=min(0.1, remaining))
                if task in done:
                    return task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def _cancelled(self) -> bool:
        return self._closed or bool(
            self.cancel_event is not None and self.cancel_event.is_set()
        )
