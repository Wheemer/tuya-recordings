"""Bounded full-header diagnostics must not expose compressed media or keys."""

import logging
import struct
from fractions import Fraction
from types import SimpleNamespace

import av
import pytest

from custom_components.tuya_recordings.lib import native_packet_cache as cache
from custom_components.tuya_recordings.lib.native_mux import (
    NativePlaybackMuxer, h264_packet_diagnostics,
)


def test_libav_packet_owns_storage_instead_of_aliasing_python_bytes():
    data = bytes(bytearray(b"\x00\x00\x00\x01\x81source"))
    packet = cache._owned_packet(data)
    assert packet.size == len(data) and bytes(packet) == data
    memoryview(packet)[4] = 0x65
    assert data[4] == 0x81
    assert bytes(packet)[4] == 0x65


def test_header_summary_preserves_flag_bits_without_media_contents():
    payload = b"\x00\x00\x00\x01\x95DO_NOT_LOG_FOOTAGE" * 20
    result = h264_packet_diagnostics(payload)
    assert result["build"] == "sdk-masked-headers-20261115"
    assert result["nal_count"] == 20 and result["headers_truncated"] is True
    assert len(result["headers"]) == 8
    assert result["headers"][0] == {
        "byte": "0x95", "type": 21, "forbidden": True, "ref_idc": 0, "bytes": 19,
    }
    assert len(result["sha256"]) == 16
    assert "DO_NOT_LOG" not in str(result)


def test_callback_and_index_traces_are_bounded_and_keep_payloads_private(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.tuya_recordings.lib.native_mux")
    muxer = NativePlaybackMuxer(tmp_path / "clip.mp4")
    first = b"\x00\x00\x01\x67SPS\x00\x00\x01\x68PPS\x00\x00\x01\x65IDR"
    muxer.open()
    try:
        muxer._write_media_codec_packet(first, {"codecName": "h264", "timestamp": 1000})
        for index in range(40):
            muxer._write_media_codec_packet(b"\x00\x00\x01\x95DO_NOT_LOG_FOOTAGE",
                                           {"codecName": "h264", "timestamp": 1001 + index})
    finally:
        muxer.cleanup()
    messages = [record.getMessage() for record in caplog.records]
    assert sum("stage=cache_callback" in message for message in messages) == 8
    assert sum("stage=cache_index" in message for message in messages) == 1
    assert "0x95" in caplog.text and "'forbidden': True" in caplog.text
    assert "DO_NOT_LOG" not in caplog.text
    fingerprint = h264_packet_diagnostics(first)["sha256"]
    assert sum(fingerprint in message for message in messages) == 2


def test_decode_error_reports_immutable_pre_decoder_headers(monkeypatch, tmp_path):
    data = b"\x00\x00\x00\x01\x81PRIVATE_ENCODED_CONTENT"
    path = tmp_path / "index"
    path.write_bytes(cache.PACKET_HEADER.pack(1, 0, 1000, 1000, len(data)) + data)

    class Decoder:
        def decode(self, packet):
            memoryview(packet)[4] = 0x65
            raise av.error.InvalidDataError(1094995529, "fixture decode failure")

    monkeypatch.setattr(cache.av, "CodecContext", SimpleNamespace(create=lambda *args: Decoder()))
    video = SimpleNamespace(streams=SimpleNamespace(video=[SimpleNamespace(
        codec_context=SimpleNamespace(has_b_frames=True, extradata=b""))]))
    with pytest.raises(ValueError, match="picture decode failed") as error:
        cache._reordered_video_timing(video, path, Fraction(1))
    message = str(error.value)
    assert "0x81" in message and "'forbidden': True" in message
    assert "packet_unchanged=False" in message
    assert "0x65" not in message and "PRIVATE_ENCODED_CONTENT" not in message


def test_frame_conversion_logs_cipher_and_clear_fingerprints_not_secrets(caplog):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from custom_components.tuya_recordings.lib.native_playback_stream import NativePlaybackStreamParser
    from custom_components.tuya_recordings.lib.native_relay_runtime import NativeRelayRuntimeSession
    caplog.set_level(logging.DEBUG, logger="custom_components.tuya_recordings.lib.native_relay_runtime")
    secret = "0123456789abcdef"
    uuid = "private-uuid"
    clear = b"\x00\x00\x00\x01\x95PRIVATE_FOOTAGE"
    pad = 16 - len(clear) % 16
    iv = bytes(range(16))
    encryptor = Cipher(algorithms.AES(secret.encode()), modes.CBC(iv)).encryptor()
    encrypted = encryptor.update(clear + bytes([pad]) * pad) + encryptor.finalize()
    codec = bytearray(60)
    struct.pack_into(">6H", codec, 0, 1, 0, 0, 1, 1, 0)
    codec[12:12 + len(uuid)] = uuid.encode()
    codec[44:60] = iv
    [frame] = NativePlaybackStreamParser().feed(
        struct.pack(">HH7I", 1, 1, len(encrypted), 0, 1000, 0, 0, 0, 1) + codec + encrypted)
    converted = NativeRelayRuntimeSession._convert_frame(frame, {uuid: secret}, uuid, trace=True)
    assert converted.payload == clear
    assert "stage=frame_conversion" in caplog.text and "encrypted=True" in caplog.text
    assert h264_packet_diagnostics(encrypted)["sha256"] in caplog.text
    assert h264_packet_diagnostics(clear)["sha256"] in caplog.text
    assert "0x95" in caplog.text and "'forbidden': True" in caplog.text
    assert all(value not in caplog.text for value in (secret, uuid, "PRIVATE_FOOTAGE"))
