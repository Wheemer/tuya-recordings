import asyncio
import json
import ssl

import pytest
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling import moto_client

from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.models import (
    MqttIdentity,
    StreamConfig,
)
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling import (
    MotoClient,
    relay_candidate_frame,
    relay_offer_frame,
)
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.exceptions import (
    TuyaIpcP2pProtocolError,
)
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling.answer import (
    parse_answer,
)
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling.envelope import (
    encode_payload,
)


@pytest.mark.parametrize("version", ["2.2", "2.3"])
def test_mqtt_only_routes_302_and_classifies_inbound_without_payloads(version):
    answers = []
    client = MotoClient(
        identity=MqttIdentity("mqtt.example.test", 8883, "client", "user", "pass"),
        uid="uid", device_id="camera", session_id="session",
        local_key="0123456789abcdef", on_answer=answers.append,
        on_candidate=lambda _: None, on_disconnect=lambda _: None,
        protocol_version=version,
    )
    header = {"type": "answer", "sessionid": "session"}
    answer = {"header": header, "msg": {"sdp": "private-sdp"}}
    frames = [
        (22, answer),
        (302, {}),
        (302, {"header": header, "msg": {}}),
        (302, {"header": {**header, "sessionid": "other"}, "msg": answer["msg"]}),
        (302, {"header": {**header, "type": "private-type"}, "msg": {}}),
        (302, answer),
    ]
    for sequence, (protocol, data) in enumerate(frames, 1):
        client._consume(encode_payload(
            b"0123456789abcdef", sequence, b"test", data, 0, protocol, version
        ))
    assert answers == ["private-sdp"]
    assert "session_mismatches=1" in client.diagnostics
    assert "answer=1,answer_without_sdp=1,invalid_shape=1,other_session_message=1,signal_query=1" in client.diagnostics
    assert "private-" not in client.diagnostics


def test_invalid_answer_key_is_a_protocol_error_without_key_material():
    with pytest.raises(TuyaIpcP2pProtocolError, match="invalid AES key encoding") as err:
        parse_answer("a=ice-ufrag:test\na=ice-pwd:test\na=aes-key:private-key")
    assert "private-key" not in str(err.value)


def test_mqtt_close_failure_retains_client_for_cleanup_retry():
    class Broker:
        attempts = 0

        async def __aexit__(self, *args):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("close failed")

    client = MotoClient(
        identity=MqttIdentity("mqtt.example.test", 8883, "client", "user", "pass"),
        uid="uid", device_id="camera", session_id="session",
        local_key="0123456789abcdef", on_answer=lambda _: None,
        on_candidate=lambda _: None, on_disconnect=lambda _: None,
    )
    broker = Broker()
    client._client = broker

    async def run():
        with pytest.raises(OSError, match="close failed"):
            await client.async_close()
        assert client._client is broker
        await client.async_close()
        assert client._client is None

    asyncio.run(run())


def test_direct_close_attempts_all_channels_and_retains_failed_channel():
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport.direct_session import DirectSession
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.exceptions import TuyaIpcP2pSessionError

    class Channel:
        def __init__(self, fails):
            self.fails = fails
            self.calls = 0

        async def async_close(self):
            self.calls += 1
            if self.fails and self.calls == 1:
                raise OSError("close failed")

    async def run():
        session = DirectSession(lambda data: None)
        await session.control.async_close()
        failed, healthy = Channel(True), Channel(False)
        session._conversations = {0: failed, 1: healthy}
        with pytest.raises(TuyaIpcP2pSessionError, match="cleanup incomplete"):
            await session.async_close()
        assert healthy.calls == 1
        assert session._conversations == {0: failed}
        await session.async_close()
        assert session._conversations == {}
        assert failed.calls == 2

    asyncio.run(run())


def test_signaling_reader_reports_transport_failure_without_retrying():
    from types import SimpleNamespace

    class FailedMessages:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise ConnectionError("private-broker-details")

    failures = []
    client = MotoClient(identity=MqttIdentity("mqtt.example.test", 8883, "client", "user", "pass"),
        uid="uid", device_id="camera", session_id="session", local_key="0123456789abcdef",
        on_answer=lambda _: None, on_candidate=lambda _: None, on_disconnect=lambda _: None,
        on_failure=failures.append)
    client._client = SimpleNamespace(messages=FailedMessages())
    asyncio.run(client._async_read())
    assert failures == ["ConnectionError"]
    assert "failure=ConnectionError" in client.diagnostics
    assert "private-broker-details" not in client.diagnostics
    assert "published=0" in client.diagnostics


def test_mqtt_offer_preserves_apk_signaling_header():
    published = []
    client = MotoClient(
        identity=MqttIdentity("mqtt.example.test", 8884, "client", "user", "pass"),
        uid="account",
        device_id="camera",
        session_id="session",
        local_key="0123456789abcdef",
        on_answer=lambda _sdp: None,
        on_candidate=lambda _candidate: None,
        on_disconnect=lambda _reason: None,
        protocol_version="2.3",
        moto_id="camera-moto-id",
    )

    async def capture(protocol, data):
        published.append((protocol, data))

    client._async_publish = capture
    asyncio.run(client.async_send_offer("sdp", [], "trace", {}, {}))

    header = published[0][1]["header"]
    assert header["moto_id"] == "camera-moto-id"
    assert header["security_level"] == 3
    assert published[0][1]["msg"]["preconnect"] is True


def test_mqtt_offer_uses_camera_advertised_connection_metadata():
    published = []
    client = MotoClient(
        identity=MqttIdentity("mqtt.example.test", 8884, "client", "user", "pass"),
        uid="account",
        device_id="camera",
        session_id="session",
        local_key="0123456789abcdef",
        on_answer=lambda _sdp: None,
        on_candidate=lambda _candidate: None,
        on_disconnect=lambda _reason: None,
        preconnect=True,
        security_level=4,
    )

    async def capture(protocol, data):
        published.append((protocol, data))

    client._async_publish = capture
    asyncio.run(client.async_send_offer("sdp", [], "trace", {}, {}))

    assert published[0][1]["header"]["security_level"] == 4
    assert published[0][1]["msg"]["preconnect"] is True


def test_relay_frames_preserve_apk_signaling_header():
    offer = json.loads(
        relay_offer_frame(
            "account",
            "camera",
            "session",
            "trace",
            "sdp",
            [],
            {},
            {},
            "camera-moto-id",
            True,
            4,
        )
    )
    candidate = json.loads(
        relay_candidate_frame(
            "account", "camera", "session", "trace", "candidate:1", "camera-moto-id"
        )
    )

    assert offer["header"]["moto_id"] == "camera-moto-id"
    assert offer["header"]["security_level"] == 4
    assert offer["msg"]["preconnect"] is True
    assert candidate["header"]["moto_id"] == "camera-moto-id"


def test_stream_config_uses_consumer_app_preconnect_policy():
    config = StreamConfig.from_json(
        {
            "password": "camera-password",
            "p2pConfig": {
                "motoId": "camera-moto-id",
                "securityLevel": 4,
                # The app-wide consumer setting, not this token field,
                # controls native preconnect. An explicit false token value
                # must not disable it.
                "preconnect": 0,
                "session": {
                    "sessionId": "session",
                    "aesKey": "0123456789abcdef",
                    "iceUfrag": "ufrag",
                    "icePassword": "password",
                    "traceId": "trace",
                },
                "tcpRelay": {
                    "urls": ["tcp4:relay.example.test:1443"],
                    "username": "1:user",
                    "credential": "credential",
                    "sessionId": "relay-session",
                },
            },
        },
        "camera",
        "0123456789abcdef",
    )

    assert config.moto_id == "camera-moto-id"
    assert config.preconnect is True
    assert config.security_level == 4


def test_signaling_tls_validates_the_broker_certificate_chain():
    """Regional hostname mismatch must not disable certificate validation."""
    assert moto_client._BROKER_TLS.check_hostname is True
    assert moto_client._BROKER_TLS.verify_mode is ssl.CERT_REQUIRED
