import struct

import pytest

from custom_components.tuya_recordings.lib.native_playback_stream import (
    PLAYBACK_AUDIO_FRAME_TYPE,
    PLAYBACK_CODEC_INFO_BYTES,
    PLAYBACK_MAX_PAYLOAD_BYTES,
    NativePlaybackStreamError,
    NativePlaybackStreamParser,
)


def frame(payload=b"encoded", *, frame_type=1, info=None, fields=(2, 3, 4, 5, 6, 7)):
    info_flag = 1 if info is not None else 0
    header = struct.pack(">HH7I", frame_type, info_flag, len(payload), *fields)
    return header + (info or b"") + payload


def codec_info(values=(4, 8, 1, 1, 9, 10)):
    return struct.pack(">6H", *values) + bytes(PLAYBACK_CODEC_INFO_BYTES - 12)


def test_parses_header_payload_and_all_generic_fields():
    parser = NativePlaybackStreamParser()
    [result] = parser.feed(frame())
    assert result.frame_type == 1
    assert result.payload == b"encoded"
    assert result.payload_length == 7
    assert (
        result.field_8,
        result.field_12,
        result.field_16,
        result.field_20,
        result.field_24,
        result.field_28,
    ) == (2, 3, 4, 5, 6, 7)
    assert result.codec_info is None
    assert not result.is_audio


def test_parses_audio_codec_indexes_with_sdk_lookup_tables():
    parser = NativePlaybackStreamParser()
    [result] = parser.feed(
        frame(b"aac", frame_type=PLAYBACK_AUDIO_FRAME_TYPE, info=codec_info())
    )
    assert result.is_audio
    assert result.codec_info.codec == 4
    assert result.codec_info.sample_rate == 48000
    assert result.codec_info.channels == 2
    assert result.codec_info.bit_width == 16
    assert result.codec_info.raw == codec_info()


@pytest.mark.parametrize(
    ("sample_rate_index", "sample_rate"),
    [(1, 11025), (4, 22050), (7, 44100)],
)
def test_parses_standard_fractional_audio_sample_rates(
    sample_rate_index, sample_rate
):
    [result] = NativePlaybackStreamParser().feed(
        frame(
            b"aac",
            frame_type=PLAYBACK_AUDIO_FRAME_TYPE,
            info=codec_info((4, sample_rate_index, 0, 1, 0, 0)),
        )
    )

    assert result.codec_info.sample_rate == sample_rate


def test_invalid_audio_indexes_match_sdk_fallbacks():
    [result] = NativePlaybackStreamParser().feed(
        frame(frame_type=3, info=codec_info((1, 99, 99, 99, 0, 0)))
    )
    assert result.codec_info.sample_rate == 8000
    assert result.codec_info.channels == 0
    assert result.codec_info.bit_width == 0


def test_incrementally_reassembles_and_emits_multiple_frames():
    parser = NativePlaybackStreamParser()
    data = frame(b"one") + frame(b"two", frame_type=3, info=codec_info())
    results = []
    for byte in data:
        results.extend(parser.feed(bytes((byte,))))
    assert [item.payload for item in results] == [b"one", b"two"]
    assert parser.buffered_bytes == 0
    parser.finish()


def test_rejects_oversized_frame_before_waiting_for_payload():
    header = struct.pack(">HH7I", 1, 0, PLAYBACK_MAX_PAYLOAD_BYTES + 1, *([0] * 6))
    parser = NativePlaybackStreamParser()
    with pytest.raises(NativePlaybackStreamError, match="exceeds"):
        parser.feed(header)
    assert parser.buffered_bytes == 0


def test_finish_rejects_and_clears_incomplete_frame():
    parser = NativePlaybackStreamParser()
    assert parser.feed(frame()[:-1]) == []
    with pytest.raises(NativePlaybackStreamError, match="incomplete"):
        parser.finish()
    assert parser.buffered_bytes == 0


def test_feed_rejects_empty_or_mutable_input():
    parser = NativePlaybackStreamParser()
    with pytest.raises(NativePlaybackStreamError):
        parser.feed(b"")
    with pytest.raises(NativePlaybackStreamError):
        parser.feed(bytearray(b"x"))
