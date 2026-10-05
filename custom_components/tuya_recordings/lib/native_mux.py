"""Mux APK-native playback frame events into browser-playable MP4 files."""

from __future__ import annotations

import base64
import binascii
import os
import struct
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from .native_frames import (
    NATIVE_PLAYBACK_EVENT_VERSION,
    NativePlaybackFrameType,
    validate_playback_frame_event,
)


class NativeMuxError(RuntimeError):
    """Native playback events could not be converted into a media file."""


BROWSER_STREAM_MAGIC = b"TRS2"
BROWSER_STREAM_VIDEO = 1
BROWSER_STREAM_AUDIO_PCM_S16LE = 2
BROWSER_STREAM_END = 3
BROWSER_STREAM_AUDIO_G711U = 4
BROWSER_STREAM_AUDIO_FORMAT = 5
_BROWSER_STREAM_HEADER = struct.Struct(">BQI")
_BROWSER_STREAM_AUDIO_FORMAT = struct.Struct(">IB")
_PIPE_READ_BYTES = 64 * 1024


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
    process_module: Any = subprocess
    protocol: int = NATIVE_PLAYBACK_EVENT_VERSION
    _video_path: Path = field(init=False, repr=False)
    _audio_path: Path = field(init=False, repr=False)
    _temp_path: Path = field(init=False, repr=False)
    _video_bytes: int = field(default=0, init=False, repr=False)
    _audio_bytes: int = field(default=0, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _finished: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.output_path = Path(self.output_path)
        if not self.output_path.is_absolute():
            raise NativeMuxError("Native mux output path must be absolute")
        self._video_path = self.output_path.with_suffix(".video.h264")
        self._audio_path = self.output_path.with_suffix(".audio.aac")
        self._temp_path = self.output_path.with_suffix(".mux.tmp.mp4")
        self._video_bytes = 0
        self._audio_bytes = 0
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
        self._run_ffmpeg()
        if not self.output_path.exists() or self.output_path.stat().st_size <= 0:
            raise NativeMuxError("Native mux did not create a playable media file")
        self._cleanup_intermediates()

    def cleanup(self) -> None:
        for path in (self._video_path, self._audio_path, self._temp_path, self.output_path):
            path.unlink(missing_ok=True)

    def _write_media_codec_packet(self, data: bytes, info: dict[str, Any]) -> None:
        codec = _codec_name(info)
        if codec in {"h264", "avc"}:
            _append_bytes(self._video_path, data)
            self._video_bytes += len(data)
            return
        if codec in {"aac", "mpeg4-generic"}:
            data = _aac_with_adts(data, info)
            _append_bytes(self._audio_path, data)
            self._audio_bytes += len(data)
            return
        raise NativeMuxError(f"Unsupported native raw packet codec: {codec or 'unknown'}")

    def _run_ffmpeg(self) -> None:
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "+genpts",
            "-f",
            "h264",
            "-i",
            str(self._video_path),
        ]
        if self._audio_bytes > 0:
            command.extend(["-f", "aac", "-i", str(self._audio_path)])
        command.extend([
            "-map",
            "0:v:0",
        ])
        if self._audio_bytes > 0:
            command.extend(["-map", "1:a:0"])
        command.extend(["-c", "copy"])
        if self._audio_bytes > 0:
            command.extend(["-bsf:a", "aac_adtstoasc"])
        command.extend(
            [
                "-movflags",
                "+faststart",
                "-y",
                str(self._temp_path),
            ]
        )
        result = self.process_module.run(
            command,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            self._temp_path.unlink(missing_ok=True)
            raise NativeMuxError(
                "ffmpeg failed to mux native Tuya recording: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        if not self._temp_path.exists() or self._temp_path.stat().st_size <= 0:
            raise NativeMuxError("ffmpeg did not create a native Tuya recording")
        self._temp_path.replace(self.output_path)

    def _cleanup_intermediates(self) -> None:
        self._video_path.unlink(missing_ok=True)
        self._audio_path.unlink(missing_ok=True)
        self._temp_path.unlink(missing_ok=True)


def _decode_payload(payload: dict[str, Any]) -> bytes:
    try:
        return base64.b64decode(payload.get("payloadBase64") or "", validate=True)
    except (binascii.Error, ValueError) as err:
        raise NativeMuxError("Native media packet payload is invalid base64") from err


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
        stderr = proc.stderr.read() if proc.stderr is not None else b""
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

    def _write_video(self, data: bytes) -> None:
        if not data:
            return
        if self._video_handle is None:
            raise NativeMuxError("Native stream video pipe is not open")
        self._video_handle.write(data)
        self._video_bytes += len(data)

    def _write_audio(self, data: bytes) -> None:
        if not data:
            return
        if self._audio_handle is None:
            raise NativeMuxError("Native stream audio pipe is not open")
        self._audio_handle.write(data)
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
    """Stream copied H.264 plus browser-decodable PCM or G.711 audio."""

    chunk_callback: Any
    process_module: Any = subprocess
    pipe_factory: Any = os.pipe
    protocol: int = NATIVE_PLAYBACK_EVENT_VERSION
    _video_handle: Any = field(default=None, init=False, repr=False)
    _proc: Any = field(default=None, init=False, repr=False)
    _reader: threading.Thread | None = field(default=None, init=False, repr=False)
    _video_bytes: int = field(default=0, init=False, repr=False)
    _audio_bytes: int = field(default=0, init=False, repr=False)
    _output_bytes: int = field(default=0, init=False, repr=False)
    _reader_error: BaseException | None = field(default=None, init=False, repr=False)
    _first_video_timestamp: int = field(default=0, init=False, repr=False)
    _audio_format: tuple[int, int] | None = field(default=None, init=False, repr=False)
    _announced: bool = field(default=False, init=False, repr=False)

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
        if not callable(self.pipe_factory):
            raise NativeMuxError("Native browser stream requires POSIX pipes")
        self.cleanup()
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
        self._video_bytes = 0
        self._audio_bytes = 0
        self._output_bytes = 0
        self._reader_error = None
        self._first_video_timestamp = 0
        self._audio_format = None
        self._announced = False
        self._reader = threading.Thread(
            target=self._read_stdout,
            name="tuya-recordings-browser-stream",
            daemon=True,
        )
        self._reader.start()

    def handle_event(self, event: dict[str, Any]) -> None:
        event = validate_playback_frame_event(event, protocol=self.protocol)
        payload = event["payload"]
        frame_type = NativePlaybackFrameType(payload["type"])
        if frame_type == NativePlaybackFrameType.STARTED:
            if not self._announced:
                self.chunk_callback(BROWSER_STREAM_MAGIC)
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
                if not _has_h264_sps(data):
                    return
                self._first_video_timestamp = int(info.get("timestamp") or 0)
            self._write_video(data)
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
        if codec in {"g711u", "pcmu"}:
            self._emit_audio_format(info)
            self._audio_bytes += len(data)
            self._emit_record(
                BROWSER_STREAM_AUDIO_G711U,
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
        stderr = proc.stderr.read() if proc.stderr is not None else b""
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

    def _write_video(self, data: bytes) -> None:
        if not data:
            return
        if self._video_handle is None:
            raise NativeMuxError("Native browser video pipe is not open")
        self._video_handle.write(data)
        self._video_bytes += len(data)

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
        audio_format = (sample_rate, channels)
        if audio_format == self._audio_format:
            return
        self._audio_format = audio_format
        self._emit_record(
            BROWSER_STREAM_AUDIO_FORMAT,
            0,
            _BROWSER_STREAM_AUDIO_FORMAT.pack(sample_rate, channels),
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

    def handle_event(self, event: dict[str, Any]) -> None:
        event = validate_playback_frame_event(
            event, protocol=NATIVE_PLAYBACK_EVENT_VERSION
        )
        frame_type = NativePlaybackFrameType(event["payload"]["type"])
        if frame_type == NativePlaybackFrameType.STARTED:
            self._replace_muxer()
            assert self._muxer is not None
            self._muxer.handle_event(event)
            return
        if frame_type == NativePlaybackFrameType.FINISHED:
            return
        if self._muxer is None:
            raise NativeMuxError("Interactive playback emitted media before start")
        self._muxer.handle_event(event)

    def finish(self) -> None:
        muxer, self._muxer = self._muxer, None
        if muxer is not None:
            muxer.finish()

    def cleanup(self) -> None:
        muxer, self._muxer = self._muxer, None
        if muxer is not None:
            muxer.cleanup()

    def _replace_muxer(self) -> None:
        self.cleanup()
        muxer = self.muxer_factory(self.chunk_callback)
        muxer.open()
        self._muxer = muxer


def _has_h264_sps(payload: bytes) -> bool:
    return any(nal and nal[0] & 0x1F == 7 for nal in payload.split(b"\x00\x00\x01")[1:])


def _decode_process_output(payload: Any) -> str:
    if isinstance(payload, bytes):
        return payload.decode("utf-8", "replace").strip() or "no ffmpeg stderr"
    return str(payload or "no ffmpeg stderr").strip()


class _suppress_mux:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return True
