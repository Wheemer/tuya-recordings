"""APK-native playback orchestration.

This module is the narrow bridge between an opened Smart Life native camera
session and Home Assistant media output. It insists on the APK callback path:
fresh catalog lookup, raw encoded video/audio packet callbacks, then
no-transcoding MP4 muxing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .native_frames import (
    NATIVE_PLAYBACK_EVENT_VERSION,
    NativePlaybackFrameType,
    validate_playback_frame_event,
)
from .native_mux import NativePlaybackBrowserStreamMuxer, NativePlaybackMuxer
from .native_session import NativeMediaFormat, NativePlaybackRequest
from .native_thumbnail import NativePlaybackThumbnailExtractor


class NativePlaybackError(RuntimeError):
    """APK-native playback failed after a camera session was available."""


class NativePlaybackUnavailable(NativePlaybackError):
    """APK-native playback cannot safely start for this clip/session."""


@dataclass(slots=True)
class NativePlaybackSessionRunner:
    """Run one native SD-card playback request through a strict packet muxer.

    The playback session itself performs the required fresh catalog query on
    the same native connection immediately before starting media.
    """

    playback_starter: Callable[..., None]
    muxer_factory: Callable[[Path], NativePlaybackMuxer] = NativePlaybackMuxer
    stream_muxer_factory: Callable[[Callable[[bytes], None]], NativePlaybackBrowserStreamMuxer] = (
        NativePlaybackBrowserStreamMuxer
    )
    thumbnail_extractor_factory: Callable[[Path], NativePlaybackThumbnailExtractor] = (
        NativePlaybackThumbnailExtractor
    )

    def receive_clip(
        self,
        start: int,
        end: int,
        output_path: Path,
        **kwargs: Any,
    ) -> None:
        """Receive one recording as a browser-playable MP4 file."""
        request = native_playback_request(start, end, **kwargs)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.unlink(missing_ok=True)
        with self.muxer_factory(output_path) as muxer:
            audit = NativePlaybackEventAudit(muxer.handle_event)
            self._start_playback(request, audit.handle_event)
            audit.require_audio_video()
        if not looks_like_mp4(output_path):
            raise NativePlaybackError("APK-native playback did not create a media file")

    def stream_clip(
        self,
        start: int,
        end: int,
        chunk_callback: Callable[[bytes], None],
        **kwargs: Any,
    ) -> None:
        """Stream one recording as fragmented MP4 chunks."""
        if not callable(chunk_callback):
            raise NativePlaybackError("APK-native stream playback requires a chunk callback")
        request = native_playback_request(start, end, **kwargs)
        with self.stream_muxer_factory(chunk_callback) as muxer:
            audit = NativePlaybackEventAudit(muxer.handle_event)
            self._start_playback(request, audit.handle_event)
            audit.require_audio_video()

    def receive_thumbnail(
        self,
        start: int,
        end: int,
        output_path: Path,
        **kwargs: Any,
    ) -> None:
        """Decode the first usable video frame, then stop native playback."""
        request = native_playback_request(start, end, **kwargs)
        output_path = Path(output_path)
        with self.thumbnail_extractor_factory(output_path) as extractor:
            try:
                self._start_playback(request, extractor.handle_event)
            except NativePlaybackError as err:
                if not extractor.ready:
                    reason = extractor.failure_reason or str(err)
                    raise NativePlaybackError(
                        f"APK-native thumbnail extraction failed: {reason}"
                    ) from err
            extractor.require_thumbnail()

    def _start_playback(
        self,
        request: NativePlaybackRequest,
        event_handler: Callable[[dict[str, Any]], None],
    ) -> None:
        call_spec = request.call_spec
        if not call_spec.uses_raw_packets:
            raise NativePlaybackUnavailable(
                "APK-native playback must use raw video/audio packet callbacks"
            )
        try:
            self.playback_starter(request, event_handler=event_handler)
        except NativePlaybackError:
            raise
        except Exception as err:
            raise NativePlaybackError(
                f"APK-native playback stream failed: {type(err).__name__}: {err}"
            ) from err


class NativePlaybackEventAudit:
    """Validate and count APK playback callbacks before success is allowed."""

    def __init__(self, forward: Callable[[dict[str, Any]], None]) -> None:
        if not callable(forward):
            raise NativePlaybackError("APK-native playback audit requires an event handler")
        self._forward = forward
        self.started = False
        self.finished = False
        self.video_packets = 0
        self.audio_packets = 0

    def handle_event(self, event: dict[str, Any]) -> None:
        try:
            event = validate_playback_frame_event(
                event, protocol=NATIVE_PLAYBACK_EVENT_VERSION
            )
        except Exception as err:
            raise NativePlaybackError(f"APK-native playback emitted an invalid frame event: {err}") from err
        payload = event["payload"]
        frame_type = NativePlaybackFrameType(payload["type"])
        if frame_type == NativePlaybackFrameType.STARTED:
            self.started = True
        elif frame_type == NativePlaybackFrameType.FINISHED:
            self.finished = True
        elif frame_type == NativePlaybackFrameType.MEDIA_CODEC:
            codec = _codec_name(payload.get("info") or {})
            if codec in {"h264", "avc"}:
                self.video_packets += 1
            elif codec in {
                "aac",
                "mpeg4-generic",
                "g711u",
                "pcmu",
                "pcm_s16le",
            }:
                self.audio_packets += 1
        self._forward(event)

    def require_audio_video(self) -> None:
        if self.video_packets <= 0:
            raise NativePlaybackError("APK-native playback emitted no raw video packets")
        if self.audio_packets <= 0:
            raise NativePlaybackError("APK-native playback emitted no raw audio packets")


def native_playback_request(start: int, end: int, **kwargs: Any) -> NativePlaybackRequest:
    """Build the only playback request shape Tuya Recordings accepts."""
    return NativePlaybackRequest(
        start=start,
        end=end,
        play_time=kwargs.get("play_time"),
        fragments_json=str(kwargs.get("fragments_json") or ""),
        encrypted=bool(kwargs.get("encrypted", False)),
        play_mode_supported=bool(kwargs.get("play_mode_supported", False)),
        media_format=NativeMediaFormat.RAW_PACKETS,
        catalog_day=kwargs.get("catalog_day"),
        encryption_uuid=str(kwargs.get("encryption_uuid") or ""),
    ).validate()


def looks_like_mp4(path: Path) -> bool:
    """Return true when a muxed clip has an MP4 file-type box."""
    try:
        if path.stat().st_size < 12:
            return False
        with path.open("rb") as file:
            header = file.read(12)
    except OSError:
        return False
    return header[4:8] == b"ftyp"


def _codec_name(info: dict[str, Any]) -> str:
    codec = info.get("codecName") or info.get("codec") or info.get("codecId")
    if isinstance(codec, str):
        return codec.lower()
    if codec in {1, 2, 27, 28}:
        return "h264"
    if codec in {4, 86018, 86019, 10}:
        return "aac"
    if codec == 133:
        return "g711u"
    if codec == 0xFFFE:
        return "pcm_s16le"
    return ""
