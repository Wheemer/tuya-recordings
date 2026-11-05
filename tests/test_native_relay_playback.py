import asyncio
import json
import struct
from dataclasses import replace
from datetime import date

import pytest
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.crypto import (
    decrypt_record,
    encrypt_record,
)

import custom_components.tuya_recordings.lib.native_relay_playback as relay_module
from custom_components.tuya_recordings.lib.native_playback_stream import (
    PLAYBACK_CODEC_INFO_BYTES,
)
from custom_components.tuya_recordings.lib.native_relay_playback import (
    NativeRelayPlaybackError,
    NativeRelayPlaybackSession,
)
from custom_components.tuya_recordings.lib.native_simple import NativeSimpleCommand
from custom_components.tuya_recordings.lib.native_session import (
    NativeCameraSessionConfig,
)

OFFER_KEY = bytes(range(16))
ANSWER_KEY = bytes(range(16, 32))
APP_SESSION = {
    "sid": "sid",
    "ecode": "ecode",
    "uid": "account-uid",
    "partner_identity": "p1000018",
    "mobile_mqtts_url": "ssl://m1.tuyaus.com:8884",
    "device_fingerprint": "fingerprint",
}


def camera_config():
    p2p_config = {
        "expire": 4102444800,
        "session": {
            "sessionId": "session-id",
            "aesKey": OFFER_KEY.hex(),
            "iceUfrag": "legacy-ufrag",
            "icePassword": "legacy-password",
            "traceId": "trace",
            "uid": "account-uid",
        },
        "tcpRelay": {
            "urls": ["tcp4:relay.example.test:1443"],
            "username": "123456:relay-user",
            "credential": "relay-credential",
            "sessionId": "server-session",
        },
        "motoId": "camera-moto-id",
        "ices": [{"urls": "stun:token.example.test"}],
        "securityLevel": 3,
    }
    camera_info = {
        "password": "camera-password",
        "motoId": "camera-moto-id",
        "p2pConfig": p2p_config,
    }
    return NativeCameraSessionConfig(
        dev_id="camera-device",
        local_key="0123456789abcdef",
        camera_password="camera-password",
        token=json.dumps(p2p_config),
        skill="{}",
        p2p_type=4,
        trace_id="trace",
        p2p_id="camera-device",
        local_id="account-uid",
        config_json=json.dumps(camera_info),
        app_sid="sid",
        app_ecode="ecode",
        app_uid="account-uid",
        mqtt_protocol_version="2.3",
    )


def session():
    return NativeRelayPlaybackSession(camera_config(), APP_SESSION)


@pytest.mark.asyncio
async def test_initial_http_token_precedes_single_mqtt_offer(monkeypatch):
    calls = []
    captured = {}
    config = camera_config()
    token = json.loads(config.token)
    token["expire"] = 0
    config = replace(config, token=json.dumps(token))

    def gateway(api, version, body, extra):
        calls.append("http")
        assert (api, version, extra) == ("thing.m.rtc.session.offer", "1.0", {})
        assert body["api"] == "thing.m.rtc.session.offer_1.0"
        assert body["devId"] == config.dev_id
        message = json.loads(body["msg"])
        assert set(message["msg"]) == {"sdp"}
        captured.update(message)
        result = json.loads(camera_config().token)
        result["session"]["sessionId"] = message["header"]["sessionid"]
        result["motoId"] = "renewed-moto"
        return {"p2pConfig": result}

    class Signaling:
        def __init__(self, **kwargs):
            captured["signaling"] = kwargs

        async def async_connect(self):
            calls.append("mqtt")

        def configure_offer_metadata(self, **kwargs):
            captured["signaling"].update(kwargs)

        async def async_send_sig_query(self):
            calls.append("query")

        async def async_send_offer(self, sdp, *args):
            calls.append("offer")
            assert sdp == captured["msg"]["sdp"]

    monkeypatch.setattr(relay_module, "MotoClient", Signaling)
    playback = NativeRelayPlaybackSession(config, APP_SESSION, call_api=gateway)
    await playback._open_signaling("old-sdp", [], "trace", {}, {})
    assert calls == ["mqtt", "http", "query", "offer"]
    assert captured["signaling"]["session_id"] == captured["header"]["sessionid"]
    assert captured["signaling"]["moto_id"] == "renewed-moto"
    assert playback.local_cid != camera_config().p2p_session_id
    assert playback._offer.sdp == captured["msg"]["sdp"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["gateway", "missing", "session", "cloud"])
async def test_initial_http_failure_never_sends_mqtt_offer_or_retries(monkeypatch, failure):
    calls = []
    config = camera_config()
    token = json.loads(config.token)
    token["skill"] = {"cloudP2P": 1}
    config = replace(config, token=json.dumps(token))

    def gateway(api, version, body, extra):
        calls.append("http")
        if failure == "gateway":
            raise OSError("request failed")
        if failure == "missing":
            return {}
        result = json.loads(camera_config().token)
        if failure == "cloud":
            result["session"]["sessionId"] = json.loads(body["msg"])["header"]["sessionid"]
            result["isCloudSession"] = True
        return {"p2pConfig": result}

    playback = NativeRelayPlaybackSession(config, APP_SESSION, call_api=gateway)
    from unittest.mock import AsyncMock
    monkeypatch.setattr(playback, "_connect_mqtt", AsyncMock())
    with pytest.raises((NativeRelayPlaybackError, OSError)):
        await playback._open_signaling("sdp", [], "trace", {}, {})
    assert calls == ["http"]


def test_current_app_token_does_not_require_http_offer():
    assert not session()._requires_http_token()


@pytest.mark.asyncio
async def test_http_initialization_accepts_bootstrap_without_preissued_ice_keys(monkeypatch):
    config = camera_config()
    info = json.loads(config.config_json)
    info["p2pConfig"] = {"expire": 0}
    config = replace(config, token='{"expire":0}', config_json=json.dumps(info))
    requests = []

    def gateway(api, version, body, extra):
        message = json.loads(body["msg"])
        requests.append(message)
        token = json.loads(camera_config().token)
        token["session"] = {"sessionId": message["header"]["sessionid"]}
        return {"p2pConfig": token}

    playback = NativeRelayPlaybackSession(config, APP_SESSION, call_api=gateway)
    from unittest.mock import AsyncMock
    monkeypatch.setattr(playback, "_connect_mqtt", AsyncMock())
    assert playback._stream_config is None
    await playback._initialize_http_offer("trace")
    assert len(requests) == 1
    assert playback._http_initialized
    assert not playback._requires_http_token()
    local = playback._stream_config.p2p_session
    assert len(local.ice_ufrag) == 4
    assert len(local.ice_password) == 24
    assert len(local.aes_key) == 16
    assert f"a=aes-key:{local.aes_key.hex()}" in requests[0]["msg"]["sdp"]
    assert playback._stream_config.relay_token.session_id == "server-session"


@pytest.mark.asyncio
async def test_cancelled_http_initialization_joins_gateway_before_returning(monkeypatch):
    import threading

    entered, release = threading.Event(), threading.Event()

    def gateway(*args):
        entered.set()
        assert release.wait(2)
        return {}

    playback = NativeRelayPlaybackSession(camera_config(), APP_SESSION, call_api=gateway)
    from unittest.mock import AsyncMock
    monkeypatch.setattr(playback, "_connect_mqtt", AsyncMock())
    task = asyncio.create_task(playback._initialize_http_offer("trace"))
    try:
        await asyncio.to_thread(entered.wait, 2)
        assert entered.is_set()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert playback._moto is None
        assert not playback._http_initialized
    finally:
        release.set()


@pytest.mark.asyncio
async def test_http_initialization_timeout_joins_request_without_retry(monkeypatch):
    import threading
    from unittest.mock import AsyncMock

    release, finished = threading.Event(), threading.Event()
    calls = []

    def gateway(*args):
        calls.append("http")
        assert release.wait(2)
        finished.set()
        return {}

    playback = NativeRelayPlaybackSession(camera_config(), APP_SESSION, call_api=gateway)
    monkeypatch.setattr(playback, "_connect_mqtt", AsyncMock())
    monkeypatch.setattr(relay_module, "_HTTP_TOKEN_TIMEOUT_SECONDS", 0.01)
    timer = asyncio.get_running_loop().call_later(0.04, release.set)
    try:
        with pytest.raises(NativeRelayPlaybackError, match="HTTP offer timed out"):
            await playback._initialize_http_offer("trace")
        assert finished.is_set()
        assert calls == ["http"]
        assert not playback._http_initialized
    finally:
        release.set()
        timer.cancel()


def answer_sdp():
    return (
        "v=0\r\n"
        "m=application 9 imm 6001\r\n"
        "a=ice-ufrag:camera-ufrag\r\n"
        "a=ice-pwd:camera-password\r\n"
        f"a=aes-key:{ANSWER_KEY.hex()}\r\n"
        "a=rtpmap:6001 AES/KCP 330\r\n"
    )


def test_maps_camera_config_without_constructing_cloud_transport():
    playback = session()

    assert playback.local_cid != "session-id"
    assert playback._session is None


def test_cached_app_bootstrap_never_reuses_local_signaling_credentials():
    config = camera_config()
    info = json.loads(config.config_json)
    info["p2pConfig"].pop("session")
    config = replace(config, config_json=json.dumps(info))
    first = NativeRelayPlaybackSession(config, APP_SESSION)
    second = NativeRelayPlaybackSession(config, APP_SESSION)
    a, b = first._stream_config.p2p_session, second._stream_config.p2p_session
    assert first.local_cid == a.session_id != b.session_id == second.local_cid
    assert a.aes_key != b.aes_key
    assert a.ice_password != b.ice_password
    assert len(a.ice_ufrag) == 4


def test_rejects_v4_camera_before_opening_transport():
    config = replace(camera_config(), p2p_type=8)
    with pytest.raises(NativeRelayPlaybackError, match="P2P type 4"):
        NativeRelayPlaybackSession(config, APP_SESSION)


def test_construction_does_not_open_camera_or_schedule_background_work():
    playback = session()

    assert playback._session is None
    assert playback._candidate_tasks == set()
    assert not playback._connected


def test_broker_reader_failure_is_not_misreported_as_silent_camera():
    async def run():
        playback = session()
        playback._answer = asyncio.get_running_loop().create_future()
        playback._ended = asyncio.get_running_loop().create_future()
        playback._on_signaling_failure("ConnectionError")
        with pytest.raises(NativeRelayPlaybackError, match="MQTT signaling stopped"):
            await playback._wait_for_answer()
        assert playback._ended.done()
    asyncio.run(run())


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_signaling_prefers_lan_for_apk_local_policy(monkeypatch, expired):
    calls = []

    class LocalSignal:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        async def async_connect(self):
            calls.append(("connect",))

        async def async_send_offer(self, *args):
            calls.append(("offer", args))

        async def async_close(self):
            calls.append(("close",))

    class UnexpectedMqtt:
        def __init__(self, **kwargs):
            raise AssertionError("MQTT must not be selected when LAN succeeds")

    monkeypatch.setattr(relay_module, "Lan302Client", LocalSignal)
    monkeypatch.setattr(relay_module, "MotoClient", UnexpectedMqtt)
    config = replace(camera_config(), p2p_policy=1)
    if expired:
        token = json.loads(config.token)
        token["expire"] = 0
        config = replace(config, token=json.dumps(token))
    playback = NativeRelayPlaybackSession(config, APP_SESSION)
    await playback._open_signaling("sdp", [], "trace", {}, {})

    assert [call[0] for call in calls] == ["init", "connect", "offer"]
    assert playback._signaling_path == "lan"


@pytest.mark.asyncio
async def test_signaling_uses_mqtt_only_after_lan_is_unavailable(monkeypatch):
    calls = []

    class UnavailableLan:
        def __init__(self, **kwargs):
            pass

        async def async_connect(self):
            raise relay_module.NativeLanUnavailableError("not local")

        async def async_close(self):
            calls.append("lan-close")

    class MqttFallback:
        def __init__(self, **kwargs):
            calls.append(("mqtt-init", kwargs))

        async def async_connect(self):
            calls.append("mqtt-connect")

        async def async_send_sig_query(self):
            calls.append("mqtt-query")

        async def async_send_offer(self, *args):
            calls.append("mqtt-offer")

    monkeypatch.setattr(relay_module, "Lan302Client", UnavailableLan)
    monkeypatch.setattr(relay_module, "MotoClient", MqttFallback)
    playback = NativeRelayPlaybackSession(
        replace(camera_config(), p2p_policy=1), APP_SESSION
    )
    await playback._open_signaling("sdp", [], "trace", {}, {})

    assert [call[0] if isinstance(call, tuple) else call for call in calls] == [
        "lan-close",
        "mqtt-init",
        "mqtt-connect",
        "mqtt-query",
        "mqtt-offer",
    ]
    assert calls[1][1]["moto_id"] == "camera-moto-id"
    assert playback._signaling_path == "mqtt"
    assert "lan=unavailable, offer=published, answer=not_received" in playback.transport_diagnostics


@pytest.mark.asyncio
async def test_signaling_does_not_send_mqtt_offer_after_lan_offer_failure(monkeypatch):
    calls = []

    class FailingLan:
        def __init__(self, **kwargs):
            pass

        async def async_connect(self):
            calls.append("lan-connect")

        async def async_send_offer(self, *args):
            calls.append("lan-offer")
            raise relay_module.NativeLanSignalError("camera did not answer")

        async def async_close(self):
            calls.append("lan-close")

    class UnexpectedMqtt:
        def __init__(self, **kwargs):
            raise AssertionError("MQTT must not follow a LAN offer failure")

    monkeypatch.setattr(relay_module, "Lan302Client", FailingLan)
    monkeypatch.setattr(relay_module, "MotoClient", UnexpectedMqtt)
    playback = NativeRelayPlaybackSession(
        replace(camera_config(), p2p_policy=1), APP_SESSION
    )

    with pytest.raises(NativeRelayPlaybackError, match="local P2P"):
        await playback._open_signaling("sdp", [], "trace", {}, {})

    assert calls == ["lan-connect", "lan-offer", "lan-close"]


@pytest.mark.asyncio
async def test_failed_lan_close_keeps_handle_and_forbids_mqtt_fallback(monkeypatch):
    class BrokenLan:
        def __init__(self, **kwargs):
            pass

        async def async_connect(self):
            raise relay_module.NativeLanUnavailableError("not local")

        async def async_close(self):
            raise OSError("socket close failed")

    def unexpected_mqtt(**kwargs):
        pytest.fail("MQTT must not start while LAN cleanup is incomplete")

    monkeypatch.setattr(relay_module, "Lan302Client", BrokenLan)
    monkeypatch.setattr(relay_module, "MotoClient", unexpected_mqtt)
    playback = NativeRelayPlaybackSession(
        replace(camera_config(), p2p_policy=1), APP_SESSION
    )
    with pytest.raises(OSError, match="socket close failed"):
        await playback._open_signaling("sdp", [], "trace", {}, {})
    assert isinstance(playback._moto, BrokenLan)


@pytest.mark.asyncio
async def test_close_does_not_wait_forever_for_transport_cleanup(monkeypatch):
    class HangingSession:
        async def async_close(self):
            await asyncio.Event().wait()

    class HangingMoto:
        async def async_send_disconnect(self):
            await asyncio.Event().wait()

        async def async_close(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(relay_module, "_CLOSE_STEP_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(relay_module, "_DISCONNECT_GRACE_SECONDS", 0)
    playback = session()
    playback._session = HangingSession()
    playback._moto = HangingMoto()

    with pytest.raises(NativeRelayPlaybackError, match="cleanup incomplete"):
        await asyncio.wait_for(playback.close(), timeout=0.2)

    assert playback._closed is True
    assert playback._session is not None
    assert playback._moto is not None


@pytest.mark.asyncio
async def test_failed_transport_cleanup_retains_ownership_and_can_be_retried(monkeypatch):
    calls = []

    class RetryTransport:
        async def async_close(self):
            calls.append("close")
            if calls.count("close") == 1:
                raise RuntimeError("failed close")

    class Signaling:
        async def async_send_disconnect(self):
            calls.append("disconnect")

        async def async_close(self):
            calls.append("signaling closed")

    monkeypatch.setattr(relay_module, "_DISCONNECT_GRACE_SECONDS", 0)
    playback = session()
    transport = RetryTransport()
    playback._session = transport
    playback._moto = Signaling()
    with pytest.raises(NativeRelayPlaybackError, match="direct session close failed"):
        await playback.close()
    assert playback._session is transport
    assert playback._moto is None
    assert calls == ["close", "disconnect", "signaling closed"]
    await playback.close()
    assert playback._session is None
    assert calls.count("close") == 2


def test_download_thumbnail_uses_smart_life_event_image_command_without_playback():
    playback = session()
    image = b"\xff\xd8image\xff\xd9"
    payload = bytearray(56)
    struct.pack_into("<III", payload, 44, 3, 0, len(image))
    sent = []
    playback._send_control = sent.append

    async def run():
        playback._connected = True
        playback._control = object()
        playback._ended = asyncio.get_running_loop().create_future()
        playback._control_messages.put_nowait(
            NativeSimpleCommand(8, 1, 3, 8, bytes(payload) + image)
        )
        return await playback.download_thumbnail(start=100, end=110)

    result = asyncio.run(run())

    assert result == image
    assert len(sent) == 1
    magic, request_id, control, high, low, length = struct.unpack_from(
        "<IIIHHI", sent[0]
    )
    assert (magic, request_id, control, high, low, length) == (
        0x12345678,
        8,
        0,
        3,
        8,
        48,
    )
    assert struct.unpack_from("<4I", sent[0], 20) == (0, 100, 110, 0)
    assert sent[0][36:] == bytes(32)
    assert playback._playback_started is False


def test_low_level_event_image_command_does_not_duplicate_client_capability_gate():
    config = replace(camera_config(), skill=json.dumps({"localStorage": 268435456}))
    playback = NativeRelayPlaybackSession(config, APP_SESSION)
    image = b"\xff\xd8image\xff\xd9"
    payload = bytearray(56)
    struct.pack_into("<III", payload, 44, 3, 0, len(image))
    sent = []
    playback._send_control = sent.append

    async def run():
        playback._connected = True
        playback._control = object()
        playback._ended = asyncio.get_running_loop().create_future()
        playback._control_messages.put_nowait(
            NativeSimpleCommand(8, 1, 3, 8, bytes(payload) + image)
        )
        return await playback.download_thumbnail(start=100, end=110)

    assert asyncio.run(run()) == image
    assert struct.unpack_from("<IIIHHI", sent[0]) == (
        0x12345678,
        8,
        0,
        3,
        8,
        48,
    )


def test_binary_catalog_uses_the_apk_sd_recording_image_command():
    playback = session()
    catalog_payload = bytearray(0x18 + 12)
    struct.pack_into("<6I", catalog_payload, 0, 0, 2026, 9, 15, 1, 0)
    struct.pack_into("<III", catalog_payload, 0x18, 100, 110, 0)
    image = b"\xff\xd8image\xff\xd9"
    image_payload = bytearray(56)
    struct.pack_into("<III", image_payload, 44, 3, 0, len(image))
    sent = []
    playback._send_control = sent.append

    async def run():
        playback._connected = True
        playback._control = object()
        playback._ended = asyncio.get_running_loop().create_future()
        playback._control_messages.put_nowait(
            NativeSimpleCommand(8, 1, 3, 1, bytes(catalog_payload))
        )
        await playback.query_recordings(date(2026, 9, 15))
        playback._control_messages.put_nowait(
            NativeSimpleCommand(9, 1, 3, 8, bytes(image_payload) + image)
        )
        return await playback.download_thumbnail(start=100, end=110)

    result = asyncio.run(run())

    assert result == image
    assert len(sent) == 2
    assert struct.unpack_from("<IIIHHI", sent[1]) == (
        0x12345678,
        9,
        0,
        3,
        8,
        48,
    )
    assert playback._playback_started is False


def test_query_recordings_uses_apk_v3_catalog_when_advertised():
    config = replace(camera_config(), skill=json.dumps({"localStorage": 134217728}))
    playback = NativeRelayPlaybackSession(config, APP_SESSION)
    payload = bytearray(0x14 + 0x48)
    struct.pack_into("<5I", payload, 0, 0, 2026, 9, 15, 1)
    struct.pack_into("<II", payload, 0x14, 2, 7)
    payload[0x1C:0x23] = b"clip-v3"
    struct.pack_into("<III", payload, 0x3C, 100, 110, 0)
    struct.pack_into("<I", payload, 0x58, 0)
    sent = []
    playback._send_control = sent.append

    async def run():
        playback._connected = True
        playback._control = object()
        playback._ended = asyncio.get_running_loop().create_future()
        playback._control_messages.put_nowait(
            NativeSimpleCommand(8, 1, 3, 7, bytes(payload))
        )
        return await playback.query_recordings(date(2026, 9, 15))

    page = asyncio.run(run())

    assert page.items[0].uuid == "clip-v3"
    assert struct.unpack_from("<IIIHHI", sent[0]) == (
        0x12345678,
        8,
        0,
        3,
        7,
        20,
    )
    assert struct.unpack_from("<5I", sent[0], 20) == (0, 2026, 9, 15, 0)


@pytest.mark.parametrize("bootstrap", ["app-token", "expired", "missing", "early-answer"])
def test_connect_uses_mqtt_signaling_and_requires_direct_ice(monkeypatch, bootstrap):
    events = []
    captured = {}

    class FakeMoto:
        def __init__(self, **kwargs):
            self.on_answer = kwargs["on_answer"]
            captured["protocol_version"] = kwargs["protocol_version"]
            captured["mqtt_session_id"] = kwargs["session_id"]

        async def async_connect(self):
            events.append("mqtt-connect")

        def configure_offer_metadata(self, **kwargs):
            captured["http_metadata"] = kwargs

        async def async_send_sig_query(self):
            events.append("sig-query")

        async def async_send_offer(self, *args):
            events.append("offer")
            captured["offer"] = args
            if bootstrap != "early-answer":
                self.on_answer(answer_sdp())

        async def async_send_candidate(self, candidate):
            events.append(("candidate", candidate))

        async def async_send_disconnect(self):
            events.append("disconnect")

        async def async_close(self):
            events.append("mqtt-close")

    class FakeIce:
        diagnostics = "bindings=1, nominated=True, segments=1, auth_failures=0"

        def __init__(self, password, on_candidate, **kwargs):
            captured["ice_password"] = password
            captured["send_key"] = kwargs["send_key"]
            self.on_candidate = on_candidate

        async def async_gather(self):
            events.append("ice-gather")
            self.on_candidate("host-candidate")

        async def async_wait_for_nomination(self, timeout_seconds):
            events.append(("ice-nominated", timeout_seconds))

        def set_receive_key(self, key):
            captured["receive_key"] = key

        def send_segment(self, segment):
            captured["direct_output"] = segment

        def close(self):
            events.append("ice-close")

    class Conversation:
        def set_message_handler(self, handler):
            self.handler = handler

        def send(self, payload):
            captured.setdefault("control", []).append(payload)

    class FakeDirectSession:
        diagnostics = "routed=0:0x81=1, malformed_datagrams=0"

        def __init__(self, transmit):
            events.append("direct-open")
            captured["transmit"] = transmit
            self.control = Conversation()

        async def async_close(self):
            events.append("direct-close")

    monkeypatch.setattr(relay_module, "MotoClient", FakeMoto)
    monkeypatch.setattr(relay_module, "IceResponder", FakeIce)
    monkeypatch.setattr(relay_module, "DirectSession", FakeDirectSession)

    async def exercise():
        config = camera_config()
        if bootstrap != "app-token":
            token = json.loads(config.token) if bootstrap == "expired" else {}
            token["expire"] = 0
            info = json.loads(config.config_json)
            info["p2pConfig"] = token
            config = replace(config, token=json.dumps(token), config_json=json.dumps(info))

        def gateway(api, version, body, extra):
            events.append("http-init")
            if bootstrap == "early-answer":
                playback._on_answer(answer_sdp())
            token = json.loads(camera_config().token)
            message = json.loads(body["msg"])
            captured["http_sdp"] = message["msg"]["sdp"]
            captured["http_trace"] = message["header"]["trace_id"]
            token["session"]["sessionId"] = message["header"]["sessionid"]
            return {"p2pConfig": token}

        playback = NativeRelayPlaybackSession(config, APP_SESSION, call_api=gateway)
        await playback.connect(media=False)
        try:
            captured["local"] = playback._stream_config.p2p_session
            await asyncio.sleep(0)
            assert playback.transport_diagnostics.startswith(
                "transport=direct-ice, signaling=mqtt, relay=disabled"
            )
        finally:
            await playback.close()

    asyncio.run(exercise())

    expected = [
        "mqtt-connect",
        "sig-query",
        "offer",
        "ice-gather",
        "direct-open",
    ]
    if bootstrap != "app-token":
        expected.insert(1, "http-init")
    assert events[:len(expected)] == expected
    assert ("ice-nominated", 10.0) in events
    assert ("candidate", "host-candidate") in events
    assert "disconnect" in events
    assert "mqtt-close" in events
    assert captured["protocol_version"] == "2.3"
    assert captured["ice_password"] == captured["local"].ice_password
    assert len(captured["ice_password"]) == 24
    assert captured["mqtt_session_id"] == captured["local"].session_id != "session-id"
    assert captured["receive_key"] == ANSWER_KEY
    assert captured["send_key"] == captured["local"].aes_key
    sdp, ice, _trace, token, log = captured["offer"]
    assert f'a=msid-semantic: WMS {captured["mqtt_session_id"]}\r\n' in sdp
    if bootstrap != "app-token":
        assert sdp == captured["http_sdp"]
        assert _trace == captured["http_trace"]
    assert "m=application 9 imm 6001\r\n" in sdp
    assert ice == [{"urls": "stun:token.example.test"}]
    assert (token["sessionId"] != "server-session") == (bootstrap == "app-token")
    assert log == {}
    assert not hasattr(relay_module, "RelaySession")


def test_connect_fails_closed_without_direct_ice_nomination(monkeypatch):
    class FakeMoto:
        def __init__(self, **kwargs):
            self.on_answer = kwargs["on_answer"]

        async def async_connect(self):
            pass

        async def async_send_sig_query(self):
            pass

        async def async_send_offer(self, *args):
            self.on_answer(answer_sdp())

        async def async_send_candidate(self, candidate):
            pass

        async def async_send_disconnect(self):
            pass

        async def async_close(self):
            pass

    class FakeIce:
        diagnostics = "bindings=0, nominated=False, segments=0, auth_failures=0"

        def __init__(self, *args, **kwargs):
            pass

        async def async_gather(self):
            pass

        async def async_wait_for_nomination(self, timeout_seconds):
            raise TimeoutError("Camera did not nominate direct ICE")

        def set_receive_key(self, key):
            pass

        def send_segment(self, segment):
            raise AssertionError("Un-nominated ICE must never carry camera commands")

        def close(self):
            pass

    monkeypatch.setattr(relay_module, "MotoClient", FakeMoto)
    monkeypatch.setattr(relay_module, "IceResponder", FakeIce)

    async def exercise():
        playback = session()
        with pytest.raises(TimeoutError, match="did not nominate"):
            await playback.connect(media=False)
        assert playback._session is None
        assert playback._control is None

    asyncio.run(exercise())


def test_control_and_media_use_directional_aes_records():
    playback = session()

    class Conversation:
        def __init__(self):
            self.sent = []
            self.handler = None

        def send(self, payload):
            self.sent.append(payload)

        def set_message_handler(self, handler):
            self.handler = handler

    playback._offer = relay_module.build_offer(
        "account-uid", "session-id", 1, "ufrag", "password", OFFER_KEY
    )
    playback._control = Conversation()
    playback._connected = True

    request = struct.pack("<IIIHHI4i", 0x12345678, 2, 0, 3, 1, 16, 0, 2026, 9, 18)
    playback._send_control(request)
    assert decrypt_record(OFFER_KEY, playback._control.sent[0]) == request

    payload = b'{"items":[]}'
    response = struct.pack("<IIIHHI", 0x12345678, 2, 1, 3, 1, len(payload)) + payload
    playback._on_control_record(encrypt_record(ANSWER_KEY, response), ANSWER_KEY)
    assert playback._control_messages.get_nowait() == NativeSimpleCommand(
        2, 1, 3, 1, payload
    )

    payload = b"\x00\x00\x00\x01\x65video"
    codec_info = struct.pack(">6H", 1, 8, 1, 1, 0, 0) + bytes(
        PLAYBACK_CODEC_INFO_BYTES - 12
    )
    frame = struct.pack(">HH7I", 1, 1, len(payload), 1, 2, 3, 4, 5, 6)
    playback._on_media_record(
        encrypt_record(ANSWER_KEY, frame + codec_info + payload), ANSWER_KEY
    )

    parsed = playback._media_frames.get_nowait()
    assert parsed.payload == payload
    assert parsed.codec_info is not None
    assert parsed.codec_info.codec == 1


def test_rtp_media_stream_id_does_not_filter_playback_video():
    playback = session()
    playback._stream_id = 0x00020008

    def media_record(stream_id, sequence, timestamp, nal):
        header = struct.pack("<I5I", stream_id, 0, 0, 0, 0, 0)
        rtp = struct.pack(">BBHII", 0x80, 96, sequence, timestamp, 0x01020304) + nal
        return encrypt_record(ANSWER_KEY, header + struct.pack("<I", len(rtp)) + rtp)

    playback._on_rtp_record(
        1, media_record(0x00010004, 1, 1000, b"\x65first"), ANSWER_KEY
    )
    playback._on_rtp_record(
        1, media_record(0x00010004, 2, 2000, b"\x41second"), ANSWER_KEY
    )

    frame = playback._media_frames.get_nowait()
    assert frame.payload == b"\x00\x00\x00\x01\x65first"
    assert playback._media_stream_packets == {0x00010004: 2}


def test_rtp_audio_is_exposed_as_pcm_s16le():
    playback = session()
    header = struct.pack("<I5I", 0x00010005, 0, 0, 0, 0, 0)
    rtp = struct.pack(">BBHII", 0x80, 99, 7, 8000, 0x05060708) + b"\xff\x7f\x00\x80"

    playback._on_rtp_record(
        2,
        encrypt_record(ANSWER_KEY, header + struct.pack("<I", len(rtp)) + rtp),
        ANSWER_KEY,
    )

    frame = playback._media_frames.get_nowait()
    assert frame.is_audio
    assert frame.payload == b"\xff\x7f\x00\x80"
    assert frame.field_12 == 8000
    assert frame.codec_info is not None
    assert frame.codec_info.codec == 0xFFFE
    assert frame.codec_info.sample_rate == 8000
    assert frame.codec_info.channels == 1
    assert frame.codec_info.bit_width == 16


def test_rtp_audio_retains_camera_sample_rate_and_channels():
    playback = session()
    header = struct.pack("<I5I", 0x00010005, 0, 0, 0, 1, 0)
    extension = struct.pack("<BB3H", 0, 0, 3, 1, 1)
    rtp = struct.pack(">BBHII", 0x80, 99, 7, 8000, 1) + b"\x00\x00\x01\x00"
    playback._on_rtp_record(2, encrypt_record(ANSWER_KEY,
        header + extension + struct.pack("<I", len(rtp)) + rtp), ANSWER_KEY)
    frame = playback._media_frames.get_nowait()
    assert frame.codec_info.sample_rate == 16000
    assert frame.codec_info.channels == 2


@pytest.mark.parametrize("payload_type,codec", [(0, 133), (8, 132), (97, 4)])
def test_rtp_audio_uses_native_codec_dispatch(payload_type, codec):
    playback = session()
    header = struct.pack("<I5I", 0x00010005, 0, 0, 0, 0, 0)
    payload = b"audio"
    wire_payload = b"\x00\x10" + (len(payload) << 3).to_bytes(2, "big") + payload if payload_type == 97 else payload
    rtp = struct.pack(">BBHII", 0x80, payload_type, 7, 8000, 1) + wire_payload
    playback._on_rtp_record(2, encrypt_record(ANSWER_KEY,
        header + struct.pack("<I", len(rtp)) + rtp), ANSWER_KEY)
    frame = playback._media_frames.get_nowait()
    assert frame.codec_info.codec == codec
    assert frame.payload == payload


def test_start_uses_ready_media_channels_and_sends_binary_video_audio_commands():
    playback = session()

    class Conversation:
        def __init__(self):
            self.sent = []
            self.handler = None

        def send(self, payload):
            self.sent.append(payload)

        def set_message_handler(self, handler):
            self.handler = handler

    playback._offer = relay_module.build_offer(
        "account-uid", "session-id", 1, "ufrag", "password", OFFER_KEY
    )
    playback._control = Conversation()
    playback._video = Conversation()
    playback._audio = Conversation()
    playback._connected = True

    async def exercise():
        class Relay:
            def __init__(self):
                self.opened = []

            def conversation(self, stream_id):
                self.opened.append(stream_id)
                value = Conversation()
                value.set_message_handler = lambda handler: setattr(
                    value, "handler", handler
                )
                return value

        playback._relay = Relay()
        playback._answer = asyncio.get_running_loop().create_future()
        playback._answer.set_result(relay_module.parse_answer(answer_sdp()))

        async def response(*_args, **_kwargs):
            return None

        playback._wait_for_playback_responses = response
        await playback.start(start=100, end=200, play_time=110)
        return playback._relay.opened

    opened = asyncio.run(exercise())
    assert opened == []
    requests = [decrypt_record(OFFER_KEY, item) for item in playback._control.sent]
    assert struct.unpack_from("<IIIHHI", requests[0]) == (
        0x12345678,
        0x00020008,
        0,
        7,
        0,
        20,
    )
    assert struct.unpack_from("<5i", requests[0], 20) == (0, 0, 100, 200, 110)
    assert struct.unpack_from("<IIIHHI2i", requests[1]) == (
        0x12345678,
        0x00020008,
        0,
        7,
        4,
        8,
        0,
        4,
    )


def test_start_keeps_media_delivered_while_playback_acknowledgements_arrive():
    playback = session()

    class Conversation:
        def __init__(self):
            self.handler = None

        def send(self, _payload):
            return None

        def set_message_handler(self, handler):
            self.handler = handler

    playback._offer = relay_module.build_offer(
        "account-uid", "session-id", 1, "ufrag", "password", OFFER_KEY
    )
    playback._control = Conversation()
    playback._video = Conversation()
    playback._audio = Conversation()
    playback._connected = True

    def media_record(stream_id, sequence, timestamp, payload_type, payload):
        header = struct.pack("<I5I", stream_id, 0, 0, 0, 0, 0)
        rtp = (
            struct.pack(">BBHII", 0x80, 0x80 | payload_type, sequence, timestamp, 1)
            + payload
        )
        return encrypt_record(ANSWER_KEY, header + struct.pack("<I", len(rtp)) + rtp)

    async def exercise():
        playback._answer = asyncio.get_running_loop().create_future()
        playback._answer.set_result(relay_module.parse_answer(answer_sdp()))

        async def response(*_args, **_kwargs):
            playback._video.handler(
                media_record(0x00010004, 1, 1000, 96, b"\x65video")
            )
            playback._audio.handler(
                media_record(0x00010005, 1, 8000, 99, b"\x00\x00\xff\x7f")
            )

        playback._wait_for_playback_responses = response
        await playback.start(start=100, end=200)

    asyncio.run(exercise())
    video = playback._media_frames.get_nowait()
    audio = playback._media_frames.get_nowait()
    assert video.payload == b"\x00\x00\x00\x01\x65video"
    assert audio.is_audio
    assert audio.payload == b"\x00\x00\xff\x7f"


def test_stop_sends_apk_commands_in_order_and_clears_playback_state():
    playback = session()

    class Conversation:
        def __init__(self):
            self.sent = []

        def send(self, payload):
            self.sent.append(payload)

    playback._offer = relay_module.build_offer(
        "account-uid", "session-id", 1, "ufrag", "password", OFFER_KEY
    )
    playback._control = Conversation()
    playback._connected = True
    playback._playback_started = True
    playback._stream_id = 0x00020008
    waited = []

    async def response(request_id, command):
        waited.append((request_id, command))

    playback._wait_for_control_response = response
    asyncio.run(playback.stop())

    requests = [decrypt_record(OFFER_KEY, item) for item in playback._control.sent]
    assert [struct.unpack_from("<HH", item, 12) for item in requests] == [
        (7, 3),
        (7, 5),
    ]
    assert waited == [(0x00020008, (7, 3))]
    assert not playback._playback_started
    assert playback._stream_id is None


def test_seek_reuses_connected_camera_and_issues_start_playback_again():
    playback = session()

    class Conversation:
        def __init__(self):
            self.sent = []

        def send(self, payload):
            self.sent.append(payload)

        def set_message_handler(self, handler):
            self.handler = handler

    playback._offer = relay_module.build_offer(
        "account-uid", "session-id", 1, "ufrag", "password", OFFER_KEY
    )
    playback._control = Conversation()
    playback._video = Conversation()
    playback._audio = Conversation()
    playback._connected = True
    playback._playback_started = True
    previous_stream_id = 0x00020007
    playback._stream_id = previous_stream_id

    async def exercise():
        playback._answer = asyncio.get_running_loop().create_future()
        playback._answer.set_result(relay_module.parse_answer(answer_sdp()))
        playback._wait_for_playback_responses = (
            lambda *_args, **_kwargs: asyncio.sleep(0)
        )
        await playback.seek(start=200, end=300, play_time=225)

    asyncio.run(exercise())

    requests = [decrypt_record(OFFER_KEY, item) for item in playback._control.sent]
    assert [struct.unpack_from("<HH", item, 12) for item in requests] == [
        (7, 0),
        (7, 4),
    ]
    assert playback._connected
    assert playback._playback_started
    assert playback._stream_id != previous_stream_id


def test_encrypted_start_uses_apk_start_playback_v2_command():
    playback = session()

    class Conversation:
        def __init__(self):
            self.sent = []

        def send(self, payload):
            self.sent.append(payload)

        def set_message_handler(self, handler):
            self.handler = handler

    playback._offer = relay_module.build_offer(
        "account-uid", "session-id", 1, "ufrag", "password", OFFER_KEY
    )
    playback._control = Conversation()
    playback._video = Conversation()
    playback._audio = Conversation()
    playback._connected = True
    waited = []

    async def exercise():
        playback._answer = asyncio.get_running_loop().create_future()
        playback._answer.set_result(relay_module.parse_answer(answer_sdp()))

        async def responses(stream_id, *, start_command=(7, 0)):
            waited.append((stream_id, start_command))

        playback._wait_for_playback_responses = responses
        await playback.start(
            start=100,
            end=200,
            play_time=125,
            encrypted=True,
        )

    asyncio.run(exercise())

    requests = [decrypt_record(OFFER_KEY, item) for item in playback._control.sent]
    assert [struct.unpack_from("<HH", item, 12) for item in requests] == [
        (100, 20),
        (7, 4),
    ]
    assert waited == [(playback._stream_id, (100, 20))]


def test_play_mode_start_uses_apk_fragment_command():
    playback = session()

    class Conversation:
        def __init__(self):
            self.sent = []

        def send(self, payload):
            self.sent.append(payload)

        def set_message_handler(self, handler):
            self.handler = handler

    playback._offer = relay_module.build_offer(
        "account-uid", "session-id", 1, "ufrag", "password", OFFER_KEY
    )
    playback._control = Conversation()
    playback._video = Conversation()
    playback._audio = Conversation()
    playback._connected = True
    waited = []

    async def exercise():
        playback._answer = asyncio.get_running_loop().create_future()
        playback._answer.set_result(relay_module.parse_answer(answer_sdp()))

        async def responses(stream_id, *, start_command=(7, 0)):
            waited.append((stream_id, start_command))

        playback._wait_for_playback_responses = responses
        await playback.start(
            start=100,
            end=200,
            play_time=125,
            fragments_json='{"fragments":[{"start":100,"end":200}]}',
            play_mode_supported=True,
        )

    asyncio.run(exercise())

    requests = [decrypt_record(OFFER_KEY, item) for item in playback._control.sent]
    assert [struct.unpack_from("<HH", item, 12) for item in requests] == [
        (7, 21),
        (7, 4),
    ]
    assert struct.unpack_from("<7i", requests[0], 20) == (
        0,
        21,
        0,
        125,
        1,
        100,
        200,
    )
    assert waited == [(playback._stream_id, (7, 21))]


def test_pause_and_resume_use_active_stream_without_stopping_transport():
    playback = session()

    class Conversation:
        def __init__(self):
            self.sent = []

        def send(self, payload):
            self.sent.append(payload)

    playback._offer = relay_module.build_offer(
        "account-uid", "session-id", 1, "ufrag", "password", OFFER_KEY
    )
    playback._control = Conversation()
    playback._connected = True
    playback._playback_started = True
    playback._stream_id = 0x00020008
    waited = []

    async def response(request_id, command):
        waited.append((request_id, command))

    playback._wait_for_control_response = response

    async def exercise():
        await playback.pause()
        await playback.resume()

    asyncio.run(exercise())

    requests = [decrypt_record(OFFER_KEY, item) for item in playback._control.sent]
    assert [struct.unpack_from("<HH", item, 12) for item in requests] == [
        (7, 1),
        (7, 2),
    ]
    assert waited == [
        (0x00020008, (7, 1)),
        (0x00020008, (7, 2)),
    ]
    assert playback._connected
    assert playback._playback_started
    assert not playback._playback_paused


def test_media_reset_replaces_h264_reassembly_state_for_a_new_playback_stream():
    playback = session()
    video_parser = playback._rtp_parsers[1]
    audio_parser = playback._rtp_parsers[2]
    h264 = playback._h264
    playback._media_frames.put_nowait(object())

    playback._reset_media_state()

    assert playback._rtp_parsers[1] is video_parser
    assert playback._rtp_parsers[2] is audio_parser
    assert playback._h264 is not h264
    assert playback._media_frames.empty()
