from datetime import date
import json

import pytest

from custom_components.tuya_recordings.lib.native_v4 import (
    NativeV4Error,
    NativeV4Recording,
    NativeV4RequestContext,
    decode_recordings_day_response,
    encode_business_enable_request,
    encode_connection_offer,
    encode_playback_start_request,
    encode_recordings_day_request,
)


def context(**changes):
    values = {
        "request_id": 17,
        "timestamp_ms": 1_789_000_000_123,
        "client_id": "mobile-client",
        "session_id": 42,
        "source": "viewer-id",
        "destination": "camera-id",
        "username": "admin",
        "password": "derived-secret",
    }
    values.update(changes)
    return NativeV4RequestContext(**values)


def test_playback_start_matches_apk_registered_request_fields():
    payload = json.loads(
        encode_playback_start_request(
            context(), stream_id=3, start=100, end=160, play_time=105
        )
    )

    assert payload == {
        "audio": True,
        "chan_id": 3,
        "channel": 0,
        "cid": "mobile-client",
        "day": 0,
        "end_time": 160,
        "f": 0,
        "fragments": [],
        "from": "viewer-id",
        "mode": 1,
        "month": 0,
        "operation": "start",
        "passwd": "derived-secret",
        "play_time": 105,
        "reqid": 17,
        "sid": 42,
        "speed": 1,
        "start_time": 100,
        "t": 1_789_000_000_123,
        "to": "camera-id",
        "type": "playback.record.view.control",
        "username": "admin",
        "v": 1,
        "video": True,
        "year": 0,
    }


def test_playback_start_defaults_position_and_keeps_password_out_of_repr():
    request_context = context()

    payload = json.loads(
        encode_playback_start_request(request_context, stream_id=1, start=100, end=200)
    )

    assert payload["play_time"] == 100
    assert payload["audio"] is payload["video"] is True
    assert "derived-secret" not in repr(request_context)


@pytest.mark.parametrize(
    ("changes", "kwargs", "message"),
    [
        ({"password": ""}, {}, "password"),
        ({"request_id": -1}, {}, "request_id"),
        ({}, {"stream_id": -1}, "stream_id"),
        ({}, {"start": 200, "end": 100}, "after start"),
        ({}, {"start": 100, "end": 200, "play_time": 200}, "inside clip"),
    ],
)
def test_playback_start_rejects_invalid_wire_values(changes, kwargs, message):
    values = {"stream_id": 1, "start": 100, "end": 200, **kwargs}
    with pytest.raises(NativeV4Error, match=message):
        encode_playback_start_request(context(**changes), **values)


def test_recordings_day_request_matches_apk_registered_fields():
    payload = json.loads(encode_recordings_day_request(context(), date(2026, 9, 17)))

    assert payload == {
        "channel": 0,
        "cid": "mobile-client",
        "day": 17,
        "f": 0,
        "from": "viewer-id",
        "mode": 1,
        "month": 9,
        "passwd": "derived-secret",
        "reqid": 17,
        "sid": 42,
        "t": 1_789_000_000_123,
        "to": "camera-id",
        "type": "playback.record.query.day",
        "username": "admin",
        "v": 1,
        "year": 2026,
    }


def test_business_enable_matches_apk_connect_handshake():
    payload = json.loads(encode_business_enable_request(context()))

    assert payload["type"] == "bizcommand.enable"
    assert payload["ch_id"] == 9
    assert payload["ch_type"] == 2


def test_connection_offer_matches_native_thing_create_offer_shape():
    payload = json.loads(
        encode_connection_offer(
            client_id="local-cid",
            source="account-uid",
            destination="camera-cid",
            p2p_config={
                "username": "token-user",
                "expired": 123,
                "crypt_algo": 1,
                "crypt_key": "crypt",
                "sign_algo": 2,
                "sign_key": "sign",
                "ice_token": {"servers": [{"url": "stun:test"}]},
                "motoId": "moto",
            },
            sdp="v=0\r\nm=application sts\r\n",
            ice_ufrag="ufrag",
            ice_password="password",
            candidates=("candidate:1 1 udp 1 127.0.0.1 9 typ host",),
            tcp_token={
                "urls": ["tcp4:127.0.0.1:1443"],
                "username": "123456:relay-user",
                "credential": "relay-credential",
            },
        )
    )
    assert payload["av"] == 4
    assert "v" not in payload
    assert payload["type"] == "offer"
    assert payload["cid"] == "local-cid"
    assert payload["from"] == "account-uid"
    assert payload["to"] == "camera-cid"
    assert payload["username"] == "token-user"
    assert "passwd" not in payload
    assert payload["ice_token"]["ufrag"] == "ufrag"
    assert payload["tcp_token"] == {
        "urls": ["tcp4:127.0.0.1:1443"],
        "key": "relay-credential"[:16],
        "username": "123456:relay-user",
        "credential": "relay-credential",
        "security_level": 3,
        "expire_time": 123456,
    }
    assert payload["moto_id"] == "moto"


def test_recordings_day_response_matches_apk_registered_fields():
    response = decode_recordings_day_response(
        json.dumps(
            {
                "type": "playback.record.query.day.resp",
                "respid": 17,
                "status": 0,
                "errmsg": "",
                "total_page": 1,
                "total_file": 1,
                "page": 1,
                "items": [
                    {
                        "start_time": 100,
                        "end_time": 160,
                        "event_type": 2,
                        "video_type": 4,
                        "encrypt": 1,
                        "uuid": "recording-id",
                        "encrypt_md5": "digest",
                    }
                ],
            }
        ).encode(),
        request_id=17,
    )

    assert response.total_files == 1
    assert response.items == (
        NativeV4Recording(100, 160, 2, 4, 1, "recording-id", "digest"),
    )
    assert response.items[0].as_recording()["uuid"] == "recording-id"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"type": "wrong"}, "wrong type"),
        ({"respid": 18}, "does not match"),
        ({"status": 9, "errmsg": "camera busy"}, "camera busy"),
        ({"items": [{"start_time": 160, "end_time": 100}]}, "after start_time"),
    ],
)
def test_recordings_day_response_rejects_invalid_wire_data(change, message):
    value = {
        "type": "playback.record.query.day.resp",
        "respid": 17,
        "status": 0,
        "errmsg": "",
        "total_page": 1,
        "total_file": 1,
        "page": 1,
        "items": [{"start_time": 100, "end_time": 160}],
    }
    value.update(change)

    with pytest.raises(NativeV4Error, match=message):
        decode_recordings_day_response(json.dumps(value).encode(), request_id=17)
