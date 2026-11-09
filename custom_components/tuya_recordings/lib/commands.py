"""One bounded queue for camera work, with waiting playback taking priority."""

import threading
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar


class CameraWorkCancelled(RuntimeError):
    """The caller cancelled before camera work could run."""


class CameraWorkBusy(RuntimeError):
    """The bounded camera queue cannot accept or start this operation."""


class Cancellation:
    def __init__(self, *events):
        self.events = tuple(event for event in events if event is not None)

    def is_set(self):
        return any(event.is_set() for event in self.events)


class Deadline:
    def __init__(self, timeout: float):
        self.expires = time.monotonic() + timeout

    def is_set(self):
        return time.monotonic() >= self.expires


_WORK_CANCEL = ContextVar("tuya_camera_work_cancel", default=None)
ON_DEMAND_DAY_CATALOG = ContextVar("tuya_on_demand_day_catalog", default=False)
ON_DEMAND_OFFER_RETRIES = ContextVar("tuya_on_demand_offer_retries", default=False)
MANUAL_REFRESH_ID = ContextVar("tuya_manual_refresh_id", default="-")


@contextmanager
def on_demand_offer_retries():
    """Opt a foreground request into same-session MQTT offer resends."""
    token = ON_DEMAND_OFFER_RETRIES.set(True)
    try:
        yield
    finally:
        ON_DEMAND_OFFER_RETRIES.reset(token)


@contextmanager
def manual_catalog_refresh():
    """Carry explicit catalog wake permission through the executor and worker."""
    token = ON_DEMAND_DAY_CATALOG.set(True)
    try:
        with on_demand_offer_retries():
            yield
    finally:
        ON_DEMAND_DAY_CATALOG.reset(token)


@contextmanager
def camera_work_scope(cancel_event=None):
    """Carry one cancellation generation through nested API and IPC work."""
    cancellation = Cancellation(_WORK_CANCEL.get(), cancel_event)
    token = _WORK_CANCEL.set(cancellation)
    try:
        yield cancellation
    finally:
        _WORK_CANCEL.reset(token)


class CameraCommandQueue:
    def __init__(self, capacity: int = 8, wait_timeout: float = 30) -> None:
        self._condition = threading.Condition()
        self._waiting = deque()
        self._owner = None
        self._owner_cancel: threading.Event | None = None
        self._owner_playback = False
        self._unsafe_transports = set()
        self._wake_after: dict[str, float] = {}
        self._wake_report_missing: set[str] = set()
        self.capacity = capacity
        self.wait_timeout = wait_timeout

    def quarantine(self, owner):
        """Block new work while an owner's transport teardown is unproven."""
        with self._condition:
            self._unsafe_transports.add(owner)
            self._condition.notify_all()

    def clear_quarantine(self, owner):
        """Release the interlock only after that owner's transport is closed."""
        with self._condition:
            self._unsafe_transports.discard(owner)
            self._condition.notify_all()

    def playback_waiting(self) -> bool:
        with self._condition:
            return any(urgent for _, urgent in self._waiting)

    def reserve_wake(self, device_id: str) -> bool:
        """Allow at most one wake per device per minute across runtimes."""
        with self._condition:
            now = time.monotonic()
            self._wake_after = {key: until for key, until in self._wake_after.items() if until > now}
            self._wake_report_missing.intersection_update(self._wake_after)
            if device_id in self._wake_after:
                return False
            self._wake_after[device_id] = now + 60
            return True

    def mark_wake_report_missing(self, device_id: str) -> None:
        """Tie an unanswered status request to its existing wake cooldown."""
        with self._condition:
            if device_id in self._wake_after:
                self._wake_report_missing.add(device_id)

    def wake_delay(self, device_id: str) -> float:
        """Remaining shared wake reservation, without extending it."""
        with self._condition:
            return max(0.0, self._wake_after.get(device_id, 0) - time.monotonic())

    def wake_report_on_cooldown(self, device_id: str) -> bool:
        with self._condition:
            return (
                device_id in self._wake_report_missing
                and self._wake_after.get(device_id, 0) > time.monotonic()
            )

    @contextmanager
    def slot(self, cancel_event=None, *, allow_reentry=True, playback=False):
        interrupt = threading.Event()
        cancel_event = Cancellation(_WORK_CANCEL.get(), cancel_event, interrupt)
        identity = threading.get_ident()
        ticket = object()
        nested = False
        deadline = time.monotonic() + self.wait_timeout
        with self._condition:
            if self._unsafe_transports:
                raise CameraWorkBusy("Camera work is blocked until failed transport cleanup completes")
            if cancel_event is not None and cancel_event.is_set():
                raise CameraWorkCancelled("Camera operation cancelled")
            if self._owner == identity:
                if not allow_reentry:
                    raise CameraWorkBusy("Camera operation already active")
                nested = True
            else:
                if len(self._waiting) >= self.capacity:
                    raise CameraWorkBusy("Camera command queue is full")
                # Playback owns the camera. Interrupt an active background
                # operation so its native request closes before playback starts.
                if (
                    playback
                    and not self._owner_playback
                    and self._owner_cancel is not None
                ):
                    self._owner_cancel.set()
                position = len(self._waiting)
                if playback:
                    position = next(
                        (
                            index
                            for index, (_, urgent) in enumerate(self._waiting)
                            if not urgent
                        ),
                        position,
                    )
                item = (ticket, playback)
                self._waiting.insert(position, item)
                try:
                    while True:
                        if self._unsafe_transports:
                            raise CameraWorkBusy("Camera work is blocked until failed transport cleanup completes")
                        if cancel_event is not None and cancel_event.is_set():
                            raise CameraWorkCancelled("Camera operation cancelled")
                        if self._owner is None and self._waiting[0][0] is ticket:
                            self._owner = identity
                            self._owner_cancel = interrupt
                            self._owner_playback = playback
                            self._waiting.popleft()
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise CameraWorkBusy("Camera command queue wait timed out")
                        self._condition.wait(min(0.1, remaining))
                except BaseException:
                    self._waiting.remove(item)
                    self._condition.notify_all()
                    raise
        try:
            yield cancel_event
        finally:
            if not nested:
                with self._condition:
                    self._owner = None
                    self._owner_cancel = None
                    self._owner_playback = False
                    self._condition.notify_all()


# Shared by every integration entry, cloud request, and IPC session in HA.
CAMERA_COMMANDS = CameraCommandQueue()
