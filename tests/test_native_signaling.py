import asyncio
import json
import ssl
from types import SimpleNamespace

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
    TuyaIpcP2pConnectionError,
    TuyaIpcP2pProtocolError,
)
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling.answer import (
    parse_answer,
)
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling.envelope import (
    decode_payload,
    encode_payload,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("topic", ["smart/mb/account", "partner/mb/account"])
@pytest.mark.parametrize("string_map", [False, True])
async def test_account_sd_status_report_completes_the_pending_device_query(topic, string_map):
    client = MotoClient(
        identity=MqttIdentity("mqtt.example.test", 8883, "client", "user", "pass"),
        uid="account", device_id="camera", session_id="session", local_key="0123456789abcdef",
        on_answer=lambda _: pytest.fail("Account reports cannot carry native answers"),
        on_candidate=lambda _: None, on_disconnect=lambda _: None,
        account_topics=("smart/mb/account", "partner/mb/account"),
    )
    with client.dp_report_listener("110") as reply:
        def receive(data, *, route=topic, protocol=4):
            client._consume_message(route, json.dumps({"protocol": protocol, "data": data}).encode())
        receive({"gwId": "other", "dps": '{"110":1}'})
        receive({"dps": '{"110":1}'})
        receive({"gwId": "camera", "devId": "other", "dps": '{"110":1}'})
        receive({"gwId": "camera", "cid": "child", "dps": '{"110":1}'})
        receive({"gwId": "camera", "dps": '{"110":1}'}, route="smart/mb/other-account")
        receive({"gwId": "camera", "dps": '{"110":1}'}, protocol=302)
        receive({"gwId": "camera", "dps": '{"248":1}'})
        receive({"gwId": "camera", "dps": '{"110":true}'})
        client._consume_message(topic, b'{"protocol":4,"data":')
        client._consume_message(topic, b"x" * 65537)
        assert not reply.done()
        data = {"devId": "camera", "dps": {"110": "1"}}
        if string_map:
            data = {"gwId": "camera", "dps": '{"110":"1"}'}
        receive(data)
        assert await reply == 1
        assert "account_received=" in client.diagnostics and "dp_report_accepted=1" in client.diagnostics
        assert not any(value in client.diagnostics for value in (topic, "0123456789abcdef", "other-account"))
    assert client._dp_query is None


@pytest.mark.asyncio
@pytest.mark.parametrize("grants", [(1, 1), (128, 1), (128, 128)])
async def test_account_subscriptions_share_one_connection_and_keep_device_replies(monkeypatch, grants):
    calls = []
    messages = asyncio.Queue()
    class Broker:
        def __init__(self, **kwargs):
            calls.append("connection")
        async def __aenter__(self): return self
        async def __aexit__(self, *args): calls.append("closed")
        async def subscribe(self, topic, **kwargs):
            calls.append(topic)
            return (1,) if isinstance(topic, str) else grants
        @property
        def messages(self):
            async def stream():
                while True:
                    yield await messages.get()
            return stream()
    monkeypatch.setattr(moto_client.aiomqtt, "Client", Broker)
    client = MotoClient(identity=MqttIdentity("mqtt.example.test", 8883, "client", "user", "pass"),
                        uid="account", device_id="camera", session_id="session", local_key="0123456789abcdef",
                        on_answer=lambda _: None, on_candidate=lambda _: None, on_disconnect=lambda _: None,
                        account_topics=("smart/mb/account", "partner/mb/account"))
    try:
        await client.async_connect()
        with client.dp_report_listener("110") as reply:
            if grants == (1, 1):
                topic = "smart/mb/account"
                payload = b'{"protocol":4,"data":{"gwId":"camera","dps":"{\\"110\\":1}"}}'
            else:
                topic = "smart/mb/in/camera"
                payload = encode_payload(b"0123456789abcdef", 1, b"test", {"dps": {"110": 1}}, 0, 4, "2.3")
            await messages.put(SimpleNamespace(topic=topic, payload=payload))
            assert await asyncio.wait_for(asyncio.shield(reply), 1) == 1
    finally:
        await client.async_close()
    assert calls == ["connection", "smart/mb/in/camera", [("smart/mb/account", 1), ("partner/mb/account", 1)], "closed"]
    assert client._reader is None and client._client is None


@pytest.mark.parametrize(("version", "frame"), [
    ("1.1", bytes.fromhex(
        "312e3160f1c19600000003616263647b2264617461223a7b22647073223a7b7d7d"
        "2c2270726f746f636f6c223a352c2274223a3132337d"
    )),
    ("2.1", b"2.1dca139974ea33ce88BUw7PgybcQiSqScYaWvdlqlEvsoL5ZBj+J9Asj7hyzWmhkyJPzvk1+"
     b"8fkKhieGBe6ccirFX+vLcRueuSuyOXw=="),
])
def test_legacy_mqtt_envelope_matches_independent_vector(version, frame):
    key = b"0123456789abcdef"
    data = {"dps": {"110": None}}
    assert encode_payload(key, 3, b"abcd", data, 123, 5, version) == frame
    assert decode_payload(key, frame) == (5, {"dps": {}} if version == "1.1" else data)
    with pytest.raises(TuyaIpcP2pProtocolError, match="invalid"):
        decode_payload(key, frame[:3] + bytes([frame[3] ^ 1]) + frame[4:])


def test_inbound_protocol_histogram_is_bounded_and_does_not_log_payloads():
    key = b"0123456789abcdef"
    client = MotoClient(
        identity=MqttIdentity("mqtt.example.test", 8883, "client", "user", "pass"),
        uid="account", device_id="camera", session_id="session", local_key=key.decode(),
        protocol_version="2.3",
        on_answer=lambda _: None, on_candidate=lambda _: None, on_disconnect=lambda _: None,
    )
    client._consume(encode_payload(key, 1, b"test", {"dps": {"110": "private-status"}}, 0, 4, "2.3"))
    for protocol in range(100, 140):
        client._consume(encode_payload(key, 1, b"test", {"payload": "private-content"}, 0, protocol, "2.3"))
    assert "dp_report_no_query=1" in client.diagnostics
    assert "4=1" in client.diagnostics and "other=" in client.diagnostics
    assert len(client._inbound_protocols) <= 17
    assert "private-status" not in client.diagnostics
    assert "private-content" not in client.diagnostics
    assert key.decode() not in client.diagnostics


def test_pv20_uses_signed_json_for_send_and_keyed_crc_for_receive():
    key = b"0123456789abcdef"
    outbound = encode_payload(
        key, 3, b"abcd", {"dps": {"110": None}}, 123, 5, "2.0", device_id="camera",
    )
    assert json.loads(outbound) == {
        "data": "2055842A689C2D8AA7096C746BF29657D0FE8315E8352360AAE299B79563441C",
        "gwId": "camera", "protocol": 5, "pv": "2.0", "t": 123,
        "sign": "ebf929c02827cb4db858354cfb9e5e2e",
    }
    answers = []
    client = MotoClient(
        identity=MqttIdentity("mqtt.example.test", 8883, "client", "user", "pass"),
        uid="uid", device_id="camera", session_id="session", local_key=key.decode(),
        on_answer=answers.append, on_candidate=lambda _: None, on_disconnect=lambda _: None,
        protocol_version="2.0",
    )
    data = {"header": {"type": "answer", "sessionid": "session"}, "msg": {"sdp": "answer"}}
    inbound = encode_payload(key, 3, b"abcd", data, 123, 302, "1.1")
    client._consume(b"2.0" + inbound[3:])
    assert answers == ["answer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["report", "timeout", "cancel", "reader_stopped"])
async def test_direct_dp_query_waits_for_matching_report(monkeypatch, outcome):
    key = b"0123456789abcdef"
    client = MotoClient(
        identity=MqttIdentity("mqtt.example.test", 8883, "client", "user", "pass"),
        uid="account", device_id="camera", session_id="session", local_key=key.decode(),
        on_answer=lambda _: None, on_candidate=lambda _: None, on_disconnect=lambda _: None,
    )
    sent = asyncio.Event()

    async def publish(protocol, data):
        assert (protocol, data) == (5, {"dps": {"110": None}})
        sent.set()

    def receive(protocol, data):
        client._consume(encode_payload(key, 3, b"abcd", data, 1, protocol))

    assert moto_client._DP_REPORT_TIMEOUT_SECONDS == 30
    monkeypatch.setattr(moto_client, "_DP_REPORT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(client, "_async_publish", publish)
    task = asyncio.create_task(client.async_query_direct_dp("110"))
    await sent.wait()
    receive(5, {"dps": {"110": 1}})
    receive(4, {"devId": "other-camera", "dps": {"110": 1}})
    receive(4, {"cid": "child", "dps": {"110": 1}})
    receive(4, {"dps": {"111": 1, "110": True}})
    assert client._dp_query is not None and not client._dp_query[1].done()
    assert "protocols=4=3,5=1" in client.diagnostics
    assert "dp_report_other_device=1" in client.diagnostics
    assert "dp_report_child=1" in client.diagnostics
    assert "dp_report_missing_or_non_integer=1" in client.diagnostics
    if outcome == "report":
        receive(4, {"devId": "camera", "dps": {"110": 0}})
        assert await task == 0
        assert "dp_report_accepted=1" in client.diagnostics
    elif outcome == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome == "reader_stopped":
        async def messages():
            if False:
                yield None

        client._client = SimpleNamespace(messages=messages())
        await client._async_read()
        with pytest.raises(TuyaIpcP2pConnectionError, match="DP report channel stopped"):
            await task
        with pytest.raises(TuyaIpcP2pConnectionError, match="DP report channel stopped"):
            await client.async_query_direct_dp("110")
    else:
        with pytest.raises(TimeoutError):
            await task
    assert client._dp_query is None


@pytest.mark.asyncio
@pytest.mark.parametrize("method,qos", [("async_send_wake", 0), ("async_send_playback_wake", 1)])
async def test_wake_matches_apk_wire_format(method, qos):
    sent = []

    async def publish(topic, payload, **options):
        sent.append((topic, payload, options))

    client = MotoClient(
        identity=MqttIdentity("mqtt.example.test", 8883, "client", "user", "pass"),
        uid="uid", device_id="camera", session_id="session",
        local_key="123456789", on_answer=lambda _: None,
        on_candidate=lambda _: None, on_disconnect=lambda _: None,
    )
    client._client = SimpleNamespace(publish=publish)
    with pytest.raises(TuyaIpcP2pConnectionError, match="not ready"):
        await getattr(client, method)()
    client._subscription = "granted_qos_1"
    await getattr(client, method)()
    assert sent == [
        ("m/w/camera", bytes.fromhex("cbf43926"), {"qos": qos, "retain": False, "timeout": 15}),
    ]


@pytest.mark.parametrize("suback", [(0,), [SimpleNamespace(value=1)], (128,), []])
def test_native_mqtt_checks_suback_before_publishing_apk_qos(monkeypatch, suback):
    calls = []

    class Broker:
        def __init__(self, **kwargs):
            assert (kwargs["hostname"], kwargs["port"]) == ("mqtt.example.test", 8883)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            calls.append("closed")

        async def subscribe(self, topic, *, qos, timeout):
            calls.append(("subscribe", topic, qos))
            assert timeout > 0
            return suback

        async def publish(self, topic, payload, *, qos):
            calls.append(("publish", topic, qos))

        @property
        async def messages(self):
            await asyncio.Event().wait()
            yield None

    monkeypatch.setattr(moto_client.aiomqtt, "Client", Broker)
    client = MotoClient(
        identity=MqttIdentity("mqtt.example.test", 8883, "client", "user", "pass"),
        uid="uid", device_id="camera", session_id="session",
        local_key="0123456789abcdef", on_answer=lambda _: None,
        on_candidate=lambda _: None, on_disconnect=lambda _: None,
    )

    async def run():
        try:
            await client.async_connect()
            await client.async_send_offer("sdp", [], "trace", {}, {})
        finally:
            if not suback or suback == (128,):
                await client.async_send_disconnect()
            await client.async_close()

    if not suback or suback == (128,):
        with pytest.raises(TuyaIpcP2pConnectionError, match="rejected"):
            asyncio.run(run())
        assert "subscription=rejected" in client.diagnostics
        assert calls == [("subscribe", "smart/mb/in/camera", 1), "closed"]
    else:
        asyncio.run(run())
        assert "subscription=granted_qos_" in client.diagnostics
        assert "broker=connected, outbound=offer=1" in client.diagnostics
        assert calls == [
            ("subscribe", "smart/mb/in/camera", 1),
            ("publish", "smart/mb/out/camera", 0),
            "closed",
        ]


def test_mqtt_reader_only_routes_exact_device_reply_topic():
    answers = []
    client = MotoClient(
        identity=MqttIdentity("mqtt.example.test", 8883, "client", "user", "pass"),
        uid="uid", device_id="camera", session_id="session",
        local_key="0123456789abcdef", on_answer=answers.append,
        on_candidate=lambda _: None, on_disconnect=lambda _: None,
    )
    payload = encode_payload(
        b"0123456789abcdef", 1, b"test",
        {"header": {"type": "answer", "sessionid": "session"}, "msg": {"sdp": "answer"}},
        0, 302, "2.2",
    )

    async def messages():
        for topic in ("smart/mb/in/another-camera", "smart/mb/out/camera", "smart/mb/in/camera"):
            yield SimpleNamespace(topic=topic, payload=payload)

    client._client = SimpleNamespace(messages=messages())
    asyncio.run(client._async_read())
    assert answers == ["answer"]
    assert "received=3, decode_failures=0" in client.diagnostics
    assert "topic_mismatch=2" in client.diagnostics
    assert "another-camera" not in client.diagnostics


@pytest.mark.parametrize(("sent_pv", "reply_pv"), [
    ("2.2", "2.3"), ("2.3", "2.2"), ("2.3", "2.1"), ("2.3", "1.1"),
])
def test_mqtt_modern_reply_uses_wire_pv_and_requires_matching_session(sent_pv, reply_pv):
    answers = []
    key = b"0123456789abcdef"
    client = MotoClient(
        identity=MqttIdentity("mqtt.example.test", 8883, "client", "user", "pass"),
        uid="uid", device_id="camera", session_id="session", local_key=key.decode(),
        protocol_version=sent_pv, on_answer=answers.append,
        on_candidate=lambda _: None, on_disconnect=lambda _: None,
    )

    def frame(session_id):
        header = {"type": "answer"}
        if session_id is not None:
            header["sessionid"] = session_id
        return encode_payload(
            key, 1, b"test", {"header": header, "msg": {"sdp": "answer"}},
            0, 302, reply_pv,
        )

    client._consume(frame(None))
    client._consume(frame("another-session"))
    valid = frame("session")
    client._consume(valid[:-1] + bytes([valid[-1] ^ 1]))
    assert answers == []
    client._consume(valid)
    assert answers == ["answer"]
    assert "decode_failures=1, session_mismatches=2" in client.diagnostics
    assert client._protocol_version == sent_pv


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_pv", ["1.1", "2.1", "2.2", "2.3"])
@pytest.mark.parametrize("status", [1, "1", 1.0, "1.0", "1e0"])
async def test_dp_report_decoder_uses_received_pv_without_changing_offer_pv(monkeypatch, reply_pv, status):
    key = b"0123456789abcdef"
    client = MotoClient(
        identity=MqttIdentity("mqtt.example.test", 8883, "client", "user", "pass"),
        uid="uid", device_id="camera", session_id="session", local_key=key.decode(),
        protocol_version="2.3", on_answer=lambda _: None,
        on_candidate=lambda _: None, on_disconnect=lambda _: None,
    )

    async def publish(protocol, data):
        assert (protocol, data) == (5, {"dps": {"247": None}})
        # Wrong-device and malformed reports cannot satisfy the query.
        for device, value in (("other", status), ("camera", True), ("camera", status)):
            client._consume(encode_payload(
                key, 3, b"test", {"devId": device, "dps": {"247": value}}, 1, 4, reply_pv,
            ))

    monkeypatch.setattr(client, "_async_publish", publish)
    assert await client.async_query_direct_dp("247") == 1
    assert client._protocol_version == "2.3"
    assert "dp_report_other_device=1" in client.diagnostics
    assert "dp_report_missing_or_non_integer=1" in client.diagnostics
    assert client._dp_query is None


@pytest.mark.parametrize(("value", "expected"), [
    (0, 0), ("0", 0), (" 5 ", 5), (True, None), ("true", None), (None, None),
    ("", None), ("null", None), (1.0, 1), (1.5, 1), ("1.5", 1), ("1e0", 1),
    (-1.5, -1), ("-1.5", -1), (float("nan"), None), (float("inf"), None),
    ("NaN", None), ("Infinity", None), ("1e9999999999999999999999", None), ("[]", None),
    ('"1"', 1), ('"+1"', 1), ('"01"', 1), ('"1.5"', None), ('" 1 "', None),
    ('"true"', None), ('"2147483648"', None),
    (2147483647.9, 2147483647), (-2147483648.9, -2147483648),
    (2147483648.0, None), (-2147483649.0, None),
    ("{}", None), (2**31, None), ("2147483648", None), ("9" * 100, None),
])
def test_dp_status_integer_conversion_is_bounded(value, expected):
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.json_types import dp_integer

    assert dp_integer(value) == expected

@pytest.mark.parametrize("version", ["1.1", "2.1", "2.2", "2.3"])
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
    client._consume(b"9.9-invalid-wire-version")
    assert answers == []
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
    assert "answer=1,answer_without_sdp=1,invalid_shape=1,other_session_message=1,signal_query=1,version_mismatch=1" in client.diagnostics
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


@pytest.mark.parametrize(("version", "wire_hex"), [
    (
        "2.2",
        "322e32c3b773a30102030489abcdef6d91e50bf1cc7865a74577284a9ee9a3f"
        "4993974c362d1c591e4417eee97165aeda1c29613a5df7c77da4aebba181bdbc5"
        "bb170f5941d75d8ca59e79a0eae7d10e09874dfb6e77397931d975a323ba64",
    ),
    (
        "2.3",
        "322e330102030489abcdef00000102030405060708090a0b861da07c6a15190c"
        "93046f7fa1ffd55f22ed2cd873ca3a2e5553c89c89cdce1e7c6ae2c2d29eeaa"
        "81e48c4f1124f67125350250c405379473599b8547742ef7a8033b631084101"
        "fe70c5224f0444062c8931",
    ),
])
def test_mqtt_302_matches_apk_derived_java_wire_vector(version, wire_hex):
    # Generated with Java Cipher and CRC32 from the shared APK serializers.
    key = b"0123456789abcdef"
    data = {"header": {"type": "offer"}}
    expected = bytes.fromhex(wire_hex)
    assert encode_payload(
        key, 0x01020304, bytes.fromhex("89abcdef"), data, 1700000000, 302, version,
        nonce=bytes(range(12)),
    ) == expected
    assert decode_payload(key, expected) == (302, data)


@pytest.mark.parametrize("version", ["2.2", "2.3"])
@pytest.mark.parametrize("include_metadata", [False, True])
def test_mqtt_offer_preserves_apk_signaling_header(version, include_metadata):
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
        protocol_version=version,
        moto_id="camera-moto-id",
    )

    async def capture(topic, payload, *, qos):
        assert topic == "smart/mb/out/camera"
        assert qos == 0
        assert payload[:3] == version.encode()
        published.append(decode_payload(b"0123456789abcdef", payload))

    client._client = SimpleNamespace(publish=capture)
    metadata = {
        "token": [{"urls": "stun:fixture.invalid:3478"}],
        "tcp_token": {"sessionId": "fixture-relay"},
        "log": {"enabled": True},
    } if include_metadata else {}
    asyncio.run(client.async_send_offer(
        "sdp", metadata.get("token", []), "trace",
        metadata.get("tcp_token", {}), metadata.get("log", {}),
    ))

    assert published == [(302, {
        "header": {
            "type": "offer", "from": "account", "to": "camera",
            "sessionid": "session", "moto_id": "camera-moto-id",
            "trace_id": "trace", "is_pre": 0, "p2p_skill": 1635, "security_level": 3,
        },
        "msg": {"sdp": "sdp", "preconnect": True, **metadata},
    })]


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
