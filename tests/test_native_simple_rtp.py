import struct

import pytest

from custom_components.tuya_recordings.lib.native_simple_rtp import (
    NativeH264RtpDepacketizer,
    NativeRtpPacket,
    NativeSimpleMediaParser,
    NativeSimpleRtpError,
    parse_rtp_packet,
)


def test_parses_captured_simple_media_packet_incrementally():
    captured = bytes.fromhex(
        "040001000000000025280bb6a00100000800000000000000"
        "0100800738040f00"
        "10000000"
        "80600001c13fbc9000000000a28ee3c8"
    )
    parser = NativeSimpleMediaParser()
    assert parser.feed(captured[:31]) == ()
    packets = parser.feed(captured[31:])
    assert len(packets) == 1
    assert len(packets[0].header) == 24
    assert packets[0].stream_id == 0x00010004
    assert len(packets[0].extension) == 8
    assert packets[0].codec_info == b""
    rtp = parse_rtp_packet(packets[0].payload)
    assert rtp.payload_type == 96
    assert rtp.sequence == 1
    assert rtp.timestamp == 0xC13FBC90
    assert rtp.payload == bytes.fromhex("a28ee3c8")


def test_parses_codec_info_and_rtp_extension():
    header = bytes(16) + struct.pack("<I", 1) + bytes(4)
    extension = b"\x00\x01" + bytes(6)
    codec_info = bytes(range(52))
    rtp = (
        b"\x90\xe1\x00\x02\x00\x00\x00\x03\x00\x00\x00\x04"
        b"\x12\x34\x00\x01" + b"abcd" + b"payload"
    )
    framed = header + extension + codec_info + struct.pack("<I", len(rtp)) + rtp
    packet = NativeSimpleMediaParser().feed(framed)[0]
    assert packet.codec_info == codec_info
    parsed = parse_rtp_packet(packet.payload)
    assert parsed.payload_type == 97
    assert parsed.marker
    assert parsed.payload == b"payload"


def test_rejects_invalid_rtp_version():
    with pytest.raises(NativeSimpleRtpError, match="version"):
        parse_rtp_packet(bytes(12))


def test_reassembles_h264_fu_a_as_annex_b():
    depacketizer = NativeH264RtpDepacketizer()
    first = NativeRtpPacket(96, False, 10, 90_000, 1, b"\x7c\x85abc")
    last = NativeRtpPacket(96, True, 11, 90_000, 1, b"\x7c\x45def")
    assert depacketizer.push(first) == ()
    assert depacketizer.push(last) == (b"\x00\x00\x00\x01\x65abcdef",)


def test_reassembles_h264_stap_a():
    depacketizer = NativeH264RtpDepacketizer()
    payload = b"\x78\x00\x02\x67a\x00\x02\x68b"
    packet = NativeRtpPacket(96, True, 1, 2, 3, payload)
    assert depacketizer.push(packet) == (
        b"\x00\x00\x00\x01\x67a\x00\x00\x00\x01\x68b",
    )


def test_finishes_unmarked_access_unit_on_timestamp_change():
    depacketizer = NativeH264RtpDepacketizer()
    assert depacketizer.push(
        NativeRtpPacket(96, False, 1, 100, 1, b"\x61one")
    ) == ()
    assert depacketizer.push(
        NativeRtpPacket(96, False, 2, 200, 1, b"\x61two")
    ) == (b"\x00\x00\x00\x01\x61one",)
