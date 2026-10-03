"""One-shot synchronous adapter for APK-native IMM SD-card playback."""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from .native_frames import NATIVE_PLAYBACK_EVENT_VERSION, NativePlaybackEventSink
from .native_media_crypto import (
    NativeMediaCryptoError,
    NativeMediaSecretStore,
    decrypt_playback_frame,
)
from .native_relay_media import playback_stream_frame
from .native_relay_playback import NativeRelayPlaybackSession
from .native_session import NativeCameraSessionConfig, NativePlaybackRequest
from .native_thumbnail import NativeThumbnailReady


class NativeRelayRuntimeError(RuntimeError):
    """The bounded APK-native playback transaction failed."""


_PLAYBACK_IDLE_SECONDS = 8.0
_POOL_CLOSE_TIMEOUT_SECONDS = 2.0
_PLAYBACK_STOP_TIMEOUT_SECONDS = 5.0
_THUMBNAIL_DEADLINE_GRACE_SECONDS = 1.0
_STOP_WORKER = object()


@dataclass(slots=True)
class _PlaybackWork:
    request: NativePlaybackRequest
    event_handler: Callable[[dict[str, Any]], None]
    cancel_event: Any
    done: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None


@dataclass(slots=True)
class _InteractivePlaybackWork:
    request: NativePlaybackRequest
    event_handler: Callable[[dict[str, Any]], None]
    commands: queue.Queue[tuple[str, NativePlaybackRequest | None]]
    cancel_event: Any
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
        self._requests: queue.Queue[object] = queue.Queue()
        self._state_lock = threading.Lock()
        self._closed = False
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
        return not self._thread.is_alive()

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
                    self._requests.get()
                    if playback is None
                    else self._requests.get(timeout=0.5)
                )
            except queue.Empty:
                if time.monotonic() - last_work >= _PLAYBACK_IDLE_SECONDS:
                    with suppress(Exception):
                        await playback.close()
                    playback = None
                continue
            if work is _STOP_WORKER:
                if playback is not None:
                    with suppress(Exception):
                        await playback.close()
                return
            if isinstance(work, _DisconnectWork):
                try:
                    if playback is not None:
                        await playback.close()
                        playback = None
                except BaseException as err:  # noqa: BLE001 - cross-thread handoff
                    work.error = err
                finally:
                    work.done.set()
                continue
            assert isinstance(work, _PlaybackWork)
            failed = False
            try:
                if self._closed or (
                    work.cancel_event is not None and work.cancel_event.is_set()
                ):
                    raise NativeRelayRuntimeError("Native playback was cancelled")
                if playback is None:
                    playback = self._playback_factory(
                        self._config,
                        self._app_session,
                    )
                    await playback.connect()
                runtime = NativeRelayRuntimeSession(
                    self._config,
                    app_session=self._app_session,
                    cancel_event=work.cancel_event,
                    playback_factory=self._playback_factory,
                    media_secrets=self._media_secrets,
                    end_idle_timeout=self._end_idle_timeout,
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
                        await playback.stop()
                    except Exception:
                        with suppress(Exception):
                            await playback.close()
                        playback = None
                if failed and playback is not None:
                    with suppress(Exception):
                        await playback.close()
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
        self._requests: queue.Queue[object] = queue.Queue()
        self._state_lock = threading.Lock()
        self._closed = False
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
        self, days: list[date], cancel_event: Any, *, events: bool = False
    ) -> list[tuple[date, list[dict[str, Any]]]]:
        """Query recording days through the shared sequential camera session."""
        work = _CatalogWork(days, cancel_event, events=events)
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
        return not self._thread.is_alive()

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
        # after the provider refreshes that configuration. Keeping the old
        # trace indefinitely prevents a camera that rebooted or returned from
        # an outage from accepting a new IMM offer.
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
                    self._requests.get()
                    if playback is None
                    else self._requests.get(timeout=0.5)
                )
            except queue.Empty:
                if (
                    playback is not None
                    and time.monotonic() - last_work >= _PLAYBACK_IDLE_SECONDS
                ):
                    with suppress(Exception):
                        await playback.close()
                    playback = None
                    connection_mode = None
                elif isinstance(work, _InteractivePlaybackWork) and playback is not None:
                    # Leaving the APK viewer destroys its camera object. Keep no
                    # hidden ICE/MQTT transport after the browser session ends.
                    with suppress(Exception):
                        await playback.close()
                    playback = None
                    connection_mode = None
                continue
            if work is _STOP_WORKER:
                if playback is not None:
                    with suppress(Exception):
                        await playback.close()
                return
            if isinstance(work, _DisconnectWork):
                try:
                    if playback is not None:
                        await playback.close()
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
                # Smart Life performs catalog, JPEG, and playback commands on
                # one fully initialized camera session. Reusing that session
                # avoids back-to-back IMM offers that cameras may reject.
                wanted_mode = "media"
                if playback is not None and connection_mode != wanted_mode:
                    with suppress(Exception):
                        await playback.close()
                    playback = None
                    connection_mode = None
                if playback is None:
                    playback = self._playback_factory(
                        self._config,
                        self._app_session,
                    )
                    await self._wait_for_work(
                        playback.connect(media=wanted_mode == "media"),
                        work,
                        timeout=self._command_timeout + 60,
                    )
                    connection_mode = wanted_mode
                if isinstance(work, _InteractivePlaybackWork):
                    runtime = NativeRelayRuntimeSession(
                        self._config,
                        app_session=self._app_session,
                        cancel_event=work.cancel_event,
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
                        cancel_event=work.cancel_event,
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
                            raise NativeRelayRuntimeError(
                                "Native catalog was cancelled"
                            )
                        query = (
                            playback.query_event_recordings
                            if work.events
                            else playback.query_recordings
                        )
                        page = await self._wait_for_work(
                            query(day),
                            work,
                            timeout=self._command_timeout,
                        )
                        if self._media_secrets is not None:
                            self._media_secrets.prefetch(
                                item.uuid
                                for item in page.items
                                if item.encrypt and item.uuid
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
                                    abort(),
                                    timeout=_POOL_CLOSE_TIMEOUT_SECONDS,
                                )
                        else:
                            # Test doubles and alternate transports may not
                            # expose abort. Prevent close() from retrying the
                            # same unresponsive ordered stop operation.
                            playback._playback_started = False
                            with suppress(Exception):
                                await asyncio.wait_for(
                                    playback.close(),
                                    timeout=_POOL_CLOSE_TIMEOUT_SECONDS,
                                )
                        playback = None
                        connection_mode = None
                if failed and playback is not None:
                    # Cancellation or a failed command can leave the camera's
                    # native playback transaction half-open. Do not hand that
                    # transport to the next clip.
                    with suppress(Exception):
                        await playback.close()
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
    ) -> Any:
        """Await one operation while honoring worker close and request cancellation."""
        task = asyncio.create_task(operation)
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        try:
            while True:
                if self._work_cancelled(work):
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

    def _work_cancelled(
        self,
        work: _PlaybackWork | _InteractivePlaybackWork | _ThumbnailWork | _CatalogWork,
    ) -> bool:
        cancel_event = work.cancel_event
        return self._closed or bool(cancel_event is not None and cancel_event.is_set())


class NativeRelayRuntimePool:
    """Own one sequential APK-native transport worker per camera."""

    def __init__(
        self,
        app_session: dict[str, Any],
        *,
        media_secrets: NativeMediaSecretStore | None = None,
    ) -> None:
        self._app_session = app_session
        self._media_secrets = media_secrets
        self._workers: dict[str, NativeRelayPlaybackWorker] = {}
        self._lock = threading.Lock()
        self._closed = False

    def session(
        self,
        config: NativeCameraSessionConfig,
        cancel_event: Any,
    ) -> NativeRelayRuntimeSession:
        config = config.validate()
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
                    playback_factory=NativeRelayPlaybackSession,
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
            if self._closed:
                return
            self._closed = True
            workers = tuple(self._workers.values())
            self._workers.clear()
        # Cancel every camera first so one slow transport cannot delay
        # cancellation of the remaining workers.
        for worker in workers:
            worker.close()
        deadline = time.monotonic() + _POOL_CLOSE_TIMEOUT_SECONDS
        for worker in workers:
            worker.wait_closed(max(0.0, deadline - time.monotonic()))


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

    def __post_init__(self) -> None:
        self.config = self.config.validate()
        if not 0 < self.signaling_timeout <= 60:
            raise NativeRelayRuntimeError("Native signaling timeout is invalid")
        if not 0 < self.first_frame_timeout <= 60:
            raise NativeRelayRuntimeError("Native first-frame timeout is invalid")
        if not 0 < self.end_idle_timeout <= 10:
            raise NativeRelayRuntimeError("Native end idle timeout is invalid")

    def close(self) -> None:
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
                playback.connect(media=True), timeout=self.signaling_timeout + 60
            )
            await self._stream_connected(playback, request, event_handler)
        finally:
            with suppress(Exception):
                await playback.close()

    async def _download_thumbnail(self, start: int, end: int) -> bytes:
        playback = self.playback_factory(
            self.config,
            self.app_session,
        )
        try:
            await self._wait_cancelable(
                playback.connect(media=True), timeout=self.signaling_timeout + 60
            )
            return await self._wait_cancelable(
                playback.download_thumbnail(start=start, end=end),
                timeout=self.signaling_timeout,
            )
        finally:
            with suppress(Exception):
                await playback.close()

    async def _stream_connected(
        self,
        playback: Any,
        request: NativePlaybackRequest,
        event_handler: Callable[[dict[str, Any]], None],
    ) -> None:
        """Play one clip over an already connected native transport."""
        playback_secrets: dict[str, str] = {}
        if request.encrypted:
            assert self.media_secrets is not None
            uuid = request.encryption_uuid.strip()
            playback_secrets[uuid] = self.media_secrets.secret(uuid)
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
        playback_secrets, recording_uuid = self._playback_secrets(current)
        receive_task: asyncio.Task[Any] | None = None
        paused = False
        started = False
        generation = 1
        audio_codec_info = None
        loop = asyncio.get_running_loop()
        video_deadline = loop.time() + self.first_frame_timeout
        saw_audio = False
        saw_video = False
        deferred_command: tuple[str, NativePlaybackRequest | None] | None = None
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
            sink.started({"transport": "tuya-imm-simple", "generation": 1})
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
                        if not saw_video:
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
                    playback_secrets, recording_uuid = self._playback_secrets(current)
                    generation += 1
                    audio_codec_info = None
                    saw_audio = False
                    saw_video = False
                    video_deadline = loop.time() + self.first_frame_timeout
                    await self._wait_cancelable(
                        playback.seek(
                            start=current.start,
                            end=current.end,
                            play_time=current.position,
                            encrypted=current.encrypted,
                            fragments_json=current.fragments_json,
                            play_mode_supported=current.play_mode_supported,
                        ),
                        timeout=self.signaling_timeout + 15,
                    )
                    sink.started(
                        {
                            "transport": "tuya-imm-simple",
                            "generation": generation,
                        }
                    )
                    paused = False
                    continue
                if paused:
                    await asyncio.sleep(0.05)
                    continue
                if not saw_video and loop.time() >= video_deadline:
                    media_seen = "audio but no video" if saw_audio else "no media"
                    raise NativeRelayRuntimeError(
                        f"Camera returned {media_seen} before the playback deadline"
                    )
                if receive_task is None:
                    receive_task = asyncio.create_task(playback.receive_frame())
                done, _ = await asyncio.wait({receive_task}, timeout=0.05)
                if receive_task not in done:
                    continue
                frame = receive_task.result()
                receive_task = None
                if frame.is_audio:
                    saw_audio = True
                    if frame.codec_info is not None:
                        audio_codec_info = frame.codec_info
                    elif audio_codec_info is not None:
                        frame = replace(frame, codec_info=audio_codec_info)
                else:
                    saw_video = True
                converted = self._convert_frame(
                    frame,
                    playback_secrets,
                    recording_uuid,
                )
                sink.frame(converted)
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
        sink.finished({"reason": "user-stop"})

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
        return playback_stream_frame(decrypted)

    async def _recordings_for_day(self, day: date) -> list[dict[str, Any]]:
        results = await self._recordings_for_days([day])
        return results[0][1]

    async def _event_recordings_for_day(self, day: date) -> list[dict[str, Any]]:
        playback = self.playback_factory(self.config, self.app_session)
        try:
            await asyncio.wait_for(
                playback.connect(media=True), timeout=self.signaling_timeout + 60
            )
            try:
                page = await asyncio.wait_for(
                    playback.query_event_recordings(day),
                    timeout=self.signaling_timeout,
                )
            except TimeoutError as err:
                raise NativeRelayRuntimeError(
                    f"Native event catalog timed out ({playback.transport_diagnostics})"
                ) from err
            if self.media_secrets is not None:
                self.media_secrets.prefetch(
                    item.uuid for item in page.items if item.encrypt and item.uuid
                )
            return [item.as_recording() for item in page.items]
        finally:
            with suppress(Exception):
                await playback.close()

    async def _recordings_for_days(
        self, days: list[date]
    ) -> list[tuple[date, list[dict[str, Any]]]]:
        playback = self.playback_factory(
            self.config,
            self.app_session,
        )
        try:
            await asyncio.wait_for(
                playback.connect(media=True), timeout=self.signaling_timeout + 60
            )
            results: list[tuple[date, list[dict[str, Any]]]] = []
            for day in days:
                if self._cancelled():
                    raise NativeRelayRuntimeError("Native catalog was cancelled")
                try:
                    page = await asyncio.wait_for(
                        playback.query_recordings(day),
                        timeout=self.signaling_timeout,
                    )
                except TimeoutError as err:
                    raise NativeRelayRuntimeError(
                        f"Native catalog timed out ({playback.transport_diagnostics})"
                    ) from err
                if self.media_secrets is not None:
                    self.media_secrets.prefetch(
                        item.uuid for item in page.items if item.encrypt and item.uuid
                    )
                results.append((day, [item.as_recording() for item in page.items]))
            return results
        finally:
            with suppress(Exception):
                await playback.close()

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
            try:
                frame = decrypt_playback_frame(
                    frame,
                    playback_secrets,
                    recording_uuid=recording_uuid,
                )
            except NativeMediaCryptoError as err:
                raise NativeRelayRuntimeError(
                    f"Native playback media decryption failed: {err}"
                ) from err
            converted = playback_stream_frame(frame)
            if media_deadline is None:
                media_deadline = loop.time() + max(float(duration), 0.1)
            info = converted.info or {}
            if info.get("avChannel") == 1:
                saw_audio = True
            else:
                saw_video = True
            sink.frame(converted)

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
