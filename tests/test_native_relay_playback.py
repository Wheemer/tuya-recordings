import asyncio
import json
import struct
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

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


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [(7, 0), (7, 21), (100, 20)])
async def test_completion_drains_media_and_ignores_other_tasks(command):
    playback = session()
    playback._connected = playback._playback_started = True
    playback._control = object()
    playback._stream_id = 0x20008
    playback._ended = asyncio.get_running_loop().create_future()

    def complete(stream_id, key=command):
        payload = struct.pack("<II", 0, 3)
        plain = struct.pack("<IIIHHI", 0x12345678, stream_id, 1, *key, len(payload)) + payload
        playback._on_control_record(encrypt_record(ANSWER_KEY, plain), ANSWER_KEY)

    complete(0x10008)
    complete(0x20008, (7, 4))
    assert not playback._playback_complete.is_set()
    frame = object()
    playback._media_frames.put_nowait(frame)
    complete(0x20008)
    assert f"{command[0]}/{command[1]}:3" in playback._playback_responses
    assert await playback.receive_frame() is frame
    with pytest.raises(relay_module.NativePlaybackCompleted):
        await asyncio.wait_for(playback.receive_frame(), 0.1)
    playback._reset_media_state()
    assert not playback._playback_complete.is_set()


@pytest.mark.asyncio
async def test_transport_failure_takes_priority_over_buffered_media_and_completion():
    playback = session()
    playback._connected = playback._playback_started = True
    playback._control = object()
    playback._ended = asyncio.get_running_loop().create_future()
    playback._ended.set_result(NativeRelayPlaybackError("fixture fatal authentication failure"))
    playback._playback_complete.set()
    playback._media_frames.put_nowait(object())
    with pytest.raises(NativeRelayPlaybackError, match="fatal authentication failure"):
        await playback.receive_frame()


@pytest.mark.asyncio
async def test_completion_wakes_blocked_media_receiver():
    playback = session()
    playback._connected = playback._playback_started = True
    playback._control = object()
    playback._stream_id = 0x20008
    playback._ended = asyncio.get_running_loop().create_future()
    receive = asyncio.create_task(playback.receive_frame())
    await asyncio.sleep(0)
    payload = struct.pack("<II", 0, 3)
    plain = struct.pack("<IIIHHI", 0x12345678, 0x20008, 1, 7, 21, 8) + payload
    playback._on_control_record(encrypt_record(ANSWER_KEY, plain), ANSWER_KEY)
    with pytest.raises(relay_module.NativePlaybackCompleted):
        await asyncio.wait_for(receive, 0.1)
APP_SESSION = {
    "sid": "sid",
    "ecode": "ecode",
    "uid": "account-uid",
    "partner_identity": "p1000018",
    "mobile_mqtts_url": "ssl://m1.tuyaus.com:8884",
    "device_fingerprint": "fingerprint",
}


class StatusBroker:
    """Use the real listener ownership contract in transport test doubles."""
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling.moto_client import MotoClient
    dp_report_listener = MotoClient.dp_report_listener
    _dp_query = None
    _dp_query_owner = None
    _reader_failure = "none"

    async def async_send_playback_wake(self):
        await self.async_send_wake()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["lan_ready", "cancel_wait", "report_during_wait"])
async def test_sd_cooldown_preserves_lan_fast_path_and_cancellable_wait(monkeypatch, outcome):
    from custom_components.tuya_recordings.lib.commands import CameraCommandQueue

    commands = CameraCommandQueue()
    monkeypatch.setattr(relay_module, "CAMERA_COMMANDS", commands)
    config = replace(camera_config(), wake_direct=True, sd_low_power_wake=True,
                     sd_status_mqtt_dp="110", sd_status_lan_dp="110" if outcome == "lan_ready" else "")
    assert commands.reserve_wake(config.dev_id)
    playback = NativeRelayPlaybackSession(config, APP_SESSION)
    playback._on_demand_catalog = True
    calls = []
    class Broker(StatusBroker):
        async def async_send_wake(self):
            pytest.fail("No wake may be published inside the cooldown")
        async def async_query_direct_dp(self, dp):
            pytest.fail("No MQTT SD query may precede its allowed wake")
    broker = Broker()
    async def lan_ready(dp, deadline, moto):
        calls.append("LAN status")
        playback._accept_sd_status_report(1)
        return True
    monkeypatch.setattr(playback, "_try_lan_sd_status", lan_ready)
    task = asyncio.create_task(playback._maybe_send_wake(broker))
    try:
        if outcome == "lan_ready":
            await asyncio.wait_for(task, 0.2)
            assert calls == ["LAN status"]
        else:
            async with asyncio.timeout(1):
                while playback._connect_phase != "wait_wake_cooldown":
                    await asyncio.sleep(0)
            assert playback._sd_status_deadline is None
            if outcome == "cancel_wait":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                broker._dp_query[1].set_result(1)
                await asyncio.wait_for(task, 0.2)
        assert not commands.wake_report_on_cooldown(config.dev_id)
        assert broker._dp_query is None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("background_status", [1, 5, None])
async def test_manual_lan_then_sleeping_background_reselects_lan_after_wake(monkeypatch, background_status):
    from custom_components.tuya_recordings.lib.commands import CameraCommandQueue

    commands = CameraCommandQueue()
    monkeypatch.setattr(relay_module, "CAMERA_COMMANDS", commands)
    monkeypatch.setattr(relay_module, "_SD_STATUS_TIMEOUT_SECONDS", 0.05)
    config = replace(camera_config(), wake_direct=True, sd_low_power_wake=True,
                     sd_status_mqtt_dp="110", sd_status_lan_dp="110")
    events = []
    awake = False
    background = False
    clients = []

    class Lan:
        diagnostics = "fixture LAN"
        def __init__(self, **kwargs):
            self.closed = False
            clients.append(self)
        async def async_connect(self):
            events.append("lan-connect")
            if not awake:
                raise relay_module.NativeLanUnavailableError("fixture sleeping")
        async def async_query_direct_dp(self, dp):
            assert dp == "110"
            events.append("lan-query")
            if background and background_status is None:
                await asyncio.Event().wait()
            return background_status if background else 1
        async def async_close(self):
            self.closed = True

    class Broker(StatusBroker):
        async def async_send_wake(self):
            nonlocal awake
            events.append("wake")
            awake = True
        async def async_query_direct_dp(self, dp):
            pytest.fail("Must not replace the newly available LAN route with MQTT")

    monkeypatch.setattr(relay_module, "Lan302Client", Lan)
    manual = NativeRelayPlaybackSession(config, APP_SESSION)
    manual._on_demand_catalog = True
    await manual._maybe_send_wake(Broker())
    assert events == ["wake", "lan-connect", "lan-query"]
    assert manual._sd_status_report_route == "lan"
    assert all(client.closed for client in clients)
    # The completed manual request leaves a wake reservation. The camera
    # sleeps before the next scheduled request, but inventory stays unchanged.
    background = True
    awake = False
    commands._wake_after[config.dev_id] = asyncio.get_running_loop().time() + 0.01
    events.clear()
    scheduled = NativeRelayPlaybackSession(config, APP_SESSION)
    scheduled._connector_wake_allowed = True
    broker = Broker()
    if background_status == 1:
        await scheduled._maybe_send_wake(broker)
        assert scheduled._sd_status_query == "report_received"
        assert scheduled._sd_status_report_route == "lan"
    else:
        with pytest.raises(NativeRelayPlaybackError, match="no_card" if background_status == 5 else "did not report"):
            await scheduled._maybe_send_wake(broker)
    assert events == ["lan-connect", "wake", "lan-connect", "lan-query"]
    assert scheduled._sd_status_send_route == "lan"
    assert scheduled._sd_status_route_basis == "inventory_lan"
    assert scheduled._sd_lan_attempts == 2
    assert scheduled._sd_lan_connect_failures == 1
    assert scheduled._sd_status_deadline is None
    assert broker._dp_query is None
    assert all(client.closed for client in clients)


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
async def test_dead_control_channel_fails_pending_seek_without_waiting_for_callback_timeout(monkeypatch):
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport.direct_session import DirectSession
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport import kcp_conversation as module
    playback = session()
    playback._ended = asyncio.get_running_loop().create_future()
    playback._connected = True
    now = 1000
    monkeypatch.setattr(module, "_now_milliseconds", lambda: now)
    direct = DirectSession(lambda _: None, on_failure=lambda reason: playback._fail_transport(
        NativeRelayPlaybackError(f"Native LAN delivery failed ({reason})")))
    playback._control = direct.control
    waiting = asyncio.create_task(playback._wait_for_playback_responses(0x20008))
    try:
        direct.control.send(b"seek")
        for _ in range(20):
            now += 201
            direct.control._retransmit_once()
        with pytest.raises(NativeRelayPlaybackError, match="delivery_exhausted"):
            await asyncio.wait_for(waiting, 0.1)
        with pytest.raises(NativeRelayPlaybackError, match="delivery_exhausted"):
            playback._require_connected()
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await direct.async_close()


@pytest.mark.parametrize("outcome", ["home", "resend", "candidate_resend", "silent", "wake", "http_wake", "lan_delivery_fail", "no_card", "missing", "hub", "lan_answer", "mqtt_and_lan_answer", "lan_answer_no_wake", "lan_answer_mqtt_status"])
@pytest.mark.parametrize("via_button", [False, True])
@pytest.mark.parametrize("profile", ["smart_life", "tuya_smart"])
@pytest.mark.parametrize("wire_preconnect", [None, False, True])
def test_refreshed_pv_completes_mqtt_handshake_and_catalog_without_restart(
    monkeypatch, tmp_path, caplog, outcome, via_button, profile, wire_preconnect,
    background_followup=False,
):
    import logging
    import re
    from custom_components.tuya_recordings.client import TuyaRecordingsClient
    from zlib import crc32

    from custom_components.tuya_recordings.lib.commands import (
        ON_DEMAND_DAY_CATALOG,
        CameraCommandQueue,
        on_demand_offer_retries,
    )
    from custom_components.tuya_recordings.lib.native_gateway import NativeAppGatewayClient
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling import moto_client
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling.envelope import (
        decode_payload,
        encode_payload,
    )

    config = camera_config()
    caplog.set_level(logging.INFO, logger="custom_components.tuya_recordings")
    dev = config.dev_id
    bootstrap = {
        **json.loads(config.config_json), "p2pSpecifiedType": 4, "p2pPolicy": 0, "skill": "{}",
    }
    # Both APKs use cJSON numeric conversion, including decimal-form numbers.
    bootstrap["p2pConfig"]["expire"] = float(bootstrap["p2pConfig"]["expire"])
    bootstrap["p2pConfig"]["skill"] = {"cloudP2P": 0.0}
    if wire_preconnect is not None:
        bootstrap["p2pConfig"]["preconnect"] = wire_preconnect
    offers = []
    refreshed_offers = []
    events = []
    detail_lookup = outcome != "home"
    dual = outcome in {"lan_answer", "mqtt_and_lan_answer", "lan_answer_no_wake", "lan_answer_mqtt_status"}
    wake = outcome in {"wake", "http_wake", "candidate_resend", "no_card", "lan_delivery_fail", "lan_answer", "mqtt_and_lan_answer", "lan_answer_mqtt_status"}
    low_power_metadata = outcome in {"wake", "http_wake", "candidate_resend", "no_card", "missing", "hub", "lan_delivery_fail"} or dual
    lan_events = ["lan-connect"] if outcome == "lan_answer_no_wake" else ["lan-connect", "lan-query", "lan-close"] if outcome == "lan_delivery_fail" else ["lan-connect", "lan-query"] if dual else []
    from queue import SimpleQueue, Empty
    lan_replies = SimpleQueue()
    day = date(2026, 9, 15)
    clip_start, clip_end = 1789459300, 1789459310
    key = config.local_key.encode()
    send_key = None
    brokers = []
    candidates = []
    background_sleeping = False

    def inventory_call(self, api, version, post, app_session=None, extra_params=None):
        assert self.app_profile_id == profile
        if api == "tuya.m.location.list":
            return [{"gid": 1}]
        if api == "tuya.m.my.group.device.list":
            pv = {"pv": "2.3"} if low_power_metadata else {} if detail_lookup else {"pv": "2.2"}
            return [{"devId": dev, "localKey": config.local_key, "category": "sp",
                     "productId": "fixture-product", **pv}]
        if api == "thing.m.device.product.ref.list":
            return [{"productId": "fixture-product", "configMetas": {"low_power_wakeup": True} if wake else {}}]
        if api == "thing.m.device.ref.info.list":
            assert version == "6.1"
            assert json.loads(post["productIds"]) == [{"pid": "fixture-product", "ver": "2.0"}]
            assert post["zigbeeGroup"] is True
            events.append("schema")
            return [{"id": "fixture-product", "productVer": "2.0", "schemaInfo": {
                "schema": '[{"id":110,"code":"sd_status"}]',
            }}]
        detail = {"devId": dev, "productId": "fixture-product", **({"productVer": "2.0"} if wake else {}),
                  "communication": {"communicationNode": dev, "dataModel": 1,
                                    "communicationModes": [{"type": 1, "pv": "2.3"}]},
                  "skills": {"p2pType": 2}, "dataPointInfo": {"dps": {"149": False} if wake else {}},
                  "gatewayVerCAD": "1.0.2", "cloudOnline": None if outcome == "http_wake" else True}
        detail["private_payload"] = {"access_token": "secret-token-not-for-logs"}
        if outcome == "lan_delivery_fail" or dual:
            detail["communication"]["communicationModes"].insert(0, {"type": 0, "pv": "3.5"})
        if outcome == "missing":
            detail.pop("skills")
            detail.pop("dataPointInfo")
            detail["communication"].pop("communicationNode")
        elif outcome == "hub":
            detail["communication"]["communicationNode"] = "fixture-hub"
            detail["dataPointInfo"]["dps"]["149"] = False
        if api == "m.life.app.smart.local.device.list":
            return {
                "deviceList": [] if detail_lookup else [detail],
                "productList": [] if wake else [{"id": "fixture-product", "schemaInfo": {
                    "schema": '[{"id":"110","code":"sd_status"}]',
                }}],
            }
        assert detail_lookup
        assert (api, version, post, extra_params) == (
            "thing.m.device.get", "4.1", {"devId": dev}, {"gid": "1"},
        )
        return detail

    class Broker(StatusBroker):
        def __init__(self, **_kwargs):
            self.inbound = asyncio.Queue()
            brokers.append(self)

        async def __aenter__(self):
            self.loop = asyncio.get_running_loop()
            return self

        async def __aexit__(self, *_args):
            events.append("broker-closed")

        async def subscribe(self, topic, *, qos, timeout):
            assert qos == 1
            if isinstance(topic, str):
                assert topic == f"smart/mb/in/{dev}"
                return (1,)
            assert topic == [("smart/mb/account-uid", 1), ("p1000018/mb/account-uid", 1)]
            return (1, 1)

        async def publish(self, topic, payload, *, qos, **kwargs):
            nonlocal background_sleeping
            if topic == f"m/w/{dev}":
                assert wake and qos == 1 and kwargs["retain"] is False
                assert payload == crc32(key).to_bytes(4, "big")
                events.append("wake")
                background_sleeping = False
                if background_followup:
                    # Barry's log: online/lowPowerConnect and unrelated DP248
                    # reports are not the requested SD-status completion.
                    for protocol, report in (
                        (1, {"online": True, "lowPowerConnect": True}),
                        (4, {"dps": {"248": 1}}),
                    ):
                        self.inbound.put_nowait(SimpleNamespace(
                            topic=f"smart/mb/in/{dev}",
                            payload=encode_payload(key, 1, b"test", {"devId": dev, **report}, 0, protocol, "2.3"),
                        ))
                if outcome == "lan_answer_mqtt_status":
                    self.inbound.put_nowait(SimpleNamespace(
                        topic=f"smart/mb/in/{dev}",
                        payload=encode_payload(key, 1, b"test", {"devId": dev, "online": True,
                                               "lowPowerConnect": True}, 0, 1, "2.3"),
                    ))
                return
            assert topic == f"smart/mb/out/{dev}"
            protocol, body = decode_payload(key, payload)
            if protocol == 5:
                assert wake and qos == 1 and payload[:3] == b"2.3"
                assert body["dps"] == {"110": None}
                assert events == (["schema"] if via_button else []) + ["wake"] + lan_events
                events.append("sd-query")
                reply = encode_payload(
                    key, 1, b"test", {"devId": dev, "dps": {"110": "5" if outcome == "no_card" else "1"}},
                    0, 4, "2.2",
                )
                self.inbound.put_nowait(SimpleNamespace(topic=f"smart/mb/in/{dev}", payload=reply))
                return
            assert qos == 0
            assert protocol == 302
            header = body["header"]
            # Native a81b8/aba90 -> a7084 -> P2PMQTTServiceManager -> rsv:
            # MQTT passes the original header; only the relay copy adds path.
            assert "path" not in header
            if header["type"] == "disconnect":
                return
            if header["type"] == "candidate":
                assert outcome == "candidate_resend"
                candidates.append(body)
                if len(candidates) == 2:
                    reply = encode_payload(
                        key, 1, b"test",
                        {"header": {"type": "answer", "sessionid": header["sessionid"]},
                         "msg": {"sdp": answer_sdp()}}, 0, 302, "2.3",
                    )
                    self.inbound.put_nowait(SimpleNamespace(topic=f"smart/mb/in/{dev}", payload=reply))
                return
            assert header["type"] == "offer"
            # The native builder reads the token boolean, not connect_v3's
            # independently enabled preconnection optimization.
            assert body["msg"]["preconnect"] is (wire_preconnect is True)
            offers.append(payload[:3])
            events.append("offer")
            if payload[:3] != b"2.3":
                return
            refreshed_offers.append(body)
            if dual:
                lan_replies.put({
                    "header": {"type": "answer", "sessionid": "wrong-session"},
                    "msg": {"sdp": answer_sdp()},
                })
                lan_replies.put({
                    "header": {"type": "answer", "sessionid": header["sessionid"]},
                    "msg": {"sdp": answer_sdp()},
                })
                if outcome in {"lan_answer", "lan_answer_no_wake", "lan_answer_mqtt_status"}:
                    return
            if outcome in {"home", "wake", "http_wake", "missing", "hub", "lan_delivery_fail", "mqtt_and_lan_answer"} or (outcome == "resend" and len(refreshed_offers) == 2):
                reply = encode_payload(
                    key, 1, b"test",
                    {"header": {"type": "answer", "sessionid": header["sessionid"]},
                     "msg": {"sdp": answer_sdp()}},
                    0, 302, "2.3",
                )
                self.inbound.put_nowait(SimpleNamespace(topic=f"smart/mb/in/{dev}", payload=reply))

        @property
        async def messages(self):
            while True:
                yield await self.inbound.get()

    class Ice:
        diagnostics = "fixture ICE"

        def __init__(self, _password, _candidate, **kwargs):
            nonlocal send_key
            send_key = kwargs["send_key"]
            self.receive_key = None
            self.candidate_callback = _candidate

        async def async_gather(self):
            if outcome == "candidate_resend":
                self.candidate_callback("candidate:1 1 udp 2130706431 192.0.2.9 12345 typ host")

        def set_receive_key(self, value):
            assert value == ANSWER_KEY
            self.receive_key = value
            events.append("answer")

        def set_remote_credentials(self, ufrag, password):
            self.credentials = (ufrag, password)

        def add_remote_candidate(self, candidate):
            pass

        async def async_wait_for_nomination(self, _timeout):
            assert self.receive_key == ANSWER_KEY
            events.append("nominated")

        def send_segment(self, _segment):
            pytest.fail("Fixture must not send direct network packets")

        def close(self):
            pass

    class Direct:
        diagnostics = "fixture direct transport"

        def __init__(self, _transmit, *, on_failure=None):
            self.on_failure = on_failure
            self.control = self
            self.channels = {}

        def conversation(self, number):
            if number not in self.channels:
                self.channels[number] = SimpleNamespace(
                    receive_sequence=0,
                    set_message_handler=lambda handler: setattr(self.channels[number], "handler", handler),
                )
            return self.channels[number]

        async def async_wait_for_video(self, _timeout):
            return self.conversation(1)

        def set_message_handler(self, handler):
            self.handler = handler

        def send(self, record):
            command = decrypt_record(send_key, record)
            _, request, _, high, low, _ = struct.unpack_from("<IIIHHI", command)
            if high in (6, 7):
                reply = struct.pack("<IIIHHIii", 0x12345678, request, 1, high, low, 8, 0, 1)
                self.handler(encrypt_record(ANSWER_KEY, reply))
                if high == 7 and low == 4 and 1 in self.channels:
                    for channel, payload_type, payload in (
                        (1, 96, b"\x65video"), (2, 99, b"\x00\x00\xff\x7f"),
                    ):
                        header = struct.pack("<I5I", request, 0, 0, 0, 0, 0)
                        rtp = struct.pack(">BBHII", 0x80, 0x80 | payload_type, 1, 8000, 1) + payload
                        self.channels[channel].handler(encrypt_record(
                            ANSWER_KEY, header + struct.pack("<I", len(rtp)) + rtp,
                        ))
                return
            if (high, low) != (3, 1):
                if background_followup and (high, low) == (3, 0):
                    assert struct.unpack_from("<4i", command, 20) == (0, 0, 0, 202609)
                    events.append("month")
                    payload = struct.pack("<4I", 0, 0, 0, 1 << 14)
                    self.handler(encrypt_record(ANSWER_KEY, struct.pack(
                        "<IIIHHI", 0x12345678, request, 1, 3, 0, len(payload),
                    ) + payload))
                return
            assert events[-1] == "nominated"
            assert struct.unpack_from("<4I", command, 20) == (0, day.year, day.month, day.day)
            events.append("catalog")
            payload = struct.pack("<9I", 0, day.year, day.month, day.day, 1, 0, clip_start, clip_end, 0)
            reply = struct.pack("<IIIHHI", 0x12345678, request, 1, 3, 1, len(payload)) + payload
            self.handler(encrypt_record(ANSWER_KEY, reply))

        async def async_close(self):
            events.append("direct-closed")

    if outcome == "lan_delivery_fail":
        class FailedLanDelivery:
            def __init__(self, **kwargs):
                pass

            async def async_connect(self):
                events.append("lan-connect")

            async def async_listen(self):
                events.append("lan-listen")

            async def async_query_direct_dp(self, dp):
                assert dp == "110"
                events.append("lan-query")
                raise relay_module.NativeLanSignalError("fixture LAN acknowledgement timeout")

            async def async_close(self):
                events.append("lan-close")

        monkeypatch.setattr(relay_module, "Lan302Client", FailedLanDelivery)
    elif dual:
        import tinytuya

        def open_device(self):
            device = tinytuya.Device(dev, "192.0.2.9", config.local_key, version=3.5)
            closed = False

            def close():
                nonlocal closed
                # TinyTuya's destructor calls close again during GC. Match
                # real socket idempotence rather than logging phantom closes.
                if not closed:
                    closed = True
                    events.append("lan-close")

            def get_socket(renew):
                events.append("lan-connect")
                return not background_sleeping

            def query(payload):
                assert wake, "Passive answer listening must not query SD status"
                assert payload.cmd == 7
                assert json.loads(payload.payload)["data"]["dps"] == {"110": None}
                events.append("lan-query")
                device.cmd_retcode = 0
                if outcome == "lan_answer_mqtt_status":
                    broker = brokers[-1]
                    broker.loop.call_soon_threadsafe(broker.inbound.put_nowait, SimpleNamespace(
                        topic=f"smart/mb/in/{dev}",
                        payload=encode_payload(key, 2, b"test",
                                               {"devId": dev, "dps": {"110": 1}}, 0, 4, "2.3"),
                    ))
                    return None
                return {"data": {"dps": {"110": 1}}}

            def receive():
                try:
                    return lan_replies.get_nowait()
                except Empty:
                    return None

            device._get_socket = get_socket
            device._send_receive = query
            device.receive = receive
            device.close = close
            device.send = lambda _: pytest.fail("Receive-only LAN must not publish signaling")
            return device

        monkeypatch.setattr(relay_module.Lan302Client, "_open_device", open_device)
    monkeypatch.setattr(NativeAppGatewayClient, "call", inventory_call)
    monkeypatch.setattr(moto_client.aiomqtt, "Client", Broker)
    monkeypatch.setattr(relay_module, "IceResponder", Ice)
    monkeypatch.setattr(relay_module, "DirectSession", Direct)
    monkeypatch.setattr(relay_module, "CAMERA_COMMANDS", CameraCommandQueue())
    monkeypatch.setattr(relay_module, "_OFFER_RETRY_OFFSETS",
                        (0.35, 0.4, 0.45, 0.5) if dual else (0.03, 0.06, 0.12, 0.24))
    # Leave scheduling headroom when asserting every shortened resend fires.
    monkeypatch.setattr(relay_module, "_ANSWER_TIMEOUT_SECONDS", 1.0 if outcome == "silent" else 0.6 if dual else 0.32)
    def native_call(api, version, body, extra):
        if api == "thing.m.device.dp.publish":
            assert outcome == "http_wake" and body["dps"] == '{"110":null}'
            events.append("sd-query")
            broker = brokers[-1]
            broker.loop.call_soon_threadsafe(broker.inbound.put_nowait, SimpleNamespace(
                topic=f"smart/mb/in/{dev}",
                payload=encode_payload(key, 1, b"test", {"devId": dev, "dps": {"110": 1}}, 0, 4, "2.3"),
            ))
            return True
        return bootstrap

    entry_data = {
        "app_profile": profile,
        "_native_app_call": native_call,
        "native_app_session": APP_SESSION,
        "device_local_keys": {dev: config.local_key},
        "device_protocol_versions": {dev: "2.2"},
    }
    client = TuyaRecordingsClient(entry_data, cache_path=tmp_path / "catalog.json")

    async def offer_from_current_backend():
        resolved = client._recordings_backend._provider.session_config(dev)
        playback = NativeRelayPlaybackSession(resolved, APP_SESSION)
        try:
            await playback._open_signaling("sdp", [], "trace", {}, {})
        finally:
            if playback._moto is not None:
                await playback._moto.async_close()

    asyncio.run(offer_from_current_backend())
    assert offers == [b"2.2"]
    if not via_button:
        assert client.refresh_inventory(entry_data)["device_protocol_versions"] == {dev: "2.3"}
    events.clear()
    error = ""
    if via_button:
        from unittest.mock import Mock
        from custom_components import tuya_recordings as integration
        from custom_components.tuya_recordings import client as client_module
        from custom_components.tuya_recordings.button import TuyaRecordingsRefreshButton
        from datetime import datetime, timezone

        entry = SimpleNamespace(entry_id="fixture", title="Fixture", data=entry_data, options={})
        handlers = {}

        async def execute(function, *args):
            return await asyncio.get_running_loop().run_in_executor(None, function, *args)

        async def call_service(domain, name, data, *, blocking):
            assert blocking
            await handlers[name](SimpleNamespace(data=data))

        services = SimpleNamespace(
            has_service=lambda *_: False,
            async_register=lambda domain, name, handler, **kw: handlers.update({name: handler}),
            async_call=call_service,
        )
        hass = SimpleNamespace(
            services=services,
            data={integration.DOMAIN: {"fixture": {"entry": entry, "client": client}}},
            config_entries=SimpleNamespace(
                async_update_entry=lambda entry, *, data: setattr(entry, "data", data),
            ),
            async_add_executor_job=execute, async_create_task=asyncio.create_task,
        )
        monkeypatch.setattr(integration, "async_dispatcher_send", Mock())
        monkeypatch.setattr(integration, "CATALOG_SYNC_DAYS_PER_PASS", 1)
        monkeypatch.setattr(client_module.dt_util, "now", lambda: datetime(2026, 9, 15, tzinfo=timezone.utc))
        integration._async_register_services(hass)
        try:
            asyncio.run(TuyaRecordingsRefreshButton(hass, entry).async_press())
            assert entry.data["device_protocol_versions"] == {dev: "2.3"}
            wake_metadata = entry.data["device_wake_capabilities"][dev]
            if outcome == "missing":
                assert "sd_low_power" not in wake_metadata
                assert wake_metadata["evidence"] == {
                    "source": "detail", "route": "missing", "provider_two": None,
                    "dp149": "missing", "sd_status_schema": True,
                }
            elif outcome == "hub":
                assert wake_metadata["sd_low_power"] is False
                assert wake_metadata["direct"] is False
                assert wake_metadata["evidence"]["route"] == "hub"
            camera = client.cached_camera_index()["cameras"][0]
            clips = camera["clips"]
            error = camera["bridgeStatus"]["error"]
            if background_followup:
                import time
                from datetime import timedelta
                assert not error and clips
                assert camera["catalogDays"].get(day.isoformat())
                client.lookback_days = 7
                for iteration in range(3):
                    if iteration:
                        day = date(2026, 9, 15)
                        events.clear()
                        client._manual_catalog_after.clear()
                        asyncio.run(TuyaRecordingsRefreshButton(hass, entry).async_press())
                        assert not client.cached_camera_index()["cameras"][0]["bridgeStatus"]["error"]
                    background_sleeping = True
                    relay_module.CAMERA_COMMANDS._wake_after[dev] = time.monotonic() + 0.2
                    events.clear()
                    # Same real client, provider and command queue: load the
                    # next historical day after manual refresh and camera sleep.
                    day = date(2026, 9, 14) - timedelta(days=iteration)
                    result = client.refresh_background_catalog(limit=1)
                    updated = result["cameras"][0]
                    assert not updated["bridgeStatus"]["error"]
                    assert day.isoformat() in updated["catalogDays"]
                    assert events == [
                        "lan-connect", "lan-close", "wake", "lan-connect", "lan-query",
                        "offer", "answer", "nominated", "catalog", "direct-closed",
                        "lan-close", "broker-closed",
                    ], events

                    async def playback_lifecycle():
                        viewer = NativeRelayPlaybackSession(
                            client._recordings_backend._provider.session_config(dev), APP_SESSION,
                        )
                        try:
                            await viewer.connect(allow_wake=True)
                            await viewer.start(start=clip_start, end=clip_end)
                            for seek in (False, True, True):
                                if seek:
                                    await viewer.seek(start=clip_start, end=clip_end, play_time=clip_start + 1)
                                async with asyncio.timeout(1):
                                    video = await viewer.receive_frame()
                                    audio = await viewer.receive_frame()
                                assert not video.is_audio and audio.is_audio
                                assert audio.payload == b"\x00\x00\xff\x7f"
                                await viewer.pause()
                                await viewer.resume()
                            await viewer.stop()
                        finally:
                            await viewer.close()
                        assert not viewer._candidate_tasks
                        assert viewer._session is viewer._moto is viewer._answer_lan is None

                    asyncio.run(playback_lifecycle())
                    background_sleeping = True
                    relay_module.CAMERA_COMMANDS._wake_after[dev] = time.monotonic() + 0.2
                    events.clear()
                    assert client._recordings_backend.recording_days_for_month(
                        dev, date(2026, 9, 1),
                    ) == [date(2026, 9, 14)]
                    import gc
                    gc.collect()
                    expected_month_events = [
                        "lan-connect", "lan-close", "wake", "lan-connect", "lan-query",
                        "offer", "answer", "nominated", "month", "direct-closed",
                        "lan-close", "broker-closed",
                    ]
                    if events != expected_month_events:
                        pytest.fail("Unexpected month lifecycle: " + json.dumps(events))
        finally:
            client._recordings_backend.close()
        if background_followup:
            return
        start = next(record.getMessage() for record in caplog.records if "manual refresh started" in record.getMessage())
        request_id = re.search(r"request_id=([a-f0-9]{12})", start).group(1)
        for milestone in ("refreshed SD wake metadata", "catalog query started", "manual refresh finished"):
            assert any(milestone in record.getMessage() and f"request_id={request_id}" in record.getMessage()
                       for record in caplog.records)
        assert config.local_key not in caplog.text
        assert config.camera_password not in caplog.text
        assert "secret-token-not-for-logs" not in caplog.text
        assert "private_payload" not in caplog.text
        if outcome in {"silent", "no_card"}:
            failed = next(record for record in caplog.records if "catalog query failed" in record.getMessage())
            assert f"request_id={request_id}" in failed.getMessage()
            assert "fresh_attempt=True" in failed.getMessage()
            assert failed.exc_info is not None
            assert "wake_scope_enabled=True" in caplog.text
            assert "offer_resend_enabled=True" in caplog.text
        if outcome == "missing":
            assert "'route': 'missing'" in caplog.text
            assert "'provider_two': None" in caplog.text
            assert "'dp149': 'missing'" in caplog.text
    else:
        backend = client._recordings_backend
        token = ON_DEMAND_DAY_CATALOG.set(wake)
        try:
            with on_demand_offer_retries():
                if outcome == "silent":
                    with pytest.raises(NativeRelayPlaybackError, match="Timed out waiting") as exc:
                        backend.recordings_for_day(dev, {}, {}, day)
                    error = str(exc.value)
                elif outcome == "no_card":
                    with pytest.raises(NativeRelayPlaybackError, match="report_no_card"):
                        backend.recordings_for_day(dev, {}, {}, day)
                else:
                    clips = backend.recordings_for_day(dev, {}, {}, day)
        finally:
            ON_DEMAND_DAY_CATALOG.reset(token)
            backend.close()
    if outcome == "no_card":
        if via_button:
            assert "report_no_card" in error and clips == []
        assert offers == [b"2.2"]
        assert events == (["schema"] if via_button else []) + ["wake", "sd-query", "broker-closed"]
        return
    count = {"home": 1, "resend": 2, "candidate_resend": 2, "silent": 5, "wake": 1, "http_wake": 1, "missing": 1, "hub": 1, "lan_delivery_fail": 1, "lan_answer": 1, "mqtt_and_lan_answer": 1, "lan_answer_no_wake": 1, "lan_answer_mqtt_status": 1}[outcome]
    if outcome == "silent":
        # The real-loop transaction may skip elapsed retry slots by design.
        # Exact timing is checked separately; never demand catch-up traffic.
        count = len(offers) - 1  # Exclude the deliberately stale PV 2.2 offer.
        assert 1 <= count <= 5
    if outcome == "candidate_resend":
        assert len(candidates) == 2 and candidates[0] == candidates[1]
    assert offers == [b"2.2"] + [b"2.3"] * count
    assert refreshed_offers == [refreshed_offers[0]] * count
    if outcome == "silent":
        assert "offer_retries=4" in error
        assert "received=0" in error
        assert events == ["offer"] * count + ["direct-closed", "broker-closed"]
    else:
        prefix = ((["schema"] if via_button else []) + ["wake"] + lan_events + ([] if dual else ["sd-query"])) if wake else lan_events
        assert events == prefix + ["offer"] * count + ["answer", "nominated", "catalog", "direct-closed"] + (["lan-close"] if dual else []) + ["broker-closed"]
        assert [(clip["start"], clip["end"]) for clip in clips] == [(clip_start, clip_end)]
        if outcome in {"wake", "lan_delivery_fail"} or dual:
            async def play_after_refreshed_catalog():
                playback = NativeRelayPlaybackSession(
                    client._recordings_backend._provider.session_config(dev), APP_SESSION,
                )
                try:
                    await playback.connect()
                    await playback.start(start=clip_start, end=clip_end)
                    async with asyncio.timeout(1):
                        video = await playback.receive_frame()
                        audio = await playback.receive_frame()
                    assert video.payload == b"\x00\x00\x00\x01\x65video"
                    assert audio.is_audio and audio.payload == b"\x00\x00\xff\x7f"
                finally:
                    await playback.close()

            asyncio.run(play_after_refreshed_catalog())


@pytest.mark.parametrize("profile", ["smart_life", "tuya_smart"])
def test_manual_button_to_previous_day_background_lan_catalog(monkeypatch, tmp_path, caplog, profile):
    test_refreshed_pv_completes_mqtt_handshake_and_catalog_without_restart(
        monkeypatch, tmp_path, caplog, outcome="lan_answer", via_button=True,
        profile=profile, wire_preconnect=True, background_followup=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_preconnect", [None, False, True])
async def test_initial_http_token_precedes_single_mqtt_offer(monkeypatch, wire_preconnect):
    calls = []
    captured = {}
    config = camera_config()
    token = json.loads(config.token)
    token["expire"] = 0
    token["preconnect"] = True
    token["skill"] = {"cloudP2P": 1.0}
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
        if wire_preconnect is not None:
            result["preconnect"] = wire_preconnect
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
    assert calls == ["mqtt", "http", "offer"]
    assert captured["signaling"]["session_id"] == captured["header"]["sessionid"]
    assert captured["signaling"]["moto_id"] == "renewed-moto"
    assert captured["signaling"]["preconnect"] is (wire_preconnect is True)
    assert playback._native_config.token_route.cloud_p2p == 0
    assert "cloud_p2p=False" in playback.transport_diagnostics
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


@pytest.mark.parametrize("fields,expected,cloud,numeric", [
    ({"expire": 1800000000.0}, None, False, True),
    ({"expire": 1800000000.0, "skill": {"cloudP2P": 1.0}}, "http_cloud_p2p", True, True),
    ({"expire": 1800000000.0, "skill": {"cloudP2P": 0.9}}, None, False, True),
    ({"expire": 0.0}, "http_expired_token", False, True),
    ({"expire": 1700000000.9}, "http_expired_token", False, True),
    ({"expire": True}, "http_missing_expiry", False, False),
    ({"expire": "1800000000"}, "http_missing_expiry", False, False),
    ({}, "http_missing_expiry", False, False),
])
@pytest.mark.parametrize("nested", [False, True])
def test_route_selection_and_logging_use_same_native_numbers(
    monkeypatch, fields, expected, cloud, numeric, nested,
):
    monkeypatch.setattr(relay_module.time, "time", lambda: 1700000000)
    token = {"session": fields} if nested else fields
    playback = NativeRelayPlaybackSession(
        replace(camera_config(), token=json.dumps(token)), APP_SESSION,
    )
    assert playback._http_token_reason() == expected
    note = playback.transport_diagnostics
    assert f"cloud_p2p={cloud}" in note
    assert f"expiry_numeric={numeric}" in note
    assert f"token_selection={expected or 'app_token'}" in note


@pytest.mark.asyncio
@pytest.mark.parametrize("bootstrap", [False, True])
async def test_initial_cloud_session_never_opens_ordinary_signaling(monkeypatch, bootstrap):
    from unittest.mock import AsyncMock

    config = camera_config()
    token = json.loads(config.token)
    token.update(isCloudSession=True, preconnect=True)
    playback = NativeRelayPlaybackSession(
        replace(config, token=json.dumps(token)), APP_SESSION,
    )
    connect = AsyncMock()
    monkeypatch.setattr(playback, "_connect_mqtt", connect)
    with pytest.raises(NativeRelayPlaybackError, match="server-managed"):
        if bootstrap:
            await playback._initialize_http_offer("trace")
        else:
            await playback._open_signaling("sdp", [], "trace", {}, {})
    connect.assert_not_called()


@pytest.mark.parametrize("nested", [False, True])
def test_token_route_diagnostics_are_credential_free(nested):
    config = camera_config()
    token = json.loads(config.token)
    target = token["session"] if nested else token
    target["preconnect"] = True
    config = replace(config, token=json.dumps(token))
    playback = NativeRelayPlaybackSession(config, APP_SESSION)
    note = playback.transport_diagnostics
    assert "preconnect=True, offer_preconnect=True" in note
    assert f"preconnect_source={'session' if nested else 'root'}" in note
    assert "session_identity=generated_local" in note
    for secret in (config.local_key, config.app_sid, config.app_uid,
                   config.camera_password, token["session"]["sessionId"],
                   token["session"]["icePassword"], token["session"]["aesKey"]):
        assert secret not in note


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allowed", "capability", "direct", "eligible", "expected"),
    [
        (False, "low_power", True, True, ["subscribe", "offer"]),
        (True, "low_power", True, None, ["subscribe", "offer"]),
        (True, "wireless", True, False, ["subscribe", "offer"]),
        (True, "low_power", False, True, ["subscribe", "offer"]),
        (True, "unknown", True, True, ["subscribe", "wake", "offer"]),
    ],
)
async def test_wake_only_for_confirmed_direct_on_demand_catalog(
    monkeypatch, allowed, capability, direct, eligible, expected,
):
    from custom_components.tuya_recordings.lib.commands import CameraCommandQueue

    calls = []

    class Broker(StatusBroker):
        def __init__(self, **kwargs):
            pass

        async def async_connect(self):
            calls.append("subscribe")

        async def async_send_wake(self):
            calls.append("wake")

        async def async_query_direct_dp(self, dp):
            assert dp == "110"
            calls.append("sd_status")
            return 1

        async def async_send_offer(self, *args):
            calls.append("offer")

    monkeypatch.setattr(relay_module, "MotoClient", Broker)
    monkeypatch.setattr(relay_module, "CAMERA_COMMANDS", CameraCommandQueue())
    config = replace(
        camera_config(), wake_capability=capability, wake_direct=direct,
        sd_low_power_wake=eligible, sd_status_mqtt_dp="110",
    )
    for _ in range(2):
        playback = NativeRelayPlaybackSession(config, APP_SESSION)
        playback._on_demand_catalog = allowed
        await playback._open_signaling("sdp", [], "trace", {}, {})
        assert (playback._retry_offer is not None) == allowed
        # Shorten only the fixture's next reservation, retaining a distinct
        # per-request wake before its SD query on non-LAN connections.
        if "wake" in expected:
            relay_module.CAMERA_COMMANDS._wake_after[config.dev_id] = relay_module.time.monotonic() + 0.01
    if "wake" in expected:
        expected = ["subscribe", "wake", "sd_status", "offer"]
        assert calls == [*expected, *expected]
    else:
        assert calls == [*expected, "subscribe", "offer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("capability", ["low_power", "remote", "low_power_and_remote", "wireless", "unknown", "none"])
async def test_connector_wake_is_foreground_direct_and_deduplicated(monkeypatch, allowed, direct, capability):
    from custom_components.tuya_recordings.lib.commands import CameraCommandQueue

    calls = []
    monkeypatch.setattr(relay_module, "CAMERA_COMMANDS", CameraCommandQueue())
    config = replace(camera_config(), wake_capability=capability, wake_direct=direct)

    class Broker(StatusBroker):
        async def async_send_wake(self):
            calls.append("wake")

    playback = NativeRelayPlaybackSession(config, APP_SESSION)
    playback._connector_wake_allowed = allowed
    await playback._maybe_send_connector_wake(Broker())
    second = NativeRelayPlaybackSession(config, APP_SESSION)
    second._connector_wake_allowed = allowed
    await second._maybe_send_connector_wake(Broker())
    assert calls == (["wake"] if allowed and direct and capability not in {"unknown", "none"} else [])


@pytest.mark.asyncio
async def test_connector_and_sd_presenter_share_one_wake_but_keep_status_query(monkeypatch):
    from custom_components.tuya_recordings.lib.commands import CameraCommandQueue

    calls = []
    monkeypatch.setattr(relay_module, "CAMERA_COMMANDS", CameraCommandQueue())
    playback = NativeRelayPlaybackSession(replace(
        camera_config(), wake_capability="low_power", wake_direct=True,
        sd_low_power_wake=True, sd_status_mqtt_dp="247",
    ), APP_SESSION)
    playback._connector_wake_allowed = True
    playback._on_demand_catalog = True

    class Broker(StatusBroker):
        async def async_send_wake(self):
            calls.append("wake")

        async def async_query_direct_dp(self, dp):
            calls.append(("query", dp))
            return 1

    await playback._maybe_send_connector_wake(Broker())
    await playback._maybe_send_wake(Broker())
    assert calls == ["wake", ("query", "247")]
    assert playback._sd_status_query == "report_received"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failure", "cancel", "report_cooldown"])
async def test_connector_wake_failure_has_no_detached_retry(monkeypatch, outcome):
    from custom_components.tuya_recordings.lib.commands import CameraCommandQueue
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.exceptions import TuyaIpcP2pError

    commands = CameraCommandQueue()
    monkeypatch.setattr(relay_module, "CAMERA_COMMANDS", commands)
    config = replace(camera_config(), wake_capability="low_power", wake_direct=True)
    playback = NativeRelayPlaybackSession(config, APP_SESSION)
    playback._connector_wake_allowed = True
    calls = []

    class Broker(StatusBroker):
        async def async_send_wake(self):
            calls.append("wake")
            if outcome == "cancel":
                raise asyncio.CancelledError()
            raise TuyaIpcP2pError("fixture send failed")

    if outcome == "report_cooldown":
        commands.reserve_wake(config.dev_id)
        commands.mark_wake_report_missing(config.dev_id)
        with pytest.raises(NativeRelayPlaybackError, match="cooldown"):
            await playback._maybe_send_connector_wake(Broker())
        assert calls == []
    elif outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await playback._maybe_send_connector_wake(Broker())
        assert calls == ["wake"]
    else:
        await playback._maybe_send_connector_wake(Broker())
        assert playback._connector_wake_status == "send_failed"
        await playback._maybe_send_connector_wake(Broker())
        assert calls == ["wake"]
    assert not playback._connector_wake_sent


@pytest.mark.asyncio
async def test_sd_timeout_cooldown_blocks_connect_before_mqtt_or_http(monkeypatch):
    from custom_components.tuya_recordings.lib.commands import CameraCommandQueue

    commands = CameraCommandQueue()
    monkeypatch.setattr(relay_module, "CAMERA_COMMANDS", commands)
    config = camera_config()
    token = json.loads(config.token)
    token["expire"] = 0
    commands.reserve_wake(config.dev_id)
    commands.mark_wake_report_missing(config.dev_id)
    playback = NativeRelayPlaybackSession(
        replace(config, token=json.dumps(token)), APP_SESSION,
        call_api=lambda *_: pytest.fail("Cooldown must not initialize an HTTP offer"),
    )

    class Broker(StatusBroker):
        def __init__(self, **kwargs):
            pytest.fail("Cooldown must not reconnect MQTT")

    monkeypatch.setattr(relay_module, "MotoClient", Broker)
    with pytest.raises(NativeRelayPlaybackError, match="cooldown"):
        await playback.connect(media=False, allow_wake=True)
    assert playback._closed and playback._moto is None
    assert playback._connector_wake_status == "report_timeout_cooldown"


@pytest.mark.asyncio
async def test_unknown_device_pv_blocks_mqtt_before_broker_connection(monkeypatch):
    def broker(**_kwargs):
        pytest.fail("Unknown DeviceBean PV must not open MQTT signaling")

    monkeypatch.setattr(relay_module, "MotoClient", broker)
    config = replace(camera_config(), mqtt_protocol_version="")
    playback = NativeRelayPlaybackSession(config, APP_SESSION)
    with pytest.raises(NativeRelayPlaybackError, match="MQTT protocol version is unknown"):
        await playback._connect_mqtt("fixture-session")
    assert "mqtt_pv=unknown" in playback.transport_diagnostics


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "reason"), [
    (2, "card_error"), (4, "formatting"), (5, "no_card"),
    (7, "loading"), (8, "unavailable"), (3, None), (None, "timeout"),
])
async def test_catalog_offer_respects_reported_sd_status(monkeypatch, status, reason):
    from types import SimpleNamespace

    from custom_components.tuya_recordings.lib import commands
    from custom_components.tuya_recordings.lib.commands import CameraCommandQueue

    calls = []
    now = 100.0
    monkeypatch.setattr(commands, "time", SimpleNamespace(monotonic=lambda: now))

    class Broker(StatusBroker):
        def __init__(self, **kwargs):
            pass

        async def async_connect(self):
            pass

        async def async_send_wake(self):
            calls.append("wake")

        async def async_query_direct_dp(self, dp):
            calls.append("query")
            if status is None and calls.count("query") == 1:
                raise TimeoutError
            return 1 if status is None else status

        async def async_send_offer(self, *args):
            calls.append("offer")

    monkeypatch.setattr(relay_module, "MotoClient", Broker)
    monkeypatch.setattr(relay_module, "CAMERA_COMMANDS", CameraCommandQueue())
    config = replace(
        camera_config(), wake_direct=True, sd_low_power_wake=True, sd_status_mqtt_dp="110",
    )
    playback = NativeRelayPlaybackSession(config, APP_SESSION)
    playback._on_demand_catalog = True
    if reason == "timeout":
        with pytest.raises(NativeRelayPlaybackError, match="did not report SD status"):
            await playback._open_signaling("sdp", [], "trace", {}, {})
        assert playback._sd_status_query == "report_timeout"
        assert playback._retry_offer is None
        now = 159.999
        repeated = NativeRelayPlaybackSession(config, APP_SESSION)
        repeated._on_demand_catalog = True
        with pytest.raises(NativeRelayPlaybackError, match="retry is on cooldown"):
            await repeated._open_signaling("sdp", [], "trace", {}, {})
        assert repeated._retry_offer is None
        assert calls == ["wake", "query"]
        now = 160.0
        fresh = NativeRelayPlaybackSession(config, APP_SESSION)
        fresh._on_demand_catalog = True
        fresh._answer = asyncio.get_running_loop().create_future()
        await fresh._open_signaling("sdp", [], "trace", {}, {})
        assert fresh._sd_status_query == "report_received"
        assert fresh._retry_offer is not None
        answer = object()
        fresh._answer.set_result(answer)
        assert await fresh._wait_for_answer() is answer
        assert calls == ["wake", "query", "wake", "query", "offer"]
    elif reason:
        with pytest.raises(NativeRelayPlaybackError, match="SD status prevents native playback"):
            await playback._open_signaling("sdp", [], "trace", {}, {})
        assert calls == ["wake", "query"]
        assert playback._sd_status_query == f"report_{reason}"
        assert playback._retry_offer is None
    else:
        await playback._open_signaling("sdp", [], "trace", {}, {})
        assert calls == ["wake", "query", "offer"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["report", "send_failed", "unavailable", "report_failed", "timeout", "close_failed"],
)
async def test_mixed_sd_status_falls_back_only_after_lan_send_failure(monkeypatch, outcome):
    from custom_components.tuya_recordings.lib.commands import CameraCommandQueue

    calls = []
    mqtt_report = None

    class Lan:
        def __init__(self, **kwargs):
            pass

        async def async_connect(self):
            calls.append("lan-connect")
            if outcome == "unavailable":
                raise relay_module.NativeLanUnavailableError("fixture unavailable")

        async def async_query_direct_dp(self, dp):
            calls.append("lan-query")
            if outcome in {"send_failed", "close_failed"}:
                raise relay_module.NativeLanSignalError("fixture send failure")
            if outcome == "report_failed":
                mqtt_report.set_result(1)
                raise relay_module.NativeLanDpReportError("fixture report failure")
            if outcome == "timeout":
                raise TimeoutError
            return 1

        async def async_close(self):
            calls.append("lan-close")
            if outcome == "close_failed" and calls.count("lan-close") == 1:
                raise OSError("fixture close failure")

    class Broker(StatusBroker):
        @contextmanager
        def dp_report_listener(self, dp):
            nonlocal mqtt_report
            mqtt_report = asyncio.get_running_loop().create_future()
            try:
                yield mqtt_report
            finally:
                mqtt_report.cancel()

        async def async_send_wake(self):
            calls.append("wake")

        async def async_query_direct_dp(self, dp):
            calls.append("mqtt-query")
            return 1

    monkeypatch.setattr(relay_module, "Lan302Client", Lan)
    monkeypatch.setattr(relay_module, "CAMERA_COMMANDS", CameraCommandQueue())
    config = replace(
        camera_config(), wake_direct=True, sd_low_power_wake=True,
        sd_status_lan_dp="110", sd_status_lan_error_route="mqtt",
    )
    playback = NativeRelayPlaybackSession(config, APP_SESSION)
    playback._on_demand_catalog = True
    if outcome == "timeout":
        with pytest.raises(NativeRelayPlaybackError, match="did not report SD status"):
            await playback._maybe_send_wake(Broker())
        assert calls == ["wake", "lan-connect", "lan-query", "lan-close"]
        assert playback._sd_status_query == "report_timeout"
        return
    if outcome == "close_failed":
        with pytest.raises(OSError, match="fixture close failure"):
            await playback._maybe_send_wake(Broker())
        assert playback._sd_status_query == "lan_cleanup_incomplete"
        assert playback._status_lan is not None
        assert calls == ["wake", "lan-connect", "lan-query", "lan-close"]
        await playback.close()
        assert calls[-2:] == ["lan-close", "lan-close"]
        assert playback._status_lan is None
        return
    await playback._maybe_send_wake(Broker())
    if outcome == "unavailable":
        assert calls == ["wake", "lan-connect", "lan-close", "mqtt-query"]
        assert playback._sd_status_query == "report_received"
        assert playback._sd_status_route_basis == "lan_connect_error"
    else:
        assert calls == ["wake", "lan-connect", "lan-query", "lan-close"] + (
            ["mqtt-query"] if outcome == "send_failed" else []
        )
        assert playback._sd_status_query == {
            "timeout": "report_timeout",
        }.get(outcome, "report_received")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["mqtt_report", "lan_report", "send_failure", "silence", "cancel"])
async def test_lan_sd_query_uses_device_wide_mqtt_listener_and_joins_cleanup(monkeypatch, outcome):
    from types import SimpleNamespace
    from custom_components.tuya_recordings.lib.commands import CameraCommandQueue
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling.envelope import (
        decode_payload, encode_payload,
    )

    monkeypatch.setattr(relay_module, "CAMERA_COMMANDS", CameraCommandQueue())
    config = replace(
        camera_config(), wake_direct=True, sd_low_power_wake=True,
        sd_status_lan_dp="247", sd_status_mqtt_dp="247", sd_status_lan_error_route="mqtt",
    )
    playback = NativeRelayPlaybackSession(config, APP_SESSION)
    key = config.local_key.encode()
    calls = []
    started = asyncio.Event()
    stopped = asyncio.Event()
    mqtt = relay_module.MotoClient(
        identity=playback._mqtt_identity, uid=config.app_uid, device_id=config.dev_id,
        session_id="fixture-session", local_key=config.local_key,
        protocol_version="2.3", on_answer=lambda _: None,
        on_candidate=lambda _: None, on_disconnect=lambda _: None,
    )

    def receive(protocol, body):
        mqtt._consume(encode_payload(key, 1, b"test", body, 1, protocol, "2.2"))

    async def publish(topic, payload, **kwargs):
        protocol, body = decode_payload(key, payload)
        assert protocol == 5 and body["dps"] == {"247": None}
        calls.append("mqtt-query")
        receive(4, {"devId": config.dev_id, "dps": {"247": "1"}})

    mqtt._client = SimpleNamespace(publish=publish)

    class Lan:
        def __init__(self, **kwargs):
            pass

        async def async_connect(self):
            calls.append("lan-connect")

        async def async_query_direct_dp(self, dp):
            assert dp == "247" and mqtt._dp_query is not None
            calls.append("lan-query")
            started.set()
            try:
                # The protocol-1 traffic in Barry's log is not an SD report.
                receive(1, {"devId": config.dev_id, "online": True})
                receive(4, {"devId": "different-camera", "dps": {"247": "1"}})
                assert not mqtt._dp_query[1].done()
                if outcome == "mqtt_report":
                    receive(4, {"devId": config.dev_id, "dps": {"247": "1"}})
                elif outcome == "lan_report":
                    return 1
                elif outcome == "send_failure":
                    raise relay_module.NativeLanSignalError("delivery failed")
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async def async_close(self):
            assert stopped.is_set()
            calls.append("lan-close")

    monkeypatch.setattr(relay_module, "Lan302Client", Lan)
    task = asyncio.create_task(playback._try_lan_sd_status(
        "247", asyncio.get_running_loop().time() + 0.05, mqtt,
    ))
    if outcome == "cancel":
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome == "silence":
        with pytest.raises(NativeRelayPlaybackError, match="did not report SD status"):
            await task
        assert playback._sd_status_query == "report_timeout"
    elif outcome == "send_failure":
        assert await task is False
        # Only a failed LAN delivery permits the APK sendCommand fallback.
        assert await mqtt.async_query_direct_dp("247") == 1
        assert calls == ["lan-connect", "lan-query", "lan-close", "mqtt-query"]
    else:
        assert await task is True
        assert playback._sd_status_query == "report_received"
        assert playback._sd_status_report_route == ("mqtt" if outcome == "mqtt_report" else "lan")
        assert playback._sd_status_dispatch == (
            "report_received_delivery_unconfirmed" if outcome == "mqtt_report" else "sent"
        )
    assert mqtt._dp_query is None
    assert stopped.is_set() and playback._status_lan is None
    assert calls.count("lan-query") == 1
    assert calls.count("mqtt-query") == (1 if outcome == "send_failure" else 0)
    assert playback._retry_offer is None


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
async def test_http_sd_status_after_wake_waits_for_device_report(monkeypatch, accepted):
    from custom_components.tuya_recordings.lib.commands import CameraCommandQueue

    calls = []

    class Broker(StatusBroker):
        async def async_send_wake(self):
            calls.append("wake")

        async def async_wait_for_dp_report(self, dp, send):
            calls.append(("waiter_registered", dp))
            await send()
            calls.append("report")
            return 1

    def gateway(*args):
        calls.append(args)
        return accepted

    monkeypatch.setattr(relay_module, "CAMERA_COMMANDS", CameraCommandQueue())
    config = replace(
        camera_config(), wake_direct=True, sd_low_power_wake=True, sd_status_http_dp="110",
    )
    playback = NativeRelayPlaybackSession(config, APP_SESSION, call_api=gateway)
    playback._on_demand_catalog = True
    if accepted:
        await playback._maybe_send_wake(Broker())
    else:
        with pytest.raises(NativeRelayPlaybackError, match="SD status query failed"):
            await playback._maybe_send_wake(Broker())
    assert calls[:3] == ["wake", ("waiter_registered", "110"), (
        "thing.m.device.dp.publish", "1.0",
        {"devId": config.dev_id, "gwId": config.dev_id, "dps": '{"110":null}'}, {},
    )]
    assert calls[3:] == (["report"] if accepted else [])
    assert playback._sd_status_query == ("report_received" if accepted else "send_failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("online", [None, False, True])
async def test_sd_query_preserves_inventory_route_without_new_online_report(monkeypatch, online):
    from custom_components.tuya_recordings.lib.commands import CameraCommandQueue

    calls = []

    class Broker(StatusBroker):
        device_online = None

        async def async_send_wake(self):
            calls.append("wake")

        async def async_query_direct_dp(self, dp):
            calls.append("mqtt")
            return 1

        async def async_wait_for_dp_report(self, dp, send):
            await send()
            return 1

    def gateway(*args):
        calls.append("http")
        return True

    monkeypatch.setattr(relay_module, "CAMERA_COMMANDS", CameraCommandQueue())
    config = replace(
        camera_config(), wake_direct=True, sd_low_power_wake=True,
        sd_status_mqtt_dp="110" if online is True else "",
        sd_status_http_dp="" if online is True else "110", sd_status_mqtt_capable=True,
    )
    playback = NativeRelayPlaybackSession(config, APP_SESSION, call_api=gateway)
    playback._on_demand_catalog = True
    await playback._maybe_send_wake(Broker())
    assert calls == ["wake", "mqtt" if online is True else "http"]
    assert playback._sd_status_query == "report_received"


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["unknown", "none", "wireless", "low_power"])
async def test_sd_catalog_suppresses_hub_dp_wake(capability):
    def gateway(*_args):
        pytest.fail("SD catalog must not write a hub wake DP")

    config = replace(camera_config(), wake_capability=capability, wake_direct=False)
    playback = NativeRelayPlaybackSession(config, APP_SESSION, call_api=gateway)
    playback._on_demand_catalog = True
    await playback._maybe_send_wake(None)
    assert playback._wake_status == "sd_low_power_unconfirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("outcome", ["answer", "disconnect", "timeout", "cancel", "publish_error"])
async def test_offer_retries_without_wake_reuse_payload_and_stop(monkeypatch, manual, outcome):
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.exceptions import (
        TuyaIpcP2pError,
    )

    loop = asyncio.get_running_loop()
    playback = session()
    playback._answer = loop.create_future()
    playback._ended = loop.create_future()
    playback._on_demand_catalog = not manual
    playback._retry_mqtt_offer = manual
    offers = []
    answer = object()

    class Broker(StatusBroker):
        async def async_send_offer(self, *args):
            offers.append(args)
            if len(offers) != 2:
                return
            if outcome == "answer":
                playback._answer.set_result(answer)
            elif outcome == "disconnect":
                playback._on_signaling_failure("ConnectionError")
            elif outcome == "publish_error":
                raise TuyaIpcP2pError("test publish error")
            elif outcome == "cancel":
                asyncio.current_task().cancel()
                await asyncio.sleep(0)

    assert relay_module._OFFER_RETRY_OFFSETS == (1.5, 3.0, 6.0, 12.0)
    monkeypatch.setattr(relay_module, "_OFFER_RETRY_OFFSETS", (0.02, 0.04, 0.08, 0.16))
    monkeypatch.setattr(relay_module, "_ANSWER_TIMEOUT_SECONDS", 0.22)
    playback._moto = Broker()
    await playback._open_signaling("sdp", [], "trace", {}, {})
    if outcome == "answer":
        assert await playback._wait_for_answer() is answer
    elif outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await asyncio.create_task(playback._wait_for_answer())
        assert not playback._answer.done()
    else:
        match = {
            "disconnect": "MQTT signaling stopped",
            "timeout": "Timed out waiting",
            "publish_error": "MQTT offer retry failed",
        }[outcome]
        with pytest.raises(NativeRelayPlaybackError, match=match):
            await playback._wait_for_answer()
    expected_retries = 4 if outcome == "timeout" else 1
    assert offers == [offers[0]] * (expected_retries + 1)
    assert f"offer_retries={expected_retries}" in playback.transport_diagnostics
    assert playback._retry_offer is None


@pytest.mark.asyncio
async def test_http_bootstrap_does_not_send_after_report_channel_failure(monkeypatch):
    def gateway(*_args):
        pytest.fail("Bootstrap must not send after the reply channel stops")

    playback = NativeRelayPlaybackSession(camera_config(), APP_SESSION, call_api=gateway)
    playback._ended = asyncio.get_running_loop().create_future()

    async def connect(_session_id):
        playback._on_signaling_failure("stream_ended")

    monkeypatch.setattr(playback, "_connect_mqtt", connect)
    with pytest.raises(NativeRelayPlaybackError, match="MQTT signaling stopped"):
        await playback._initialize_http_offer("trace")


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
    assert playback._stream_config.relay_token is None
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
@pytest.mark.parametrize("wake", ["unknown", "low_power"])
async def test_subscription_failure_has_sanitized_handshake_context(monkeypatch, wake):
    class RejectedSubscription:
        diagnostics = "subscription=rejected, published=0, received=0"

        def __init__(self, **kwargs):
            pass

        async def async_connect(self):
            raise NativeRelayPlaybackError("Broker rejected the subscription")

        async def async_send_disconnect(self):
            pass

        async def async_close(self):
            pass

    monkeypatch.setattr(relay_module, "MotoClient", RejectedSubscription)
    monkeypatch.setattr(relay_module, "_DISCONNECT_GRACE_SECONDS", 0)
    config = replace(camera_config(), app_profile_id="tuya_smart", wake_capability=wake)
    playback = NativeRelayPlaybackSession(config, APP_SESSION)
    with pytest.raises(NativeRelayPlaybackError, match="rejected") as failure:
        await playback.connect(media=False)
    note, = failure.value.__notes__
    assert "subscription=rejected, published=0, received=0" in note
    assert "phase=mqtt_subscribe, mode=metadata" in note
    assert "app_profile=tuya_smart, bootstrap_api_version=1.0" in note
    assert f"token_route=app_token, wake_capability={wake}, wake_direct=False, wake_status=not_requested" in note
    for private in (config.local_key, config.app_uid, config.trace_id, playback.local_cid):
        assert private not in note


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
        "mqtt-offer",
    ]
    assert calls[1][1]["moto_id"] == "camera-moto-id"
    assert playback._signaling_path == "mqtt"
    assert "lan=unavailable, offer=published, offer_delivery=unconfirmed, answer=not_received" in playback.transport_diagnostics


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


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["silence", "offer_answer", "candidate_answer", "cancel", "failure"])
async def test_cached_candidate_retries_are_bounded_and_stop_immediately(monkeypatch, outcome):
    playback = session()
    loop = asyncio.get_running_loop()
    playback._answer = loop.create_future()
    playback._ended = loop.create_future()
    playback._command_candidates = ["candidate-one", "candidate-two"]
    calls = []
    answer = object()

    async def offer():
        calls.append("offer")
        if outcome == "offer_answer":
            playback._answer.set_result(answer)

    class Broker:
        async def async_send_candidate(self, value):
            calls.append(value)
            if outcome == "candidate_answer":
                playback._answer.set_result(answer)
            elif outcome == "cancel":
                raise asyncio.CancelledError
            elif outcome == "failure":
                raise relay_module.TuyaIpcP2pError("candidate publish failed")

    playback._moto = Broker()
    playback._retry_offer = (loop.time(), offer)
    monkeypatch.setattr(relay_module, "_OFFER_RETRY_OFFSETS", (0.01, 0.02, 0.04, 0.06))
    monkeypatch.setattr(relay_module, "_ANSWER_TIMEOUT_SECONDS", 0.08)
    try:
        if outcome == "silence":
            with pytest.raises(NativeRelayPlaybackError, match="Timed out"):
                await playback._wait_for_answer()
            assert calls == ["offer", "candidate-one", "candidate-two"] * 4
        elif outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await playback._wait_for_answer()
            assert calls == ["offer", "candidate-one"]
        elif outcome == "failure":
            with pytest.raises(NativeRelayPlaybackError, match="retry failed"):
                await playback._wait_for_answer()
            assert calls == ["offer", "candidate-one"]
        else:
            assert await playback._wait_for_answer() is answer
            assert calls == (["offer"] if outcome == "offer_answer" else ["offer", "candidate-one"])
        count = len(calls)
        await asyncio.sleep(0.02)
        assert len(calls) == count
    finally:
        playback._answer.cancel()
        playback._ended.cancel()


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


@pytest.mark.parametrize("bootstrap", ["app-token", "expired", "missing", "early-answer", "cloud-number"])
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

        def set_remote_credentials(self, ufrag, password):
            captured["remote_credentials"] = (ufrag, password)

        def add_remote_candidate(self, candidate):
            captured.setdefault("remote_candidates", []).append(candidate)

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

        def __init__(self, transmit, *, on_failure=None):
            self.on_failure = on_failure
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
            token = json.loads(config.token) if bootstrap in {"expired", "cloud-number"} else {}
            token["expire"] = 0
            if bootstrap == "cloud-number":
                token.update(expire=4102444800.0, skill={"cloudP2P": 1.0})
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
        "offer",
        "direct-open",
        "ice-gather",
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

        def set_remote_credentials(self, ufrag, password):
            pass

        def add_remote_candidate(self, candidate):
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


def test_sd_media_diagnostics_distinguish_stale_preview_from_invalid_rtp():
    playback = session()
    playback._playback_started = True
    playback._task_id = 2
    playback._stream_id = 0x00020008

    def deliver(channel, stream_id, payload):
        header = struct.pack("<I5I", stream_id, 0, 0, 0, 0, 0)
        playback._on_rtp_record(channel, encrypt_record(ANSWER_KEY,
            header + struct.pack("<I", len(payload)) + payload), ANSWER_KEY)

    deliver(1, 0x00010004, b"preview")
    deliver(2, 0x00010005, b"preview audio")
    deliver(1, 0x00020008, b"invalid RTP")
    deliver(1, 0x00020008, struct.pack(">BBHII", 0x80, 0x80 | 98, 1, 9000, 1) + b"other codec")
    assert playback._media_frames.empty()
    assert playback._media_drops == {
        "stale_task_ch1": 1, "stale_task_ch2": 1,
        "rtp_header": 1, "unsupported_ch1_pt98": 1,
    }
    note = playback.transport_diagnostics
    assert "started=True, task=2, stream=131080" in note
    assert "video_frames=0, audio_frames=0" in note
    assert "stale_task_ch1" in note
    playback._reset_media_state()
    assert playback._media_drops == {}


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
                media_record(playback._stream_id, 1, 1000, 96, b"\x65video")
            )
            playback._audio.handler(
                media_record(playback._stream_id, 1, 8000, 99, b"\x00\x00\xff\x7f")
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
    assert waited == [(8, (7, 3))]
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
        (8, (7, 1)),
        (0x00020009, (7, 2)),
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
