"""Keeps one camera streaming, and fans its frames out to whoever is watching."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

from .const import LOGGER
from .exceptions import TuyaIpcP2pDeviceBusyError
from .motion_detector import DEFAULT_SENSITIVITY, MotionDetector
from .stream_session import StreamSession

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from .client import TuyaIpcP2pClient

DEFAULT_RETRY_MIN_SECONDS = 3.0
DEFAULT_RETRY_MAX_SECONDS = 60.0
DEFAULT_SESSION_COOLDOWN_SECONDS = 5.0

# A busy reply on its own is ordinary. This many in a row, with nothing
# streaming in between, is the device having stopped answering offers
# altogether — a state it enters after a dozen back-to-back attempts and stays
# in until it is power cycled, for the vendor app as much as for this client.
DEFAULT_BUSY_REFUSAL_LIMIT = 10
# Once it is in that state, offering every minute is what keeps it there.
DEFAULT_REFUSED_RETRY_SECONDS = 900.0

# A session that has streamed goes quiet between frames, and this hardware's
# cadence is a couple of frames a second, so a minute of silence is a stall
# rather than a slow scene.
DEFAULT_STALL_TIMEOUT_SECONDS = 60.0

_STALL_CHECK_INTERVAL_SECONDS = 5.0
_SUBSCRIBER_QUEUE_SIZE = 2


class CameraStream:
    """
    One camera's supervised session, plus the frames it produces.

    Every attempt fetches a fresh config — the session, its media key and the
    relay token are minted per fetch, and a camera does not answer an offer
    built from a stale one — runs one session, and restarts after a backoff.
    Cameras serve one client at a time, so while this is running the vendor app
    cannot connect, and vice versa.
    """

    def __init__(  # noqa: PLR0913, PLR0917 -- one knob per timing the device imposes
        self,
        client: TuyaIpcP2pClient,
        device_id: str,
        local_key: str,
        motion_sensitivity: float = DEFAULT_SENSITIVITY,
        stall_timeout_seconds: float = DEFAULT_STALL_TIMEOUT_SECONDS,
        retry_min_seconds: float = DEFAULT_RETRY_MIN_SECONDS,
        retry_max_seconds: float = DEFAULT_RETRY_MAX_SECONDS,
        session_cooldown_seconds: float = DEFAULT_SESSION_COOLDOWN_SECONDS,
        busy_refusal_limit: int = DEFAULT_BUSY_REFUSAL_LIMIT,
        refused_retry_seconds: float = DEFAULT_REFUSED_RETRY_SECONDS,
    ) -> None:
        """Describe the camera to stream and how patiently to retry it."""
        self._client = client
        self._device_id = device_id
        self._local_key = local_key
        self._stall_timeout = stall_timeout_seconds
        self._retry_min = retry_min_seconds
        self._retry_max = retry_max_seconds
        self._session_cooldown = session_cooldown_seconds
        self._busy_refusal_limit = busy_refusal_limit
        self._refused_retry_seconds = refused_retry_seconds
        self._busy_refusals = 0
        self._motion = MotionDetector(self._on_motion, motion_sensitivity)
        self._subscribers: set[asyncio.Queue[bytes]] = set()
        self._state_listeners: list[Callable[[], None]] = []
        self._last_frame: bytes | None = None
        self._last_frame_at = 0.0
        self._session: StreamSession | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._stall_task: asyncio.Task[None] | None = None
        self._first_frame = asyncio.Event()
        self._stop_requested = asyncio.Event()

    @property
    def device_id(self) -> str:
        """The device this stream belongs to."""
        return self._device_id

    @property
    def running(self) -> bool:
        """Whether the supervisor is up, which is not the same as streaming."""
        return self._supervisor_task is not None

    @property
    def streaming(self) -> bool:
        """Whether a frame arrived recently enough to call the stream live."""
        return (
            self._last_frame_at > 0 and time.monotonic() - self._last_frame_at < self._stall_timeout
        )

    @property
    def needs_power_cycle(self) -> bool:
        """
        Whether the device has stopped answering and only a power cycle helps.

        Nothing this client does brings it back: it answers every offer with
        its busy reply, the vendor app cannot load the picture either, and the
        state survives for as long as the hardware stays powered.
        """
        return self._busy_refusals >= self._busy_refusal_limit

    @property
    def motion_detected(self) -> bool:
        """Whether the frames currently look like something is moving."""
        return self._motion.detected

    @property
    def last_frame(self) -> bytes | None:
        """The most recent JPEG this camera produced, if any."""
        return self._last_frame

    @property
    def viewer_count(self) -> int:
        """How many consumers are currently reading the frame stream."""
        return len(self._subscribers)

    @property
    def local_key(self) -> str:
        """The local key the sessions are built with."""
        return self._local_key

    @local_key.setter
    def local_key(self, value: str) -> None:
        """Adopt a rotated local key; it takes effect on the next session."""
        self._local_key = value

    def add_state_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a callback for streaming and motion changes, and return its remover."""
        self._state_listeners.append(listener)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._state_listeners.remove(listener)

        return remove

    def set_motion_sensitivity(self, sensitivity: float) -> None:
        """Rebuild the detector around a new sensitivity."""
        self._motion.reset()
        self._motion = MotionDetector(self._on_motion, sensitivity)

    async def async_start(self) -> None:
        """Start supervising the camera; the first session comes up in the background."""
        if self._supervisor_task is not None:
            return
        self._stop_requested.clear()
        self._supervisor_task = asyncio.create_task(self._async_supervise())
        self._stall_task = asyncio.create_task(self._async_watch_for_stalls())

    async def async_stop(self) -> None:
        """Stop supervising and release the camera."""
        if self._supervisor_task is None:
            return
        self._stop_requested.set()
        for task in (self._supervisor_task, self._stall_task):
            if task is not None:
                task.cancel()
        for task in (self._supervisor_task, self._stall_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._supervisor_task = None
        self._stall_task = None
        session = self._session
        self._session = None
        if session is not None:
            await session.async_close()
        self._first_frame.clear()
        self._last_frame_at = 0.0
        self._motion.reset()
        self._notify_state()

    async def async_wait_for_frame(self, timeout_seconds: float) -> bytes | None:
        """Wait for the first frame of a cold session, and return it if it arrives."""
        if self._last_frame is not None:
            return self._last_frame
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout_seconds):
                await self._first_frame.wait()
        return self._last_frame

    async def async_frames(self) -> AsyncIterator[bytes]:
        """
        Yield every frame the camera produces, starting with the most recent one.

        The queue is deliberately shallow: a consumer that cannot keep up with
        the camera should skip ahead rather than fall further behind.
        """
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(queue)
        try:
            if self._last_frame is not None:
                yield self._last_frame
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)

    async def _async_supervise(self) -> None:
        """Run one session after another, backing off between failures."""
        backoff = self._retry_min
        while not self._stop_requested.is_set():
            started_at = time.monotonic()
            streamed: bool = False
            try:
                streamed = await self._async_run_one_session()
            except asyncio.CancelledError:
                raise
            except Exception as exception:
                LOGGER.warning(
                    "Failed to stream %s after %.0fs: %s",
                    self._device_id,
                    time.monotonic() - started_at,
                    exception,
                )
                if isinstance(exception, TuyaIpcP2pDeviceBusyError):
                    self._note_busy_refusal()
                else:
                    self._clear_busy_refusals()
            if streamed:
                backoff = self._retry_min
                self._clear_busy_refusals()
            # The device needs a moment to release the session it just closed;
            # reconnecting too fast earns a busy reply on the next offer.
            wait = backoff + self._session_cooldown
            if self.needs_power_cycle:
                wait = self._refused_retry_seconds
            LOGGER.debug("Reconnecting %s in %.0fs", self._device_id, wait)
            await self._async_wait_before_retry(wait)
            backoff = min(backoff * 2, self._retry_max)

    def _note_busy_refusal(self) -> None:
        """Count one busy reply, and report a run of them as the state it is."""
        self._busy_refusals += 1
        if self._busy_refusals != self._busy_refusal_limit:
            return
        LOGGER.warning(
            "Camera %s has answered %s offers in a row as busy; it has stopped "
            "answering and needs to be power cycled",
            self._device_id,
            self._busy_refusals,
        )
        self._notify_state()

    def _clear_busy_refusals(self) -> None:
        """Forget the run of busy replies; the device is answering again."""
        was_stuck = self.needs_power_cycle
        self._busy_refusals = 0
        if was_stuck:
            self._notify_state()

    async def _async_wait_before_retry(self, seconds: float) -> None:
        """Back off, but come back the moment the caller asks the stream to stop."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await self._stop_requested.wait()

    async def _async_run_one_session(self) -> bool:
        """Fetch a fresh config, run one session, and report whether it streamed."""
        config = await self._client.async_stream_config(self._device_id, self._local_key)
        identity = await self._client.async_mqtt_identity()
        uid = await self._client.async_uid()
        session = StreamSession(config, identity, uid, self._on_frame)
        self._session = session
        try:
            await session.async_start()
            self._notify_state()
            reason = await session.async_wait_closed()
            LOGGER.debug(
                "Session for %s ended after %s frames: %s",
                self._device_id,
                session.frame_count,
                reason,
            )
            return session.frame_count > 0
        finally:
            self._session = None
            self._last_frame_at = 0.0
            self._first_frame.clear()
            self._motion.reset()
            self._notify_state()
            await session.async_close()

    async def _async_watch_for_stalls(self) -> None:
        """
        Tear a stalled session down.

        A stalled stream keeps the relay connection alive, so nothing errors on
        its own: the frame cadence is the only thing that says the device
        stopped sending.
        """
        try:
            while not self._stop_requested.is_set():
                await asyncio.sleep(_STALL_CHECK_INTERVAL_SECONDS)
                self._motion.poll()
                session = self._session
                if session is None or self._last_frame_at == 0.0:
                    continue
                if time.monotonic() - self._last_frame_at < self._stall_timeout:
                    continue
                LOGGER.info(
                    "No frame from %s for %.0fs, restarting the session",
                    self._device_id,
                    self._stall_timeout,
                )
                await session.async_close()
        except asyncio.CancelledError:
            raise
        except Exception as exception:
            LOGGER.debug("Stall watcher for %s stopped: %s", self._device_id, exception)

    def _on_frame(self, frame: bytes) -> None:
        """Publish one frame to every consumer and feed the motion detector."""
        was_streaming = self.streaming
        self._last_frame = frame
        self._last_frame_at = time.monotonic()
        self._motion.sample(len(frame))
        for queue in self._subscribers:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(frame)
        if not self._first_frame.is_set():
            self._first_frame.set()
        if not was_streaming:
            self._notify_state()

    def _on_motion(self, detected: bool) -> None:
        """Publish a motion change."""
        LOGGER.debug("Motion %s on %s", "detected" if detected else "cleared", self._device_id)
        self._notify_state()

    def _notify_state(self) -> None:
        """Tell every listener that something they display has changed."""
        for listener in list(self._state_listeners):
            try:
                listener()
            except Exception as exception:
                LOGGER.debug("A stream state listener failed: %s", exception)

    async def async_close(self) -> None:
        """Stop the stream and drop every subscriber."""
        await self.async_stop()
        self._subscribers.clear()
        self._state_listeners.clear()
