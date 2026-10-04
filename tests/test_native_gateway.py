import json

import pytest
from custom_components.tuya_recordings.lib.native_auth import NativeAppSession
from custom_components.tuya_recordings.lib.native_gateway import (
    COMPOSITE_KEY,
    NativeAppGatewayClient,
    NativeGatewayAuthError,
    NativeGatewayProtocolError,
    _body_key,
    _dump_json,
    _encrypt_post_data,
    _wire_api_name,
    generate_device_fingerprint,
    native_app_profile,
)


def test_tuya_smart_profile_uses_its_own_current_mobile_identity():
    profile = native_app_profile("tuya_smart")

    assert profile.package_name == "com.tuya.smart"
    assert profile.app_version == "7.11.0"
    assert profile.qr_scheme == "thingSmart"
    assert profile.composite_key != COMPOSITE_KEY


def test_device_fingerprint_is_unique_urlsafe_and_sdk_sized():
    first = generate_device_fingerprint()
    second = generate_device_fingerprint()

    assert first != second
    assert len(first) == len(second) == 44
    assert all(character.isalnum() or character in "-_" for character in first)


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self, result=None, envelope=None):
        self.result = result or {"ok": True}
        self.envelope = envelope
        self.calls = []

    def post(self, url, *, data, headers, timeout):
        self.calls.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        if self.envelope is not None:
            return FakeResponse(json.dumps(self.envelope))
        key = _body_key(data["requestId"], "0123456789abcdef", COMPOSITE_KEY)
        encrypted = _encrypt_post_data(key, _dump_json({"success": True, "result": self.result}))
        return FakeResponse(json.dumps({"result": encrypted}))


def test_app_session_gateway_call_signs_encrypts_and_decrypts_response():
    session = FakeSession({"cameraInfo": {"id": "p2p-id"}})
    client = NativeAppGatewayClient(region="eu", session=session)

    result = client.call(
        "m.ipc.v4.rtc.config.get",
        "1.0",
        {"devId": "camera-id"},
        NativeAppSession(
            sid="sid-value",
            ecode="0123456789abcdef",
            uid="uid-value",
            partner_identity="partner",
            mobile_mqtts_url="mqtt.example.test",
            device_fingerprint="fingerprint",
        ),
    )

    call = session.calls[0]
    params = call["data"]
    assert call["url"] == "https://a1-eu.lifeaiot.com/api.json"
    assert params["a"] == "m.ipc.v4.rtc.config.get"
    assert params["v"] == "1.0"
    assert params["sid"] == "sid-value"
    assert params["et"] == "3"
    assert len(params["sign"]) == 64
    assert params["postData"] != json.dumps({"devId": "camera-id"})
    assert result == {"cameraInfo": {"id": "p2p-id"}}


def test_thing_api_names_are_rewritten_for_smartlife_wire_calls():
    assert _wire_api_name("thing.m.rtc.session.init") == "smartlife.m.rtc.session.init"
    assert _wire_api_name("m.ipc.v4.rtc.config.get") == "m.ipc.v4.rtc.config.get"


def test_saved_session_requires_complete_native_qr_authorization():
    client = NativeAppGatewayClient(session=FakeSession())

    with pytest.raises(NativeGatewayAuthError, match="incomplete"):
        client.call_with_saved_session("m.ipc.v4.rtc.config.get", "1.0", {}, {"sid": "sid", "ecode": "ecode"})


def test_saved_session_passes_extra_query_parameters(monkeypatch):
    client = NativeAppGatewayClient(session=FakeSession())
    captured = {}

    def fake_call(self, api, version, post, app_session=None, extra_params=None):
        captured.update(
            api=api,
            version=version,
            post=post,
            sid=app_session.sid,
            extra_params=extra_params,
        )
        return {"ok": True}

    monkeypatch.setattr(NativeAppGatewayClient, "call", fake_call)
    result = client.call_with_saved_session(
        "tuya.m.my.group.device.list",
        "1.0",
        {},
        {
            "sid": "sid",
            "ecode": "ecode",
            "uid": "uid",
            "partner_identity": "partner",
            "mobile_mqtts_url": "mqtt.example.test",
            "device_fingerprint": "fingerprint",
        },
        {"gid": "123"},
    )

    assert result == {"ok": True}
    assert captured == {
        "api": "tuya.m.my.group.device.list",
        "version": "1.0",
        "post": {},
        "sid": "sid",
        "extra_params": {"gid": "123"},
    }


def test_device_local_keys_uses_each_home_once(monkeypatch):
    client = NativeAppGatewayClient(session=FakeSession())
    calls = []

    def fake_call(self, api, version, post, app_session=None, extra_params=None):
        calls.append((api, version, post, extra_params))
        if api == "tuya.m.location.list":
            return [{"gid": 123}, {"gid": 123}, {"gid": "invalid"}]
        return [
            {
                "devId": "camera-a",
                "localKey": "fresh-a",
                "pv": "2.3",
            },
            {
                "devId": "camera-b",
                "localKey": "fresh-b",
                "moduleMap": {"wifi": {"pv": "invalid"}},
            },
            {"devId": "missing-key"},
        ]

    monkeypatch.setattr(NativeAppGatewayClient, "call", fake_call)
    keys = client.device_local_keys(
        {
            "sid": "sid",
            "ecode": "ecode",
            "uid": "uid",
            "partner_identity": "partner",
            "mobile_mqtts_url": "mqtt.example.test",
            "device_fingerprint": "fingerprint",
        }
    )

    assert keys == {"camera-a": "fresh-a", "camera-b": "fresh-b"}
    assert calls == [
        ("tuya.m.location.list", "2.1", {}, None),
        ("tuya.m.my.group.device.list", "1.0", {}, {"gid": "123"}),
    ]


def test_device_connection_data_extracts_apk_mqtt_protocol(monkeypatch):
    client = NativeAppGatewayClient(session=FakeSession())

    def fake_call(self, api, version, post, app_session=None, extra_params=None):
        if api == "tuya.m.location.list":
            return [{"gid": 123}]
        return [
            {
                "devId": "camera-a",
                "localKey": "fresh-a",
                "protocol_version": "3.5",
                "pv": "2.3",
                "moduleMap": {"wifi": {"pv": "9.9"}},
            }
        ]

    monkeypatch.setattr(NativeAppGatewayClient, "call", fake_call)

    keys, protocols = client.device_connection_data(
        {
            "sid": "sid",
            "ecode": "ecode",
            "uid": "uid",
            "partner_identity": "partner",
            "mobile_mqtts_url": "mqtt.example.test",
            "device_fingerprint": "fingerprint",
        }
    )

    assert keys == {"camera-a": "fresh-a"}
    assert protocols == {"camera-a": "2.3"}


@pytest.mark.parametrize("homes", [None, {}, "invalid"])
def test_device_local_keys_rejects_malformed_home_list(monkeypatch, homes):
    client = NativeAppGatewayClient(session=FakeSession())
    monkeypatch.setattr(
        NativeAppGatewayClient,
        "call",
        lambda self, *args, **kwargs: homes,
    )

    with pytest.raises(NativeGatewayProtocolError, match="home list"):
        client.device_local_keys(
            {
                "sid": "sid",
                "ecode": "ecode",
                "uid": "uid",
                "partner_identity": "partner",
                "mobile_mqtts_url": "mqtt.example.test",
                "device_fingerprint": "fingerprint",
            }
        )


def test_session_invalid_error_maps_to_auth_error():
    client = NativeAppGatewayClient(
        session=FakeSession(envelope={"success": False, "errorCode": "USER_SESSION_INVALID", "errorMsg": "expired"})
    )

    with pytest.raises(NativeGatewayAuthError, match="USER_SESSION_INVALID"):
        client.call(
            "m.ipc.v4.rtc.config.get",
            "1.0",
            {},
            NativeAppSession(
                sid="sid",
                ecode="0123456789abcdef",
                uid="uid",
                partner_identity="partner",
                mobile_mqtts_url="mqtt.example.test",
                device_fingerprint="fingerprint",
            ),
        )


def test_malformed_encrypted_response_is_protocol_error():
    client = NativeAppGatewayClient(session=FakeSession(envelope={"result": "not-base64"}))

    with pytest.raises(NativeGatewayProtocolError):
        client.call(
            "m.ipc.v4.rtc.config.get",
            "1.0",
            {},
            NativeAppSession(
                sid="sid",
                ecode="0123456789abcdef",
                uid="uid",
                partner_identity="partner",
                mobile_mqtts_url="mqtt.example.test",
                device_fingerprint="fingerprint",
            ),
        )
