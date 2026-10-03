"""APK-native playback frame contract.

Smart Life SD playback is not a file URL. The APK calls `startPlayBack` and
receives camera SDK callbacks for video and audio frames. This module defines
the small, serializable frame envelope that the native session emits so Home
Assistant can mux or stream the received media without falling back to browser
WebRTC or playback download APIs.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from enum import Enum
from typing import Any


NATIVE_PLAYBACK_EVENT_VERSION = 1


MAX_FRAME_BYTES = 8 * 1024 * 1024


class NativePlaybackFrameType(str, Enum):
    VIDEO_YUV = "video-yuv"
    AUDIO_PCM = "audio-pcm"
    MEDIA_CODEC = "media-codec"
    STARTED = "started"
    FINISHED = "finished"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class NativeVideoFrameInfo:
    width: int
    height: int
    frame_rate: int
    is_key_frame: bool
    timestamp: int
    progress: int
    duration: int
    angle: int = 0
    mirror: int = 0

    def validate(self) -> "NativeVideoFrameInfo":
        _positive("width", self.width)
        _positive("height", self.height)
        _non_negative("frame_rate", self.frame_rate)
        _non_negative("timestamp", self.timestamp)
        _non_negative("progress", self.progress)
        _non_negative("duration", self.duration)
        _non_negative("angle", self.angle)
        _non_negative("mirror", self.mirror)
        if type(self.is_key_frame) is not bool:
            raise ValueError("is_key_frame must be boolean")
        return self

    def to_jsonable(self) -> dict[str, Any]:
        self.validate()
        return {
            "width": self.width,
            "height": self.height,
            "frameRate": self.frame_rate,
            "isKeyFrame": self.is_key_frame,
            "timestamp": self.timestamp,
            "progress": self.progress,
            "duration": self.duration,
            "angle": self.angle,
            "mirror": self.mirror,
        }


@dataclass(frozen=True, slots=True)
class NativeAudioFrameInfo:
    sample_rate: int
    channels: int
    bit_width: int
    timestamp: int
    progress: int = 0
    duration: int = 0
    pcm_db: int | None = None

    def validate(self) -> "NativeAudioFrameInfo":
        _positive("sample_rate", self.sample_rate)
        _positive("channels", self.channels)
        _positive("bit_width", self.bit_width)
        _non_negative("timestamp", self.timestamp)
        _non_negative("progress", self.progress)
        _non_negative("duration", self.duration)
        if self.pcm_db is not None:
            _non_negative("pcm_db", self.pcm_db)
        return self

    def to_jsonable(self) -> dict[str, Any]:
        self.validate()
        payload: dict[str, Any] = {
            "sampleRate": self.sample_rate,
            "channels": self.channels,
            "bitWidth": self.bit_width,
            "timestamp": self.timestamp,
            "progress": self.progress,
            "duration": self.duration,
        }
        if self.pcm_db is not None:
            payload["pcmDb"] = self.pcm_db
        return payload


@dataclass(frozen=True, slots=True)
class NativeMediaCodecFrameInfo:
    """Metadata from `receiveFrameDataForMediaCodec` in the Smart Life APK."""

    av_channel: int
    frame_no: int
    codec_id: int
    is_key_frame: bool
    timestamp: int = 0
    frame_info: bytes = b""
    sample_rate: int | None = None
    channels: int | None = None

    def validate(self) -> "NativeMediaCodecFrameInfo":
        _non_negative("av_channel", self.av_channel)
        _non_negative("frame_no", self.frame_no)
        _non_negative("codec_id", self.codec_id)
        _non_negative("timestamp", self.timestamp)
        if type(self.is_key_frame) is not bool:
            raise ValueError("is_key_frame must be boolean")
        if not isinstance(self.frame_info, bytes):
            raise ValueError("frame_info must be bytes")
        if len(self.frame_info) > MAX_FRAME_BYTES:
            raise ValueError("frame_info is too large")
        if self.sample_rate is not None:
            _positive("sample_rate", self.sample_rate)
        if self.channels is not None:
            _positive("channels", self.channels)
        return self

    def to_jsonable(self) -> dict[str, Any]:
        self.validate()
        payload: dict[str, Any] = {
            "avChannel": self.av_channel,
            "frameNo": self.frame_no,
            "codecId": self.codec_id,
            "codec": self.codec_id,
            "isKeyFrame": self.is_key_frame,
            "timestamp": self.timestamp,
            "frameInfoBase64": base64.b64encode(self.frame_info).decode("ascii"),
        }
        if self.sample_rate is not None:
            payload["sampleRate"] = self.sample_rate
        if self.channels is not None:
            payload["channels"] = self.channels
        return payload


@dataclass(frozen=True, slots=True)
class NativePlaybackFrame:
    frame_type: NativePlaybackFrameType
    payload: bytes = b""
    info: dict[str, Any] | None = None

    def validate(self) -> "NativePlaybackFrame":
        if not isinstance(self.frame_type, NativePlaybackFrameType):
            raise ValueError("frame_type must be a NativePlaybackFrameType")
        if not isinstance(self.payload, bytes):
            raise ValueError("payload must be bytes")
        if len(self.payload) > MAX_FRAME_BYTES:
            raise ValueError("payload is too large")
        if self.info is not None and not isinstance(self.info, dict):
            raise ValueError("info must be an object")
        return self

    def to_jsonable(self) -> dict[str, Any]:
        self.validate()
        return {
            "type": self.frame_type.value,
            "payloadBase64": base64.b64encode(self.payload).decode("ascii"),
            "info": self.info or {},
        }


def playback_frame_event(frame: NativePlaybackFrame, *, protocol: int) -> dict[str, Any]:
    return {
        "protocol": protocol,
        "event": "playbackFrame",
        "payload": frame.to_jsonable(),
    }


def validate_playback_frame_event(event: dict[str, Any], *, protocol: int) -> dict[str, Any]:
    """Validate a playback-frame event before it reaches the muxer."""
    if not isinstance(event, dict):
        raise ValueError("playback event must be an object")
    if event.get("protocol") != protocol:
        raise ValueError("playback event protocol version mismatch")
    if event.get("event") != "playbackFrame":
        raise ValueError("playback event type must be playbackFrame")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("playback event payload must be an object")
    frame_type = NativePlaybackFrameType(payload.get("type"))
    info = payload.get("info") or {}
    if not isinstance(info, dict):
        raise ValueError("playback frame info must be an object")
    encoded = payload.get("payloadBase64") or ""
    if not isinstance(encoded, str):
        raise ValueError("playback frame payloadBase64 must be a string")
    try:
        frame_payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as err:
        raise ValueError("playback frame payloadBase64 is invalid") from err
    NativePlaybackFrame(frame_type, frame_payload, info).validate()
    if frame_type == NativePlaybackFrameType.VIDEO_YUV:
        _validate_yuv_lengths(frame_payload, info)
    if frame_type == NativePlaybackFrameType.AUDIO_PCM:
        NativeAudioFrameInfo(
            sample_rate=_int_info(info, "sampleRate"),
            channels=_int_info(info, "channels"),
            bit_width=_int_info(info, "bitWidth"),
            timestamp=_int_info(info, "timestamp"),
            progress=_int_info(info, "progress", default=0),
            duration=_int_info(info, "duration", default=0),
            pcm_db=_optional_int_info(info, "pcmDb"),
        ).validate()
    if frame_type == NativePlaybackFrameType.MEDIA_CODEC:
        NativeMediaCodecFrameInfo(
            av_channel=_int_info(info, "avChannel"),
            frame_no=_int_info(info, "frameNo"),
            codec_id=_int_info(info, "codecId"),
            is_key_frame=_bool_info(info, "isKeyFrame"),
            timestamp=_int_info(info, "timestamp", default=0),
            frame_info=_base64_info(info, "frameInfoBase64", default=b""),
            sample_rate=_optional_int_info(info, "sampleRate"),
            channels=_optional_int_info(info, "channels"),
        ).validate()
    return {
        "protocol": protocol,
        "event": "playbackFrame",
        "payload": {
            "type": frame_type.value,
            "payloadBase64": encoded,
            "info": info,
        },
    }


class NativePlaybackFrameSink:
    """Synchronous sink implemented by muxers or tests."""

    def started(self, info: dict[str, Any] | None = None) -> None:
        self.frame(NativePlaybackFrame(NativePlaybackFrameType.STARTED, info=info or {}))

    def video_yuv(self, y: bytes, u: bytes, v: bytes, info: NativeVideoFrameInfo) -> None:
        meta = info.to_jsonable()
        meta["yLength"] = len(y)
        meta["uLength"] = len(u)
        meta["vLength"] = len(v)
        self.frame(NativePlaybackFrame(NativePlaybackFrameType.VIDEO_YUV, y + u + v, meta))

    def audio_pcm(self, pcm: bytes, info: NativeAudioFrameInfo) -> None:
        self.frame(NativePlaybackFrame(NativePlaybackFrameType.AUDIO_PCM, pcm, info.to_jsonable()))

    def media_codec(
        self,
        frame: bytes,
        *,
        av_channel: int,
        frame_no: int,
        codec_id: int,
        is_key_frame: bool,
        timestamp: int = 0,
        frame_info: bytes = b"",
        sample_rate: int | None = None,
        channels: int | None = None,
    ) -> None:
        self.frame(
            NativePlaybackFrame(
                NativePlaybackFrameType.MEDIA_CODEC,
                frame,
                NativeMediaCodecFrameInfo(
                    av_channel=av_channel,
                    frame_no=frame_no,
                    codec_id=codec_id,
                    is_key_frame=is_key_frame,
                    timestamp=timestamp,
                    frame_info=frame_info,
                    sample_rate=sample_rate,
                    channels=channels,
                ).to_jsonable(),
            )
        )

    def finished(self, info: dict[str, Any] | None = None) -> None:
        self.frame(NativePlaybackFrame(NativePlaybackFrameType.FINISHED, info=info or {}))

    def error(self, message: str) -> None:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        self.frame(NativePlaybackFrame(NativePlaybackFrameType.ERROR, info={"message": message}))

    def frame(self, frame: NativePlaybackFrame) -> None:
        raise NotImplementedError


class NativePlaybackEventSink(NativePlaybackFrameSink):
    """Frame sink that emits validated native playback events."""

    def __init__(self, emit: Any, *, protocol: int) -> None:
        if not callable(emit):
            raise ValueError("emit must be callable")
        self.emit = emit
        self.protocol = protocol

    def frame(self, frame: NativePlaybackFrame) -> None:
        event = playback_frame_event(frame, protocol=self.protocol)
        self.emit(validate_playback_frame_event(event, protocol=self.protocol))


def _positive(name: str, value: int) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _non_negative(name: str, value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _int_info(info: dict[str, Any], key: str, *, default: int | None = None) -> int:
    value = info.get(key, default)
    if type(value) is not int:
        raise ValueError(f"{key} must be an integer")
    return value


def _optional_int_info(info: dict[str, Any], key: str) -> int | None:
    value = info.get(key)
    if value is None:
        return None
    if type(value) is not int:
        raise ValueError(f"{key} must be an integer")
    return value


def _bool_info(info: dict[str, Any], key: str) -> bool:
    value = info.get(key)
    if type(value) is not bool:
        raise ValueError(f"{key} must be boolean")
    return value


def _base64_info(info: dict[str, Any], key: str, *, default: bytes) -> bytes:
    value = info.get(key)
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as err:
        raise ValueError(f"{key} is invalid") from err


def _validate_yuv_lengths(payload: bytes, info: dict[str, Any]) -> None:
    y_length = _int_info(info, "yLength")
    u_length = _int_info(info, "uLength")
    v_length = _int_info(info, "vLength")
    _non_negative("yLength", y_length)
    _non_negative("uLength", u_length)
    _non_negative("vLength", v_length)
    if y_length + u_length + v_length != len(payload):
        raise ValueError("YUV payload lengths do not match payload size")
