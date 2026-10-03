import struct

import pytest

from custom_components.tuya_recordings.lib.native_frames import NativePlaybackFrameType
from custom_components.tuya_recordings.lib.native_playback_stream import (
    PLAYBACK_CODEC_INFO_BYTES,
    NativePlaybackStreamParser,
)
from custom_components.tuya_recordings.lib.native_relay_media import (
    NativeRelayMediaError,
    playback_stream_frame,
)


def stream_frame(payload, *, frame_type, codec=None, fields=(7, 8, 9, 10, 11, 12)):
    info = b""
    flag = 0
    if codec is not None:
        flag = 1
        info = struct.pack(">6H", codec, 8, 1, 1, 0, 0) + bytes(
            PLAYBACK_CODEC_INFO_BYTES - 12
        )
    wire = struct.pack(">HH7I", frame_type, flag, len(payload), *fields) + info + payload
    return NativePlaybackStreamParser().feed(wire)[0]


def test_converts_annex_b_h264_and_detects_idr():
    converted = playback_stream_frame(
        stream_frame(b"\x00\x00\x00\x01\x65video", frame_type=1)
    )
    assert converted.frame_type == NativePlaybackFrameType.MEDIA_CODEC
    assert converted.info["codecId"] == 1
    assert converted.info["isKeyFrame"] is True
    assert converted.info["frameNo"] == 12
    assert converted.info["timestamp"] == (7 << 32) | 8


def test_converts_compact_native_aac_codec_with_parameters():
    converted = playback_stream_frame(
        stream_frame(b"aac", frame_type=3, codec=4)
    )
    assert converted.info["codecId"] == 4
    assert converted.info["sampleRate"] == 48000
    assert converted.info["channels"] == 2


def test_preserves_native_g711u_without_transcoding():
    converted = playback_stream_frame(
        stream_frame(b"g711", frame_type=3, codec=133)
    )
    assert converted.info["codecId"] == 133
    assert converted.info["sampleRate"] == 48000
    assert converted.info["channels"] == 2


def test_preserves_native_pcm_s16le_without_transcoding():
    converted = playback_stream_frame(
        stream_frame(b"\x00\x00\x00\x01", frame_type=3, codec=0xFFFE)
    )
    assert converted.info["codecId"] == 0xFFFE
    assert converted.info["sampleRate"] == 48000
    assert converted.info["channels"] == 2


def test_rejects_unknown_audio_that_would_require_transcoding():
    with pytest.raises(NativeRelayMediaError, match="requires transcoding"):
        playback_stream_frame(stream_frame(b"unknown", frame_type=3, codec=134))
