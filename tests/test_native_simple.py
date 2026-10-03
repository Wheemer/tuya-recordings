import json
import struct
from datetime import date

import pytest
from custom_components.tuya_recordings.lib.native_simple import (
    NativeSimpleCommand,
    NativeSimpleCommandDecoder,
    NativeSimpleError,
    decode_catalog_reply,
    decode_event_catalog_reply,
    decode_thumbnail_reply,
    encode_audio_open,
    encode_catalog_request,
    encode_event_catalog_request,
    encode_playback_start,
    encode_playback_start_fragments,
    encode_playback_pause,
    encode_playback_resume,
    encode_playback_stop,
    encode_preview_stop,
    encode_thumbnail_request,
    playback_stream_id,
)


def header(packet):
    return struct.unpack_from("<IIIHHI", packet)


def test_encodes_apk_catalog_command():
    packet = encode_catalog_request(
        request_id=2, channel=0, day=date(2026, 9, 18)
    )

    assert header(packet) == (0x12345678, 2, 0, 3, 1, 16)
    assert struct.unpack_from("<4i", packet, 20) == (0, 2026, 9, 18)


def test_encodes_apk_v3_catalog_command():
    packet = encode_catalog_request(
        request_id=2,
        channel=0,
        day=date(2026, 9, 18),
        version=2,
        encrypted=1,
    )

    assert header(packet) == (0x12345678, 2, 0, 3, 7, 20)
    assert struct.unpack_from("<5i", packet, 20) == (0, 2026, 9, 18, 1)


def test_encodes_apk_v1_event_catalog_command():
    packet = encode_event_catalog_request(
        request_id=7, channel=0, day=date(2026, 9, 15)
    )

    assert header(packet) == (0x12345678, 7, 0, 3, 3, 84)
    assert packet[20:84] == bytes(64)
    assert struct.unpack_from("<5i", packet, 84) == (0, 2026, 9, 15, 0)


def test_encodes_apk_v2_event_catalog_command():
    packet = encode_event_catalog_request(
        request_id=7, channel=0, day=date(2026, 9, 15), v2=True
    )

    assert header(packet) == (0x12345678, 7, 0, 3, 9, 20)
    assert struct.unpack_from("<5i", packet, 20) == (0, 2026, 9, 15, 1)


def test_decodes_apk_event_catalog_json():
    payload = json.dumps(
        [{"startTime": 100, "endTime": 110, "eventType": 1}]
    ).encode()
    reply = NativeSimpleCommand(7, 1, 3, 9, payload)

    page = decode_event_catalog_reply(reply, request_id=7)

    assert [(item.start_time, item.end_time) for item in page.items] == [
        (100, 110)
    ]


def test_decodes_apk_legacy_extended_event_catalog():
    payload = bytearray(0x64 + 2 * 0x20)
    struct.pack_into("<III", payload, 0x50, 1, 5, 2)
    struct.pack_into("<I", payload, 0x60, 2)
    struct.pack_into(
        "<IIHH20s", payload, 0x64, 100, 110, 1, 2, b"first.jpg"
    )
    struct.pack_into(
        "<IIHH20s", payload, 0x84, 120, 140, 1, 4, b"second.jpg"
    )
    reply = NativeSimpleCommand(7, 1, 3, 3, bytes(payload))

    page = decode_event_catalog_reply(reply, request_id=7)

    assert page.page == 2
    assert page.total_files == 5
    assert page.total_pages == 3
    assert [
        (item.start_time, item.end_time, item.event_type)
        for item in page.items
    ] == [(100, 110, 2), (120, 140, 4)]


def test_decodes_apk_legacy_compact_event_catalog():
    payload = bytearray(0x60 + 2 * 0x0C)
    struct.pack_into("<III", payload, 0x50, 0, 2, 2)
    struct.pack_into("<I", payload, 0x5C, 2)
    struct.pack_into("<HHII", payload, 0x60, 1, 2, 100, 110)
    struct.pack_into("<HHII", payload, 0x6C, 1, 4, 120, 140)
    reply = NativeSimpleCommand(7, 1, 3, 3, bytes(payload))

    page = decode_event_catalog_reply(reply, request_id=7)

    assert [
        (item.start_time, item.end_time, item.event_type)
        for item in page.items
    ] == [(100, 110, 2), (120, 140, 4)]


def test_rejects_binary_reply_for_v2_event_catalog():
    reply = NativeSimpleCommand(7, 1, 3, 9, bytes(0x64))

    with pytest.raises(NativeSimpleError, match="V2 event catalog"):
        decode_event_catalog_reply(reply, request_id=7, v2=True)


def test_encodes_smart_life_sd_recording_thumbnail_command():
    packet = encode_thumbnail_request(
        request_id=9, channel=0, start=100, end=110
    )

    assert header(packet) == (0x12345678, 9, 0, 3, 8, 48)
    assert struct.unpack_from("<4i", packet, 20) == (0, 100, 110, 0)
    assert packet[36:] == bytes(32)


def test_decodes_smart_life_sd_recording_thumbnail_jpeg():
    jpeg = b"\xff\xd8image\xff\xd9"
    payload = bytearray(56)
    struct.pack_into("<III", payload, 44, 3, 0, len(jpeg))
    reply = NativeSimpleCommand(9, 1, 3, 8, bytes(payload) + jpeg)

    assert decode_thumbnail_reply(reply, request_id=9) == jpeg


def test_decodes_thumbnail_callback_without_requiring_echoed_command_ids():
    jpeg = b"\xff\xd8image\xff\xd9"
    payload = bytearray(56)
    struct.pack_into("<III", payload, 44, 3, 0, len(jpeg))
    reply = NativeSimpleCommand(9, 1, 0, 0, bytes(payload) + jpeg)

    assert decode_thumbnail_reply(reply, request_id=9) == jpeg


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (NativeSimpleCommand(8, 1, 3, 8, bytes(56)), "does not match"),
        (NativeSimpleCommand(9, 0, 3, 8, bytes(56)), "does not match"),
        (NativeSimpleCommand(9, 1, 3, 8, bytes(55)), "truncated"),
        (
            NativeSimpleCommand(
                9, 1, 3, 8, bytes(44) + struct.pack("<III", 2, 0, 0)
            ),
            "not a JPEG",
        ),
        (
            NativeSimpleCommand(
                9,
                1,
                100,
                11,
                bytes(44) + struct.pack("<III", 3, 0, 99) + b"\xff\xd8x\xff\xd9",
            ),
            "length",
        ),
    ],
)
def test_rejects_invalid_apk_thumbnail_replies(reply, message):
    with pytest.raises(NativeSimpleError, match=message):
        decode_thumbnail_reply(reply, request_id=9)


def test_encodes_apk_playback_and_audio_commands_on_combined_stream():
    stream_id = playback_stream_id(task_id=1, request_id=3)
    playback = encode_playback_start(
        stream_id=stream_id, channel=0, start=100, end=200, play_time=110
    )
    audio = encode_audio_open(stream_id=stream_id, channel=0)

    assert stream_id == 0x00010003
    assert header(playback) == (0x12345678, stream_id, 0, 7, 0, 20)
    assert struct.unpack_from("<5i", playback, 20) == (0, 0, 100, 200, 110)
    assert header(audio) == (0x12345678, stream_id, 0, 7, 4, 8)
    assert struct.unpack_from("<2i", audio, 20) == (0, 4)


def test_encodes_apk_playback_stop_commands_in_native_order():
    stream_id = playback_stream_id(task_id=2, request_id=8)

    media_stop, playback_stop = encode_playback_stop(
        stream_id=stream_id, channel=0
    )

    assert header(media_stop) == (0x12345678, stream_id, 0, 7, 3, 20)
    assert struct.unpack_from("<5i", media_stop, 20) == (0, 3, 0, 0, 0)
    assert header(playback_stop) == (0x12345678, stream_id, 0, 7, 5, 8)
    assert struct.unpack_from("<2i", playback_stop, 20) == (0, 5)


def test_encodes_apk_playback_pause_and_resume_commands():
    stream_id = playback_stream_id(task_id=2, request_id=8)

    pause = encode_playback_pause(stream_id=stream_id, channel=0)
    resume = encode_playback_resume(stream_id=stream_id, channel=0)

    assert header(pause) == (0x12345678, stream_id, 0, 7, 1, 20)
    assert struct.unpack_from("<5i", pause, 20) == (0, 1, 0, 0, 0)
    assert header(resume) == (0x12345678, stream_id, 0, 7, 2, 20)
    assert struct.unpack_from("<5i", resume, 20) == (0, 2, 0, 0, 0)


def test_encodes_apk_encrypted_playback_v2_command():
    stream_id = playback_stream_id(task_id=2, request_id=8)

    command = encode_playback_start(
        stream_id=stream_id,
        channel=0,
        start=100,
        end=200,
        play_time=125,
        encrypted=True,
    )

    assert header(command) == (0x12345678, stream_id, 0, 100, 20, 20)
    assert struct.unpack_from("<5i", command, 20) == (0, 0, 100, 200, 125)


def test_encodes_apk_fragment_list_playback_command():
    stream_id = playback_stream_id(task_id=2, request_id=8)

    command = encode_playback_start_fragments(
        stream_id=stream_id,
        channel=0,
        play_time=125,
        fragments_json=(
            '{"fragments":[{"start":100,"end":140},'
            '{"start":160,"end":180}]}'
        ),
    )

    assert header(command) == (0x12345678, stream_id, 0, 7, 21, 36)
    assert struct.unpack_from("<5i", command, 20) == (0, 21, 0, 125, 2)
    assert struct.unpack_from("<4i", command, 40) == (100, 140, 160, 180)


def test_encodes_apk_preview_stop_commands_in_native_order():
    stream_id = playback_stream_id(task_id=1, request_id=8)

    video_stop, audio_stop = encode_preview_stop(
        stream_id=stream_id, channel=0
    )

    assert [header(item) for item in (video_stop, audio_stop)] == [
        (0x12345678, stream_id, 0, 6, 3, 8),
        (0x12345678, stream_id, 0, 6, 5, 8),
    ]
    assert struct.unpack_from("<2i", video_stop, 20) == (0, 3)
    assert struct.unpack_from("<2i", audio_stop, 20) == (0, 5)


def test_decoder_handles_partial_and_coalesced_commands():
    first = encode_audio_open(stream_id=0x10002, channel=0)
    second = encode_catalog_request(
        request_id=3, channel=0, day=date(2026, 9, 18)
    )
    decoder = NativeSimpleCommandDecoder()

    assert decoder.feed(first[:13]) == ()
    commands = decoder.feed(first[13:] + second)

    assert [(item.high_command, item.low_command) for item in commands] == [
        (7, 4),
        (3, 1),
    ]


def test_decodes_apk_camel_case_catalog_items():
    payload = json.dumps(
        {
            "items": [
                {
                    "startTime": 100,
                    "endTime": 110,
                    "eventType": 2,
                    "videoType": 1,
                    "encrypt": 0,
                    "uuid": "clip",
                    "encryptMD5": "digest",
                }
            ]
        }
    ).encode()
    reply = NativeSimpleCommand(2, 1, 3, 1, payload)

    page = decode_catalog_reply(reply, request_id=2)

    assert page.items[0].as_recording() == {
        "start_time": 100,
        "end_time": 110,
        "event_type": 2,
        "video_type": 1,
        "encrypt": 0,
        "uuid": "clip",
        "encrypt_md5": "digest",
    }


def test_decodes_apk_v1_binary_catalog_items():
    payload = bytearray(0x18 + 2 * 12)
    struct.pack_into("<I", payload, 0x10, 2)
    struct.pack_into("<III", payload, 0x14, (2 << 16) | 1, 100, 110)
    struct.pack_into("<III", payload, 0x20, (4 << 16) | 3, 120, 135)
    reply = NativeSimpleCommand(2, 1, 3, 1, bytes(payload))

    page = decode_catalog_reply(reply, request_id=2)

    assert [item.as_recording() for item in page.items] == [
        {
            "start_time": 100,
            "end_time": 110,
            "event_type": 2,
            "video_type": 1,
            "encrypt": 0,
            "uuid": "",
            "encrypt_md5": "",
        },
        {
            "start_time": 120,
            "end_time": 135,
            "event_type": 4,
            "video_type": 3,
            "encrypt": 0,
            "uuid": "",
            "encrypt_md5": "",
        },
    ]


def test_decodes_camera_v1_catalog_header_and_record_order():
    payload = bytearray(0x18 + 2 * 12)
    struct.pack_into("<5I", payload, 0, 0, 2026, 9, 15, 2)
    struct.pack_into("<III", payload, 0x14, 0, 1789476672, 1789476710)
    struct.pack_into("<III", payload, 0x20, 0, 1789476849, 1789476885)
    reply = NativeSimpleCommand(8, 1, 3, 1, bytes(payload))

    page = decode_catalog_reply(reply, request_id=8)

    assert [(item.start_time, item.end_time) for item in page.items] == [
        (1789476672, 1789476710),
        (1789476849, 1789476885),
    ]


def test_decodes_apk_v1_extended_binary_catalog_envelope():
    payload = bytearray(0x58 + 12)
    struct.pack_into("<I", payload, 0x50, 1)
    struct.pack_into("<III", payload, 0x54, (5 << 16) | 2, 200, 220)
    reply = NativeSimpleCommand(7, 1, 3, 1, bytes(payload))

    page = decode_catalog_reply(reply, request_id=7)

    assert page.items[0].start_time == 200
    assert page.items[0].end_time == 220


def test_decodes_apk_v2_binary_catalog_items():
    payload = bytearray(0x14 + 0x40)
    struct.pack_into("<5I", payload, 0, 0, 2026, 9, 18, 1)
    struct.pack_into("<HH", payload, 0x14, 2, 7)
    payload[0x18:0x1F] = b"clip-v2"
    struct.pack_into("<III", payload, 0x38, 100, 110, 0)
    reply = NativeSimpleCommand(2, 1, 3, 4, bytes(payload))

    page = decode_catalog_reply(reply, request_id=2, version=1)

    assert page.items[0].as_recording() == {
        "start_time": 100,
        "end_time": 110,
        "event_type": 7,
        "video_type": 2,
        "encrypt": 0,
        "uuid": "clip-v2",
        "encrypt_md5": "",
    }


def test_decodes_apk_v3_binary_catalog_items_with_variable_event_types():
    payload = bytearray(0x14 + 0x48 + 8)
    struct.pack_into("<5I", payload, 0, 0, 2026, 9, 18, 1)
    struct.pack_into("<II", payload, 0x14, 3, 9)
    payload[0x1C:0x23] = b"clip-v3"
    struct.pack_into("<III", payload, 0x3C, 200, 230, 0)
    struct.pack_into("<I2I", payload, 0x58, 2, 4, 6)
    reply = NativeSimpleCommand(2, 1, 3, 7, bytes(payload))

    page = decode_catalog_reply(reply, request_id=2, version=2)

    assert page.items[0].as_recording() == {
        "start_time": 200,
        "end_time": 230,
        "event_type": 9,
        "video_type": 3,
        "encrypt": 0,
        "uuid": "clip-v3",
        "encrypt_md5": "",
        "event_types": [4, 6],
    }


def test_rejects_truncated_apk_v1_binary_catalog():
    payload = bytearray(0x18 + 11)
    struct.pack_into("<I", payload, 0x10, 1)
    reply = NativeSimpleCommand(2, 1, 3, 1, bytes(payload))

    with pytest.raises(NativeSimpleError, match="invalid V1 binary layout"):
        decode_catalog_reply(reply, request_id=2)


@pytest.mark.parametrize("task_id,request_id", [(0, 1), (1, 0), (0x10000, 1)])
def test_rejects_invalid_playback_stream_ids(task_id, request_id):
    with pytest.raises(NativeSimpleError):
        playback_stream_id(task_id=task_id, request_id=request_id)
