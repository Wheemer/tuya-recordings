"""Convert native playback stream packets into the integration frame contract."""

from __future__ import annotations

from .native_frames import (
    NativeMediaCodecFrameInfo,
    NativePlaybackFrame,
    NativePlaybackFrameType,
)
from .native_playback_stream import NativePlaybackStreamFrame

NATIVE_CODEC_H264 = 1
NATIVE_CODEC_AAC = 4
NATIVE_CODEC_G711U = 133
NATIVE_CODEC_PCM_S16LE = 0xFFFE
FFMPEG_CODEC_H264 = 27
FFMPEG_CODEC_AAC = 86018


class NativeRelayMediaError(ValueError):
    """A native packet cannot be remuxed without transcoding."""


def playback_stream_frame(frame: NativePlaybackStreamFrame) -> NativePlaybackFrame:
    """Return one raw media event while preserving encoded packet bytes."""
    if not isinstance(frame, NativePlaybackStreamFrame):
        raise NativeRelayMediaError("Native playback frame is invalid")
    if not frame.payload:
        raise NativeRelayMediaError("Native playback packet is empty")

    if frame.is_audio:
        codec_info = frame.codec_info
        if codec_info is None:
            raise NativeRelayMediaError("Native audio packet has no codec information")
        codec_id = codec_info.codec
        if codec_id not in {
            NATIVE_CODEC_AAC,
            NATIVE_CODEC_G711U,
            FFMPEG_CODEC_AAC,
            NATIVE_CODEC_PCM_S16LE,
        } and not _is_adts(frame.payload):
            raise NativeRelayMediaError(
                f"Native audio codec {codec_id} requires transcoding"
            )
        sample_rate = codec_info.sample_rate
        channels = codec_info.channels
        if channels <= 0:
            raise NativeRelayMediaError("Native AAC channel count is unavailable")
        info = NativeMediaCodecFrameInfo(
            av_channel=1,
            frame_no=frame.field_28,
            codec_id=codec_id,
            is_key_frame=False,
            timestamp=_timestamp(frame),
            frame_info=codec_info.raw,
            sample_rate=sample_rate,
            channels=channels,
        )
    else:
        codec_id = frame.codec_info.codec if frame.codec_info is not None else frame.frame_type
        if codec_id not in {NATIVE_CODEC_H264, FFMPEG_CODEC_H264} and not _is_annex_b(frame.payload):
            raise NativeRelayMediaError(
                f"Native video codec {codec_id} is not H.264"
            )
        info = NativeMediaCodecFrameInfo(
            av_channel=0,
            frame_no=frame.field_28,
            codec_id=NATIVE_CODEC_H264,
            is_key_frame=_has_h264_idr(frame.payload),
            timestamp=_timestamp(frame),
            frame_info=frame.codec_info.raw if frame.codec_info is not None else b"",
        )
    return NativePlaybackFrame(
        NativePlaybackFrameType.MEDIA_CODEC,
        frame.payload,
        info.to_jsonable(),
    )


def _timestamp(frame: NativePlaybackStreamFrame) -> int:
    """Return the 64-bit timestamp assembled by ``HandlePlaybackStreamImpl``."""
    return (frame.field_8 << 32) | frame.field_12


def _is_adts(payload: bytes) -> bool:
    return len(payload) >= 2 and payload[0] == 0xFF and (payload[1] & 0xF0) == 0xF0


def _is_annex_b(payload: bytes) -> bool:
    return payload.startswith((b"\x00\x00\x01", b"\x00\x00\x00\x01"))


def _has_h264_idr(payload: bytes) -> bool:
    offset = 0
    while offset < len(payload) - 4:
        if payload[offset : offset + 3] == b"\x00\x00\x01":
            nal = offset + 3
        elif payload[offset : offset + 4] == b"\x00\x00\x00\x01":
            nal = offset + 4
        else:
            offset += 1
            continue
        if nal < len(payload) and payload[nal] & 0x1F == 5:
            return True
        offset = nal + 1
    return False
