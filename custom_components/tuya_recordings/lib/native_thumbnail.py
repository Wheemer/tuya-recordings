"""Extract one JPEG from the APK-native SD-card playback packet stream."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from .native_frames import (
    NATIVE_PLAYBACK_EVENT_VERSION,
    NativePlaybackFrameType,
    validate_playback_frame_event,
)
from .native_mux import NativeMuxError, _codec_name, _decode_payload


class NativeThumbnailReady(RuntimeError):
    """Stop the camera stream after its first decodable video frame."""


@dataclass(slots=True)
class NativePlaybackThumbnailExtractor:
    """Decode one JPEG from a bounded prefix of raw H.264 playback packets."""

    output_path: Path
    process_module: Any = subprocess
    protocol: int = NATIVE_PLAYBACK_EVENT_VERSION
    max_video_bytes: int = 4 * 1024 * 1024
    max_video_packets: int = 30
    max_attempts: int = 4
    retry_packet_interval: int = 3
    _video: bytearray = field(default_factory=bytearray, init=False, repr=False)
    _video_packets: int = field(default=0, init=False, repr=False)
    _packets_at_last_attempt: int = field(default=0, init=False, repr=False)
    _attempts: int = field(default=0, init=False, repr=False)
    _saw_key_frame: bool = field(default=False, init=False, repr=False)
    ready: bool = field(default=False, init=False)
    failure_reason: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.output_path = Path(self.output_path)
        if not self.output_path.is_absolute():
            raise NativeMuxError("Native thumbnail output path must be absolute")
        if (
            self.max_video_bytes <= 0
            or self.max_video_packets <= 0
            or self.max_attempts <= 0
        ):
            raise NativeMuxError("Native thumbnail extraction limits are invalid")

    def __enter__(self) -> Self:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.unlink(missing_ok=True)
        self._temporary_path.unlink(missing_ok=True)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._temporary_path.unlink(missing_ok=True)
        if not self.ready:
            self.output_path.unlink(missing_ok=True)

    @property
    def _temporary_path(self) -> Path:
        return self.output_path.with_suffix(f"{self.output_path.suffix}.tmp")

    def handle_event(self, event: dict[str, Any]) -> None:
        event = validate_playback_frame_event(event, protocol=self.protocol)
        payload = event["payload"]
        frame_type = NativePlaybackFrameType(payload["type"])
        if frame_type in {
            NativePlaybackFrameType.STARTED,
            NativePlaybackFrameType.FINISHED,
        }:
            return
        if frame_type == NativePlaybackFrameType.ERROR:
            message = payload.get("info", {}).get("message") or "Native playback reported an error"
            self._fail(str(message))
        if frame_type != NativePlaybackFrameType.MEDIA_CODEC:
            self._fail("Native thumbnail extraction requires raw encoded packets")

        info = payload.get("info") or {}
        if _codec_name(info) not in {"h264", "avc"}:
            return
        packet = _decode_payload(payload)
        if not packet:
            return
        self._video.extend(packet)
        self._video_packets += 1
        self._saw_key_frame = self._saw_key_frame or bool(info.get("isKeyFrame"))
        if len(self._video) > self.max_video_bytes or self._video_packets > self.max_video_packets:
            self._fail("Native thumbnail frame was not available within the safety limit")
        if not self._saw_key_frame:
            return
        if (
            self._packets_at_last_attempt
            and self._video_packets - self._packets_at_last_attempt < self.retry_packet_interval
        ):
            return
        self._packets_at_last_attempt = self._video_packets
        self._attempts += 1
        if self._extract_jpeg():
            self.ready = True
            raise NativeThumbnailReady
        if self._attempts >= self.max_attempts:
            self._fail("Native thumbnail frame could not be decoded within the attempt limit")

    def require_thumbnail(self) -> None:
        if not self.ready or not self.output_path.exists():
            reason = (
                "Native playback ended before a thumbnail keyframe was received"
                if not self._saw_key_frame
                else "Native playback ended before a thumbnail could be decoded"
            )
            self._fail(reason)

    def _fail(self, reason: str) -> None:
        self.failure_reason = reason
        raise NativeMuxError(reason)

    def _extract_jpeg(self) -> bool:
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "h264",
            "-i",
            "pipe:0",
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-vf",
            "scale=min(640\\,iw):-2",
            "-q:v",
            "3",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ]
        try:
            result = self.process_module.run(
                command,
                input=bytes(self._video),
                stdout=self.process_module.PIPE,
                stderr=self.process_module.PIPE,
                timeout=3,
                check=False,
            )
        except self.process_module.TimeoutExpired:
            return False
        image = result.stdout if result.returncode == 0 else b""
        if not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9"):
            return False
        self._temporary_path.write_bytes(image)
        self._temporary_path.replace(self.output_path)
        return True
