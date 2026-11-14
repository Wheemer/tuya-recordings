"""Mux APK-native playback frame events into browser-playable MP4 files."""

from __future__ import annotations

import base64
import binascii
import os
import struct
import subprocess
import threading
import select
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from .native_frames import (
    NATIVE_PLAYBACK_EVENT_VERSION,
    NativePlaybackFrameType,
    validate_playback_frame_event,
)
from .native_packet_cache import (
    MAX_PACKET_BYTES, PACKET_HEADER, NativePresentationClock, TimestampedVideoMuxer,
    TimestampedAacMuxer, mux_camera_packets,
)


class NativeMuxError(RuntimeError):
    """Native playback events could not be converted into a media file."""


class _StderrTail:
    """Drain process diagnostics continuously, retaining only a bounded tail."""

    def __init__(self, stream):
        self.data = bytearray()
        self.stream = stream
        self.thread = threading.Thread(target=self._read, name="tuya-mux-stderr", daemon=True)
        self.thread.start()

    def _read(self):
        try:
            while self.stream is not None and (chunk := self.stream.read(4096)):
                self.data.extend(chunk)
                del self.data[:-65536]
        except (OSError, ValueError):
            return

    def finish(self):
        self.thread.join(timeout=2)
        return bytes(self.data)


def _write_pipe(handle, data):
    try:
        fd = handle.fileno()
    except (AttributeError, OSError, ValueError):
        handle.write(data)
        return
    os.set_blocking(fd, False)
    pending = memoryview(data)
    deadline = time.monotonic() + 5
    while pending:
        if time.monotonic() >= deadline:
            raise NativeMuxError("ffmpeg input pipe stopped accepting data")
        if not select.select([], [fd], [], 0.1)[1]:
            continue
        try:
            written = os.write(fd, pending[:65536])
        except BlockingIOError:
            continue
        pending = pending[written:]


# A segment header is emitted before each independently seekable browser
# sequence. The generation and requested camera time let the browser reject
# residual frames from a superseded seek before they reach MediaSource/audio.
BROWSER_STREAM_MAGIC = b"TRS3"
BROWSER_STREAM_VIDEO = 1
BROWSER_STREAM_AUDIO_PCM_S16LE = 2
BROWSER_STREAM_END = 3
BROWSER_STREAM_AUDIO_G711U = 4
BROWSER_STREAM_AUDIO_FORMAT = 5
BROWSER_STREAM_AUDIO_G711A = 6
BROWSER_STREAM_AUDIO_AAC_MP4 = 7
BROWSER_STREAM_VIDEO_FORMAT = 8
BROWSER_STREAM_CAMERA_CLOCK = 9
_BROWSER_STREAM_HEADER = struct.Struct(">BQI")
_BROWSER_STREAM_SEGMENT = struct.Struct(">IQ")
_BROWSER_STREAM_AUDIO_FORMAT = struct.Struct(">IB")
_PIPE_READ_BYTES = 64 * 1024


def browser_stream_header(generation: int = 0, position: int = 0) -> bytes:
    """Return the versioned header for one browser media sequence."""
    if type(generation) is not int or generation < 0:
        raise NativeMuxError("Native browser stream generation is invalid")
    if type(position) is not int or position < 0:
        raise NativeMuxError("Native browser stream position is invalid")
    return BROWSER_STREAM_MAGIC + _BROWSER_STREAM_SEGMENT.pack(generation, position)


def _read_pipe_chunk(stream: Any) -> bytes:
    """Read whatever a pipe currently provides without filling a 64 KiB buffer."""
    read1 = getattr(stream, "read1", None)
    if callable(read1):
        return read1(_PIPE_READ_BYTES)
    return stream.read(_PIPE_READ_BYTES)


@dataclass(slots=True)
class NativePlaybackMuxer:
    """Collect raw APK media callback packets and remux them without transcoding."""

    output_path: Path
    require_audio: bool = True
    protocol: int = NATIVE_PLAYBACK_EVENT_VERSION
    _clock: NativePresentationClock = field(default_factory=NativePresentationClock, init=False, repr=False)
    _video_path: Path = field(init=False, repr=False)
    _audio_path: Path = field(init=False, repr=False)
    _temp_path: Path = field(init=False, repr=False)
    _packet_path: Path = field(init=False, repr=False)
    _audio_format: tuple[str, int, int] = field(default=("", 0, 0), init=False, repr=False)
    _origin: Fraction | None = field(default=None, init=False, repr=False)
    _video_bytes: int = field(default=0, init=False, repr=False)
    _audio_bytes: int = field(default=0, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _finished: bool = field(default=False, init=False, repr=False)
    _pending_video_headers: bytes = field(default=b"", init=False, repr=False)

    def __post_init__(self) -> None:
        self.output_path = Path(self.output_path)
        if not self.output_path.is_absolute():
            raise NativeMuxError("Native mux output path must be absolute")
        self._video_path = self.output_path.with_suffix(".video.h264")
        self._audio_path = self.output_path.with_suffix(".audio.aac")
        self._temp_path = self.output_path.with_suffix(".mux.tmp.mp4")
        self._packet_path = self.output_path.with_suffix(".packets.tmp")
        self._video_bytes = 0
        self._audio_bytes = 0
        self._audio_format = ("", 0, 0)
        self._origin = None
        self._started = False
        self._finished = False

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            try:
                self.finish()
            except Exception:
                self.cleanup()
                raise
        else:
            self.cleanup()

    def open(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.cleanup()
        self._started = False
        self._finished = False
        self._video_bytes = 0
        self._audio_bytes = 0
        self._audio_format = ("", 0, 0)
        self._origin = None

    def handle_event(self, event: dict[str, Any]) -> None:
        event = validate_playback_frame_event(event, protocol=self.protocol)
        payload = event["payload"]
        frame_type = NativePlaybackFrameType(payload["type"])
        if frame_type == NativePlaybackFrameType.STARTED:
            self._started = True
            return
        if frame_type == NativePlaybackFrameType.FINISHED:
            self._finished = True
            return
        if frame_type == NativePlaybackFrameType.ERROR:
            message = payload.get("info", {}).get("message") or "Native playback session reported an error"
            raise NativeMuxError(str(message))
        data = _decode_payload(payload)
        info = payload.get("info") or {}
        if frame_type == NativePlaybackFrameType.MEDIA_CODEC:
            self._write_media_codec_packet(data, info)
            return
        if frame_type in {NativePlaybackFrameType.VIDEO_YUV, NativePlaybackFrameType.AUDIO_PCM}:
            raise NativeMuxError(
                "Native session emitted decoded frames; cached playback requires raw packets for no-transcoding MP4"
            )
        raise NativeMuxError(f"Unsupported native playback frame type: {frame_type.value}")

    def finish(self) -> None:
        if self._video_bytes <= 0:
            raise NativeMuxError("Native playback emitted no video packets")
        if self.require_audio and self._audio_bytes <= 0:
            raise NativeMuxError("Native playback emitted no audio packets")
        assert self._origin is not None
        codec, rate, channels = self._audio_format
        try:
            mux_camera_packets(self._video_path, self._audio_path, self._packet_path,
                self._temp_path, audio_codec=codec, sample_rate=rate, channels=channels,
                origin=self._origin)
        except Exception as err:
            raise NativeMuxError(f"Native packet remux failed: {err}") from err
        self._temp_path.replace(self.output_path)
        if not self.output_path.exists() or self.output_path.stat().st_size <= 0:
            raise NativeMuxError("Native mux did not create a playable media file")
        self._cleanup_intermediates()

    def cleanup(self) -> None:
        self._pending_video_headers = b""
        for path in (self._video_path, self._audio_path, self._packet_path, self._temp_path, self.output_path):
            path.unlink(missing_ok=True)

    def _write_media_codec_packet(self, data: bytes, info: dict[str, Any]) -> None:
        codec = _codec_name(info)
        if codec in {"h264", "avc"}:
            if len(self._pending_video_headers) + len(data) > MAX_PACKET_BYTES:
                raise NativeMuxError("Native cached video headers exceed packet limit")
            _append_bytes(self._video_path, data)
            if _h264_metadata_only(data):
                self._pending_video_headers += data
                return
            data = self._pending_video_headers + data
            self._pending_video_headers = b""
            self._video_bytes += len(data)
            self._record_packet(1, data, info)
            return
        if codec in {"aac", "mpeg4-generic", "pcm_s16le", "g711u", "g711a"}:
            if codec in {"aac", "mpeg4-generic"}:
                codec = "aac"
                data = _aac_with_adts(data, info)
            rate, channels = info.get("sampleRate"), info.get("channels")
            if type(rate) is not int or not 0 < rate <= 192000 or channels not in {1, 2}:
                raise NativeMuxError("Native cached audio requires valid sampleRate and channels")
            audio_format = (codec, rate, channels)
            if self._audio_format[0] and self._audio_format != audio_format:
                raise NativeMuxError("Camera changed audio format during cached recording")
            self._audio_format = audio_format
            _append_bytes(self._audio_path, data)
            self._audio_bytes += len(data)
            self._record_packet(2, data, info)
            return
        raise NativeMuxError(f"Unsupported native raw packet codec: {codec or 'unknown'}")

    def _record_packet(self, kind: int, data: bytes, info: dict[str, Any]) -> None:
        info = _presentation_info(self._clock, kind, data, info)
        timestamp, scale = info.get("timestamp"), info.get("timestampScale", 1000)
        if type(timestamp) is not int or not 0 <= timestamp < 1 << 64 or type(scale) is not int or not 0 < scale <= 1_000_000:
            raise NativeMuxError("Native cached packet has invalid camera timing")
        if not data:
            raise NativeMuxError("Native cached packet is empty")
        stamp = Fraction(timestamp, scale)
        self._origin = stamp if self._origin is None else min(self._origin, stamp)
        _append_bytes(self._packet_path, PACKET_HEADER.pack(kind, int(bool(info.get("isKeyFrame"))), timestamp, scale, len(data)) + data)

    def _cleanup_intermediates(self) -> None:
        self._pending_video_headers = b""
        self._video_path.unlink(missing_ok=True)
        self._audio_path.unlink(missing_ok=True)
        self._temp_path.unlink(missing_ok=True)
        self._packet_path.unlink(missing_ok=True)


def _decode_payload(payload: dict[str, Any]) -> bytes:
    try:
        return base64.b64decode(payload.get("payloadBase64") or "", validate=True)
    except (binascii.Error, ValueError) as err:
        raise NativeMuxError("Native media packet payload is invalid base64") from err


def _codec_name(info: dict[str, Any]) -> str:
    codec = info.get("codecName") or info.get("codec") or info.get("codecId")
    if isinstance(codec, str):
        return codec.lower()
    if codec in {1, 27}:
        return "h264"
    if codec in {4, 86018}:
        return "aac"
    if codec == 133:
        return "g711u"
    if codec == 132:
        return "g711a"
    if codec == 0xFFFE:
        return "pcm_s16le"
    return ""


def _append_bytes(path: Path, data: bytes) -> None:
    if not data:
        return
    with path.open("ab") as handle:
        handle.write(data)


def _aac_with_adts(payload: bytes, info: dict[str, Any]) -> bytes:
    if _has_adts_header(payload):
        return payload
    sample_rate = info.get("sampleRate")
    channels = info.get("channels")
    if type(sample_rate) is not int or type(channels) is not int:
        raise NativeMuxError("Raw AAC packets require sampleRate and channels to add ADTS framing")
    return _adts_header(len(payload), sample_rate, channels) + payload


def _has_adts_header(payload: bytes) -> bool:
    return len(payload) >= 2 and payload[0] == 0xFF and (payload[1] & 0xF0) == 0xF0


def _adts_header(payload_size: int, sample_rate: int, channels: int) -> bytes:
    if payload_size <= 0:
        raise NativeMuxError("AAC packet is empty")
    try:
        frequency_index = _AAC_SAMPLE_RATES.index(sample_rate)
    except ValueError as err:
        raise NativeMuxError(f"Unsupported AAC sample rate: {sample_rate}") from err
    if not 0 < channels <= 7:
        raise NativeMuxError(f"Unsupported AAC channel count: {channels}")
    profile = 1  # AAC LC, stored as profile minus one in ADTS.
    frame_length = payload_size + 7
    if frame_length >= 1 << 13:
        raise NativeMuxError("AAC packet is too large for ADTS framing")
    return bytes(
        [
            0xFF,
            0xF1,
            ((profile & 0x03) << 6) | ((frequency_index & 0x0F) << 2) | ((channels >> 2) & 0x01),
            ((channels & 0x03) << 6) | ((frame_length >> 11) & 0x03),
            (frame_length >> 3) & 0xFF,
            ((frame_length & 0x07) << 5) | 0x1F,
            0xFC,
        ]
    )


_AAC_SAMPLE_RATES = [
    96000,
    88200,
    64000,
    48000,
    44100,
    32000,
    24000,
    22050,
    16000,
    12000,
    11025,
    8000,
    7350,
]


@dataclass(slots=True)
class NativePlaybackFmp4StreamMuxer:
    """Remux APK playback packets to fragmented MP4 chunks without transcoding."""

    chunk_callback: Any
    require_audio: bool = True
    process_module: Any = subprocess
    pipe_factory: Any = os.pipe
    protocol: int = NATIVE_PLAYBACK_EVENT_VERSION
    _video_handle: Any = field(default=None, init=False, repr=False)
    _audio_handle: Any = field(default=None, init=False, repr=False)
    _proc: Any = field(default=None, init=False, repr=False)
    _reader: threading.Thread | None = field(default=None, init=False, repr=False)
    _video_bytes: int = field(default=0, init=False, repr=False)
    _audio_bytes: int = field(default=0, init=False, repr=False)
    _output_bytes: int = field(default=0, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _finished: bool = field(default=False, init=False, repr=False)
    _reader_error: BaseException | None = field(default=None, init=False, repr=False)
    _stderr: Any = field(default=None, init=False, repr=False)

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            try:
                self.finish()
            except Exception:
                self.cleanup()
                raise
        else:
            self.cleanup()

    def open(self) -> None:
        if not callable(self.chunk_callback):
            raise NativeMuxError("Native stream muxer requires a chunk callback")
        if not callable(self.pipe_factory):
            raise NativeMuxError("Progressive native playback requires POSIX pipes")
        self.cleanup()
        video_read, video_write = self.pipe_factory()
        audio_read, audio_write = self.pipe_factory()
        try:
            self._proc = self.process_module.Popen(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "warning",
                    "-fflags",
                    "+genpts",
                    "-f",
                    "h264",
                    "-i",
                    f"pipe:{video_read}",
                    "-f",
                    "aac",
                    "-i",
                    f"pipe:{audio_read}",
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c",
                    "copy",
                    "-bsf:a",
                    "aac_adtstoasc",
                    "-movflags",
                    "frag_keyframe+empty_moov+default_base_moof",
                    "-f",
                    "mp4",
                    "pipe:1",
                ],
                pass_fds=(video_read, audio_read),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            os.close(video_read)
            os.close(audio_read)
            video_read = audio_read = -1
            self._video_handle = os.fdopen(video_write, "wb", buffering=0)
            video_write = -1
            self._audio_handle = os.fdopen(audio_write, "wb", buffering=0)
            audio_write = -1
        except BaseException:
            for fd in (video_read, video_write, audio_read, audio_write):
                if fd >= 0:
                    with _suppress_mux():
                        os.close(fd)
            self.cleanup()
            raise
        self._reader = threading.Thread(target=self._read_stdout, name="tuya-recordings-fmp4", daemon=True)
        self._stderr = _StderrTail(self._proc.stderr)
        self._reader.start()
        self._video_bytes = 0
        self._audio_bytes = 0
        self._output_bytes = 0
        self._started = False
        self._finished = False
        self._reader_error = None

    def handle_event(self, event: dict[str, Any]) -> None:
        event = validate_playback_frame_event(event, protocol=self.protocol)
        payload = event["payload"]
        frame_type = NativePlaybackFrameType(payload["type"])
        if frame_type == NativePlaybackFrameType.STARTED:
            self._started = True
            return
        if frame_type == NativePlaybackFrameType.FINISHED:
            self._finished = True
            return
        if frame_type == NativePlaybackFrameType.ERROR:
            message = payload.get("info", {}).get("message") or "Native playback session reported an error"
            raise NativeMuxError(str(message))
        data = _decode_payload(payload)
        info = payload.get("info") or {}
        if frame_type != NativePlaybackFrameType.MEDIA_CODEC:
            raise NativeMuxError(
                "Progressive native playback requires raw encoded packets for no-transcoding MP4"
            )
        codec = _codec_name(info)
        if codec in {"h264", "avc"}:
            self._write_video(data)
            return
        if codec in {"aac", "mpeg4-generic"}:
            self._write_audio(_aac_with_adts(data, info))
            return
        raise NativeMuxError(f"Unsupported native raw packet codec: {codec or 'unknown'}")

    def finish(self) -> None:
        if self._video_bytes <= 0:
            raise NativeMuxError("Native playback emitted no video packets")
        if self.require_audio and self._audio_bytes <= 0:
            raise NativeMuxError("Native playback emitted no audio packets")
        self._close_inputs()
        proc = self._proc
        if proc is None:
            raise NativeMuxError("Native stream muxer was not started")
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired as exc:
            proc.terminate()
            with _suppress_mux():
                proc.wait(timeout=5)
            if proc.poll() is None:
                proc.kill()
                with _suppress_mux():
                    proc.wait(timeout=5)
            raise NativeMuxError("ffmpeg did not finish native Tuya recording stream") from exc
        if self._reader is not None:
            self._reader.join(timeout=5)
        if self._reader_error is not None:
            raise NativeMuxError(f"Native stream mux reader failed: {type(self._reader_error).__name__}")
        stderr = self._stderr.finish() if self._stderr is not None else b""
        if proc.returncode != 0:
            raise NativeMuxError(
                "ffmpeg failed to stream native Tuya recording: "
                f"{_decode_process_output(stderr)}"
            )
        if self._output_bytes <= 0:
            raise NativeMuxError("ffmpeg emitted no fragmented MP4 data")
        self.cleanup()

    def cleanup(self) -> None:
        self._close_inputs()
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            with _suppress_mux():
                proc.terminate()
                proc.wait(timeout=3)
            if proc.poll() is None:
                with _suppress_mux():
                    proc.kill()
                    proc.wait(timeout=3)
        if self._reader is not None:
            self._reader.join(timeout=1)
            self._reader = None
        if self._stderr is not None:
            self._stderr.finish()
            self._stderr = None

    def _write_video(self, data: bytes) -> None:
        if not data:
            return
        if self._video_handle is None:
            raise NativeMuxError("Native stream video pipe is not open")
        _write_pipe(self._video_handle, data)
        self._video_bytes += len(data)

    def _write_audio(self, data: bytes) -> None:
        if not data:
            return
        if self._audio_handle is None:
            raise NativeMuxError("Native stream audio pipe is not open")
        _write_pipe(self._audio_handle, data)
        self._audio_bytes += len(data)

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                chunk = _read_pipe_chunk(proc.stdout)
                if not chunk:
                    return
                self._emit_chunk(chunk)
        except Exception as err:  # pragma: no cover - defensive thread boundary
            self._reader_error = err

    def _emit_chunk(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._output_bytes += len(chunk)
        self.chunk_callback(chunk)

    def _close_inputs(self) -> None:
        for attr in ("_video_handle", "_audio_handle"):
            handle = getattr(self, attr)
            if handle is not None:
                with _suppress_mux():
                    handle.close()
                setattr(self, attr, None)


@dataclass(slots=True)
class NativePlaybackBrowserStreamMuxer:
    """Stream copied H264/AAC or browser-decodable PCM/G711 audio."""

    chunk_callback: Any
    process_module: Any = subprocess
    pipe_factory: Any = os.pipe
    protocol: int = NATIVE_PLAYBACK_EVENT_VERSION
    _clock: NativePresentationClock = field(default_factory=NativePresentationClock, init=False, repr=False)
    stream_header: bytes = BROWSER_STREAM_MAGIC + _BROWSER_STREAM_SEGMENT.pack(0, 0)
    _video_handle: Any = field(default=None, init=False, repr=False)
    _proc: Any = field(default=None, init=False, repr=False)
    _reader: threading.Thread | None = field(default=None, init=False, repr=False)
    _video_bytes: int = field(default=0, init=False, repr=False)
    _audio_bytes: int = field(default=0, init=False, repr=False)
    _output_bytes: int = field(default=0, init=False, repr=False)
    _reader_error: BaseException | None = field(default=None, init=False, repr=False)
    _stderr: Any = field(default=None, init=False, repr=False)
    _first_video_timestamp: int = field(default=0, init=False, repr=False)
    _audio_format: tuple[int, int, int] | None = field(default=None, init=False, repr=False)
    _announced: bool = field(default=False, init=False, repr=False)
    _h264_parameter_sets: bytes = field(default=b"", init=False, repr=False)
    _packet_video: Any = field(default=None, init=False, repr=False)
    _video_info: dict = field(default_factory=dict, init=False, repr=False)
    _packet_audio: Any = field(default=None, init=False, repr=False)
    _audio_codec: str = field(default="", init=False, repr=False)
    _first_audio_timestamp: int = field(default=0, init=False, repr=False)
    _first_video_source: Fraction | None = field(default=None, init=False, repr=False)
    _video_source_scale: int | None = field(default=None, init=False, repr=False)
    _video_clock_offset: Fraction | None = field(default=None, init=False, repr=False)
    _camera_clock_anchor: tuple[Fraction, Fraction] = field(default=(Fraction(0), Fraction(0)), init=False, repr=False)

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            try:
                self.finish()
            except Exception:
                self.cleanup()
                raise
        else:
            self.cleanup()

    def open(self) -> None:
        if not callable(self.chunk_callback):
            raise NativeMuxError("Native browser stream requires a chunk callback")
        self.cleanup()
        self._video_bytes = self._audio_bytes = self._output_bytes = 0
        self._first_video_timestamp = 0
        self._audio_format = None
        self._announced = False
        self._h264_parameter_sets = b""
        self._audio_codec = ""

    def _open_legacy_video(self) -> None:
        """Serve callback fixtures/older adapters without a declared clock."""
        if not callable(self.pipe_factory):
            raise NativeMuxError("Native browser stream requires POSIX pipes")
        video_read, video_write = self.pipe_factory()
        try:
            self._proc = self.process_module.Popen(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "warning",
                    "-fflags",
                    "+genpts+nobuffer",
                    "-probesize",
                    "32",
                    "-analyzeduration",
                    "0",
                    "-f",
                    "h264",
                    "-i",
                    f"pipe:{video_read}",
                    "-map",
                    "0:v:0",
                    "-c:v",
                    "copy",
                    "-movflags",
                    "frag_keyframe+empty_moov+default_base_moof+dash",
                    "-flush_packets",
                    "1",
                    "-f",
                    "mp4",
                    "pipe:1",
                ],
                pass_fds=(video_read,),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            os.close(video_read)
            video_read = -1
            self._video_handle = os.fdopen(video_write, "wb", buffering=0)
            video_write = -1
        except BaseException:
            for fd in (video_read, video_write):
                if fd >= 0:
                    with _suppress_mux():
                        os.close(fd)
            self.cleanup()
            raise
        self._reader_error = None
        self._reader = threading.Thread(
            target=self._read_stdout,
            name="tuya-recordings-browser-stream",
            daemon=True,
        )
        self._stderr = _StderrTail(self._proc.stderr)
        self._reader.start()

    def handle_event(self, event: dict[str, Any]) -> bool | None:
        event = validate_playback_frame_event(event, protocol=self.protocol)
        payload = event["payload"]
        frame_type = NativePlaybackFrameType(payload["type"])
        if frame_type == NativePlaybackFrameType.STARTED:
            if not self._announced:
                self.chunk_callback(self.stream_header)
                self._announced = True
            return
        if frame_type == NativePlaybackFrameType.FINISHED:
            return
        if frame_type == NativePlaybackFrameType.ERROR:
            message = payload.get("info", {}).get("message") or "Native playback session reported an error"
            raise NativeMuxError(str(message))
        if frame_type != NativePlaybackFrameType.MEDIA_CODEC:
            raise NativeMuxError("Native browser stream requires raw encoded packets")
        data = _decode_payload(payload)
        info = payload.get("info") or {}
        codec = _codec_name(info)
        if codec in {"h264", "avc"}:
            if not self._video_bytes:
                parameter_sets = _h264_parameter_sets(data)
                if parameter_sets:
                    self._h264_parameter_sets = _merge_h264_parameter_sets(
                        self._h264_parameter_sets,
                        parameter_sets,
                    )
                # A new browser segment needs a complete decoder configuration
                # and an IDR access unit. Starting FFmpeg on an SPS-only or
                # inter-frame packet yields an apparently valid fMP4 stream
                # that can play audio while Chromium never renders video.
                if not _has_h264_decoder_config(self._h264_parameter_sets) or not _has_h264_idr(data):
                    return False
                if not parameter_sets:
                    data = self._h264_parameter_sets + data
            if "timestampScale" in info:
                source_info = info
                info = _presentation_info(self._clock, 1, data, info)
                source = Fraction(source_info["timestamp"], source_info["timestampScale"])
                source_scale = source_info["timestampScale"]
                if self._first_video_source is None:
                    self._first_video_source = source
                    self._video_source_scale = source_scale
                else:
                    presented = Fraction(info["timestamp"], info["timestampScale"])
                    clock_offset = source - presented
                    if clock_offset != self._video_clock_offset and source_scale == self._video_source_scale:
                        media_time = presented - Fraction(self._first_video_timestamp, info["timestampScale"])
                        camera_time = source - self._first_video_source
                        old_media, old_camera = self._camera_clock_anchor
                        if camera_time >= old_camera + media_time - old_media:
                            self._emit_record(BROWSER_STREAM_CAMERA_CLOCK, 0,
                                              struct.pack(">dd", float(media_time), float(camera_time)))
                            self._camera_clock_anchor = media_time, camera_time
                    self._video_clock_offset = clock_offset
                if self._video_clock_offset is None:
                    self._video_clock_offset = source - Fraction(info["timestamp"], info["timestampScale"])
            if not self._video_bytes:
                self._first_video_timestamp = int(info.get("timestamp") or 0)
            self._video_info = info
            if not self._video_bytes and type(info.get("timestampScale")) is int:
                self._emit_record(BROWSER_STREAM_VIDEO_FORMAT, 0, struct.pack(">I", info["timestampScale"]))
            self._write_video(data)
            return True
        if self._audio_codec and self._audio_codec != codec:
            raise NativeMuxError("Camera changed audio codec during playback")
        self._audio_codec = codec
        if "timestampScale" in info:
            info = _presentation_info(self._clock, 2, data, info)
        if codec in {"aac", "mpeg4-generic"}:
            self._emit_audio_format(info)
            data = _aac_with_adts(data, info)
            scale = info.get("timestampScale")
            if type(scale) is not int or not 0 < scale <= 1_000_000:
                raise NativeMuxError("Native AAC timestamp scale is invalid")
            if self._packet_audio is None:
                self._first_audio_timestamp = int(info.get("timestamp") or 0)
                self._packet_audio = TimestampedAacMuxer(
                    data, lambda chunk: self._emit_record(BROWSER_STREAM_AUDIO_AAC_MP4, self._first_audio_timestamp, chunk)
                )
            self._packet_audio.push(data, int(info.get("timestamp") or 0), scale)
            self._audio_bytes += len(data)
            return
        if codec == "pcm_s16le":
            self._emit_audio_format(info)
            self._audio_bytes += len(data)
            self._emit_record(
                BROWSER_STREAM_AUDIO_PCM_S16LE,
                int(info.get("timestamp") or 0),
                data,
            )
            return
        if codec in {"g711u", "pcmu", "g711a", "pcma"}:
            self._emit_audio_format(info)
            self._audio_bytes += len(data)
            self._emit_record(
                BROWSER_STREAM_AUDIO_G711A if codec in {"g711a", "pcma"} else BROWSER_STREAM_AUDIO_G711U,
                int(info.get("timestamp") or 0),
                data,
            )
            return
        raise NativeMuxError(
            f"Unsupported native browser audio codec: {codec or 'unknown'}"
        )

    def finish(self) -> None:
        if self._video_bytes <= 0:
            raise NativeMuxError("Native playback emitted no video packets")
        if self._audio_bytes <= 0:
            raise NativeMuxError("Native playback emitted no audio packets")
        if self._packet_audio is not None:
            self._packet_audio.close()
            self._packet_audio = None
        if self._packet_video is not None:
            self._packet_video.close()
            self._packet_video = None
            if not self._output_bytes:
                raise NativeMuxError("Native packet remux emitted no video")
            self._emit_record(BROWSER_STREAM_END, 0, b"")
            return
        self._close_video()
        proc = self._proc
        if proc is None:
            raise NativeMuxError("Native browser stream was not started")
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired as exc:
            proc.terminate()
            with _suppress_mux():
                proc.wait(timeout=5)
            if proc.poll() is None:
                proc.kill()
                with _suppress_mux():
                    proc.wait(timeout=5)
            raise NativeMuxError("ffmpeg did not finish browser video stream") from exc
        if self._reader is not None:
            self._reader.join(timeout=5)
        if self._reader_error is not None:
            raise NativeMuxError(
                f"Native browser stream reader failed: {type(self._reader_error).__name__}"
            )
        stderr = self._stderr.finish() if self._stderr is not None else b""
        if proc.returncode != 0:
            raise NativeMuxError(
                "ffmpeg failed to remux native browser video: "
                f"{_decode_process_output(stderr)}"
            )
        if self._output_bytes <= 0:
            raise NativeMuxError("ffmpeg emitted no browser video data")
        self._emit_record(BROWSER_STREAM_END, 0, b"")
        self.cleanup()

    def cleanup(self) -> None:
        if self._packet_audio is not None:
            self._packet_audio.close(flush=False)
            self._packet_audio = None
        if self._packet_video is not None:
            self._packet_video.close(flush=False)
            self._packet_video = None
        self._close_video()
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            with _suppress_mux():
                proc.terminate()
                proc.wait(timeout=3)
            if proc.poll() is None:
                with _suppress_mux():
                    proc.kill()
                    proc.wait(timeout=3)
        if self._reader is not None:
            self._reader.join(timeout=1)
            self._reader = None
        if self._stderr is not None:
            self._stderr.finish()
            self._stderr = None

    def _write_video(self, data: bytes) -> None:
        if not data:
            return
        scale = self._video_info.get("timestampScale")
        if scale is not None or self._packet_video is not None:
            if self._proc is not None:
                raise NativeMuxError("Camera changed video clock contract during playback")
            if type(scale) is not int or not 0 < scale <= 1_000_000:
                raise NativeMuxError("Native video timestamp scale is invalid")
            if self._packet_video is None:
                self._packet_video = TimestampedVideoMuxer(data, self._emit_video_chunk)
            self._packet_video.push(data, int(self._video_info.get("timestamp") or 0), scale,
                                    self._video_info.get("isKeyFrame", False))
            self._video_bytes += len(data)
            return
        if self._proc is None:
            self._open_legacy_video()
        if self._video_handle is None:
            raise NativeMuxError("Native browser video pipe is not open")
        _write_pipe(self._video_handle, data)
        self._video_bytes += len(data)

    def _emit_video_chunk(self, chunk):
        self._output_bytes += len(chunk)
        self._emit_record(BROWSER_STREAM_VIDEO, self._first_video_timestamp, chunk)

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while chunk := _read_pipe_chunk(proc.stdout):
                self._output_bytes += len(chunk)
                self._emit_record(
                    BROWSER_STREAM_VIDEO,
                    self._first_video_timestamp,
                    chunk,
                )
        except Exception as err:  # pragma: no cover - defensive thread boundary
            self._reader_error = err
            with _suppress_mux():
                while _read_pipe_chunk(proc.stdout):
                    pass

    def _emit_record(self, kind: int, timestamp: int, payload: bytes) -> None:
        self.chunk_callback(
            _BROWSER_STREAM_HEADER.pack(kind, max(0, timestamp), len(payload))
            + payload
        )

    def _emit_audio_format(self, info: dict[str, Any]) -> None:
        sample_rate = int(info.get("sampleRate") or 0)
        channels = int(info.get("channels") or 0)
        if not 1 <= sample_rate <= 192000 or not 1 <= channels <= 8:
            raise NativeMuxError("Native browser audio format is invalid")
        scale = int(info.get("timestampScale") or 0)
        if not 0 <= scale <= 1_000_000:
            raise NativeMuxError("Native browser timestamp scale is invalid")
        audio_format = (sample_rate, channels, scale)
        if audio_format == self._audio_format:
            return
        if self._audio_format is not None:
            raise NativeMuxError("Camera changed audio format during playback")
        self._audio_format = audio_format
        self._emit_record(
            BROWSER_STREAM_AUDIO_FORMAT,
            0,
            _BROWSER_STREAM_AUDIO_FORMAT.pack(sample_rate, channels)
            + (struct.pack(">I", scale) if scale else b""),
        )

    def _close_video(self) -> None:
        if self._video_handle is not None:
            with _suppress_mux():
                self._video_handle.close()
            self._video_handle = None


@dataclass(slots=True)
class NativePlaybackInteractiveBrowserMuxer:
    """Restart only the browser media segment when the APK seeks."""

    chunk_callback: Any
    muxer_factory: Any = NativePlaybackBrowserStreamMuxer
    _muxer: NativePlaybackBrowserStreamMuxer | None = field(
        default=None, init=False, repr=False
    )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.finish()
        else:
            self.cleanup()

    def handle_event(self, event: dict[str, Any]) -> bool | None:
        event = validate_playback_frame_event(
            event, protocol=NATIVE_PLAYBACK_EVENT_VERSION
        )
        frame_type = NativePlaybackFrameType(event["payload"]["type"])
        if frame_type == NativePlaybackFrameType.STARTED:
            info = event["payload"].get("info") or {}
            generation = info.get("generation")
            position = info.get("position")
            if type(generation) is not int or generation < 1:
                raise NativeMuxError("Interactive playback generation is invalid")
            if type(position) is not int or position < 0:
                raise NativeMuxError("Interactive playback position is invalid")
            self._replace_muxer(generation, position)
            assert self._muxer is not None
            self._muxer.handle_event(event)
            return
        if frame_type == NativePlaybackFrameType.FINISHED:
            return
        if self._muxer is None:
            raise NativeMuxError("Interactive playback emitted media before start")
        return self._muxer.handle_event(event)

    def finish(self) -> None:
        muxer, self._muxer = self._muxer, None
        if muxer is not None:
            muxer.finish()

    def cleanup(self) -> None:
        muxer, self._muxer = self._muxer, None
        if muxer is not None:
            muxer.cleanup()

    def _replace_muxer(self, generation: int, position: int) -> None:
        self.cleanup()
        muxer = self.muxer_factory(self.chunk_callback)
        if isinstance(muxer, NativePlaybackBrowserStreamMuxer):
            muxer.stream_header = browser_stream_header(generation, position)
        muxer.open()
        self._muxer = muxer


def _presentation_info(clock, kind, data, info):
    duration = None
    codec = _codec_name(info)
    if kind == 2:
        rate, channels = info.get("sampleRate"), info.get("channels")
        if type(rate) is not int or rate <= 0 or type(channels) is not int or channels <= 0:
            raise NativeMuxError("Native audio presentation clock requires a valid sample format")
        if codec in {"aac", "mpeg4-generic"}:
            duration = Fraction(1024, rate)
        else:
            width = 2 if codec == "pcm_s16le" else 1
            duration = Fraction(len(data), width * channels * rate)
    try:
        timestamp, scale = clock.map(
            kind, info.get("timestamp"), info.get("timestampScale", 1000), duration=duration,
        )
    except ValueError as err:
        raise NativeMuxError(str(err)) from err
    return {**info, "timestamp": timestamp, "timestampScale": scale}


def _h264_nals(payload: bytes) -> tuple[bytes, ...]:
    """Return Annex-B NAL units without assuming a fixed start-code width."""
    units: list[bytes] = []
    offset = 0
    while offset < len(payload):
        three = payload.find(b"\x00\x00\x01", offset)
        four = payload.find(b"\x00\x00\x00\x01", offset)
        if three < 0 and four < 0:
            break
        if four >= 0 and (three < 0 or four <= three):
            start = four + 4
        else:
            start = three + 3
        next_three = payload.find(b"\x00\x00\x01", start)
        next_four = payload.find(b"\x00\x00\x00\x01", start)
        candidates = [item for item in (next_three, next_four) if item >= 0]
        end = min(candidates) if candidates else len(payload)
        if start < end:
            units.append(payload[start:end])
        offset = end
    return tuple(units)


def _h264_metadata_only(payload: bytes) -> bool:
    """Recognize configuration/delimiter packets, not corrupt or unknown NALs."""
    nals = _h264_nals(payload)
    return bool(nals) and all(
        not nal[0] & 0x80 and nal[0] & 0x1F in {6, 7, 8, 9, 10, 11, 12}
        for nal in nals
    )


def _h264_parameter_sets(payload: bytes) -> bytes:
    """Keep the latest SPS/PPS pair so an IDR can start a fresh muxer."""
    parameter_sets = [
        b"\x00\x00\x00\x01" + nal
        for nal in _h264_nals(payload)
        if nal and nal[0] & 0x1F in {7, 8}
    ]
    return b"".join(parameter_sets)


def _merge_h264_parameter_sets(current: bytes, update: bytes) -> bytes:
    """Retain one current SPS and PPS when the camera sends them separately."""
    selected: dict[int, bytes] = {}
    for payload in (current, update):
        for nal in _h264_nals(payload):
            if nal and nal[0] & 0x1F in {7, 8}:
                selected[nal[0] & 0x1F] = nal
    return b"".join(
        b"\x00\x00\x00\x01" + selected[nal_type]
        for nal_type in (7, 8)
        if nal_type in selected
    )


def _has_h264_decoder_config(payload: bytes) -> bool:
    types = {nal[0] & 0x1F for nal in _h264_nals(payload) if nal}
    return {7, 8}.issubset(types)


def _has_h264_idr(payload: bytes) -> bool:
    return any(nal and nal[0] & 0x1F == 5 for nal in _h264_nals(payload))


def _decode_process_output(payload: Any) -> str:
    if isinstance(payload, bytes):
        return payload.decode("utf-8", "replace").strip() or "no ffmpeg stderr"
    return str(payload or "no ffmpeg stderr").strip()


class _suppress_mux:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return True
