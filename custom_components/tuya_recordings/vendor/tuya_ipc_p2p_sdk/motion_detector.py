"""
Motion read out of the frames the camera already sends.

These cameras report no motion of their own. A JPEG is only as large as its
content is complex, so how much each frame differs in size from the one before
it is a direct measure of how much the scene changed — and it costs nothing,
because the frames arrive either way.

Measured against a still scene on a real camera, consecutive frames differ by
0.2% in daylight and 0.41% at night, where sensor noise is higher. The typical
difference is tracked continuously rather than fixed, so the threshold follows
the camera from day into night.

It is an indirect signal, and it cannot tell a cat from the lights coming on.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

# How quickly the typical difference follows the scene. Slow enough that a
# burst of motion does not teach the detector that motion is normal.
_TYPICAL_SMOOTHING = 0.05

# The floor under the threshold: without it, a scene compressing to almost
# identical frames would report motion on the faintest noise.
_MINIMUM_DIFFERENCE = 0.015

# Motion stays on this long after the last frame that triggered it, so an
# automation covers the whole event rather than flickering through it.
DEFAULT_MOTION_HOLD_SECONDS = 12.0

# A frame has to differ by this multiple of the typical difference to count as
# motion. Over a still scene at night — the noisiest case — frames differ by
# 0.41% at the median against a typical difference of about 0.5%, so six times
# typical sits above everything that scene produced but one isolated outlier,
# which the confirmation requirement below then discards.
DEFAULT_SENSITIVITY = 6.0

# Frames collected before the typical difference means anything.
_WARMUP_FRAMES = 8

# How many frames in a row have to exceed the threshold. Anything actually
# moving stays in shot longer than one frame at this camera's rate, while the
# camera's own exposure adjustments show up as a single frame that differs
# sharply from both its neighbours.
_CONFIRMING_FRAMES = 2


class MotionDetector:
    """Turns a stream of frame sizes into a held motion state."""

    def __init__(
        self,
        on_motion: Callable[[bool], None],
        sensitivity: float = DEFAULT_SENSITIVITY,
        hold_seconds: float = DEFAULT_MOTION_HOLD_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the detector to the callback that publishes its state."""
        self._on_motion = on_motion
        self._sensitivity = sensitivity
        self._hold_seconds = hold_seconds
        self._clock = clock
        self._previous_size = 0
        self._typical_difference = 0.0
        self._frames = 0
        self._exceeded = 0
        self._motion_until = 0.0
        self._detected = False
        self._last_difference = 0.0

    @property
    def detected(self) -> bool:
        """Whether motion is currently being reported."""
        return self._detected

    @property
    def last_difference_percent(self) -> float:
        """What the most recent frame differed by, as a percentage."""
        return round(self._last_difference * 100, 2)

    @property
    def typical_difference_percent(self) -> float:
        """What this camera's quiet frames currently look like, as a percentage."""
        return round(self._typical_difference * 100, 2)

    def sample(self, frame_size: int) -> None:
        """Feed the size of one frame the camera sent."""
        previous = self._previous_size
        self._previous_size = frame_size
        if not previous or not frame_size:
            return

        difference = abs(frame_size - previous) / previous
        self._last_difference = difference
        self._frames += 1
        if self._frames <= _WARMUP_FRAMES:
            self._typical_difference += (difference - self._typical_difference) / self._frames
            return

        threshold = max(self._typical_difference * self._sensitivity, _MINIMUM_DIFFERENCE)
        now = self._clock()
        if difference > threshold:
            self._exceeded += 1
            if self._exceeded >= _CONFIRMING_FRAMES:
                self._motion_until = now + self._hold_seconds
        else:
            self._exceeded = 0
            # Only quiet frames teach the detector what quiet looks like.
            self._typical_difference += (difference - self._typical_difference) * _TYPICAL_SMOOTHING

        self._publish(now < self._motion_until)

    def poll(self) -> None:
        """Clear a held motion state whose hold has run out, without a new frame."""
        self._publish(self._clock() < self._motion_until)

    def _publish(self, detected: bool) -> None:
        """Notify the consumer, but only on a change."""
        if detected == self._detected:
            return
        self._detected = detected
        self._on_motion(detected)

    def reset(self) -> None:
        """Forget everything learned and clear any held motion."""
        self._previous_size = 0
        self._typical_difference = 0.0
        self._frames = 0
        self._exceeded = 0
        self._motion_until = 0.0
        self._last_difference = 0.0
        self._publish(False)
