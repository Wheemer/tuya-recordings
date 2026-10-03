"""One bounded queue for camera work, with waiting playback taking priority."""

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time


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
        self.capacity = capacity
        self.wait_timeout = wait_timeout

    def playback_waiting(self) -> bool:
        with self._condition:
            return any(urgent for _, urgent in self._waiting)

    @contextmanager
    def slot(self, cancel_event=None, *, allow_reentry=True, playback=False):
        cancel_event = Cancellation(_WORK_CANCEL.get(), cancel_event)
        identity = threading.get_ident()
        ticket = object()
        nested = False
        deadline = time.monotonic() + self.wait_timeout
        with self._condition:
            if cancel_event is not None and cancel_event.is_set():
                raise CameraWorkCancelled("Camera operation cancelled")
            if self._owner == identity:
                if not allow_reentry:
                    raise CameraWorkBusy("Camera operation already active")
                nested = True
            else:
                if len(self._waiting) >= self.capacity:
                    raise CameraWorkBusy("Camera command queue is full")
                # FIFO within each class; never interrupt the active operation.
                position = len(self._waiting)
                if playback:
                    position = next(
                        (index for index, (_, urgent) in enumerate(self._waiting) if not urgent),
                        position,
                    )
                item = (ticket, playback)
                self._waiting.insert(position, item)
                try:
                    while True:
                        if cancel_event is not None and cancel_event.is_set():
                            raise CameraWorkCancelled("Camera operation cancelled")
                        if self._owner is None and self._waiting[0][0] is ticket:
                            self._owner = identity
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
                    self._condition.notify_all()


# Shared by every integration entry, cloud request, and IPC session in HA.
CAMERA_COMMANDS = CameraCommandQueue()
