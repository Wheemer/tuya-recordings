"""Offline reproductions for the transaction audit; no camera/network calls."""
import asyncio
from types import SimpleNamespace

import pytest
import tinytuya

from test_native_relay_playback import APP_SESSION, StatusBroker, camera_config
from dataclasses import replace
from custom_components.tuya_recordings.lib import native_relay_playback as playback_module
from custom_components.tuya_recordings.lib.commands import CameraCommandQueue
from custom_components.tuya_recordings.lib.native_gateway import _sd_status_http_dp
from custom_components.tuya_recordings.lib.native_lan_signaling import Lan302Client, NativeLanSignalError
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling import moto_client
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling.envelope import encode_payload


def test_unrelated_empty_frame_is_not_a_control_acknowledgement():
    device = tinytuya.Device("camera", "192.0.2.9", "0123456789abcdef", version=3.3)
    device.set_socketPersistent(True)
    device.set_retry(False)
    device.set_socketRetryLimit(0)
    device.sendWait = None
    device.socket = SimpleNamespace(sendall=lambda _: None, settimeout=lambda _: None, close=lambda: None)
    device._get_socket = lambda _: True
    device._encode_message = lambda payload: tinytuya.pack_message(
        tinytuya.TuyaMessage(7, payload.cmd, 0, payload.payload, 0, True),
    )
    device._receive = lambda: tinytuya.TuyaMessage(999, 9, 0, b"", 0, True)
    lan = Lan302Client(
        uid="uid", device_id="camera", session_id="session", local_key="0123456789abcdef",
        on_answer=lambda _: None, on_candidate=lambda _: None, on_disconnect=lambda _: None,
        preconnect=False, security_level=3,
    )
    with pytest.raises(NativeLanSignalError):
        lan._send_query(device, {"dps": {"247": None}})


def test_get_dp_http_fallback_does_not_require_an_http_mode():
    device = {
        "cloudOnline": False, "gatewayVerCAD": "1.0.2",
        "communication": {"dataModel": 1, "communicationModes": [{"type": 0}, {"type": 1}]},
        "productBean": {"schemaInfo": {"schema": '[{"id":"247","code":"sd_status"}]'}},
    }
    assert _sd_status_http_dp(device) == "247"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["missing_schema", "lan_without_fallback"])
async def test_required_sd_preparation_cannot_succeed_without_a_query_route(monkeypatch, route):
    commands = CameraCommandQueue()
    monkeypatch.setattr(playback_module, "CAMERA_COMMANDS", commands)
    config = replace(
        camera_config(), wake_direct=True, sd_low_power_wake=True,
        sd_status_lan_dp="247" if route == "lan_without_fallback" else "",
        sd_status_mqtt_dp="", sd_status_http_dp="", sd_status_lan_error_route="",
    )
    playback = playback_module.NativeRelayPlaybackSession(config, APP_SESSION)
    playback._on_demand_catalog = True
    calls = []

    class Broker(StatusBroker):
        async def async_send_wake(self):
            calls.append("wake")

    async def unavailable(*args):
        calls.append("lan")
        playback._sd_status_query = "lan_unavailable"
        return False

    monkeypatch.setattr(playback, "_try_lan_sd_status", unavailable)
    broker = Broker()
    with pytest.raises(playback_module.NativeRelayPlaybackError, match="SD status query route"):
        await playback._maybe_send_wake(broker)
    assert not playback._sd_status_prepared
    assert broker._dp_query is None
    assert playback._sd_status_deadline is None
    assert calls == (["wake", "lan"] if route == "lan_without_fallback" else [])
    assert not commands.wake_report_on_cooldown(config.dev_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("arrival", ["wake", "lan_connect", "lan_close"])
async def test_one_dp_listener_spans_the_entire_transaction(monkeypatch, arrival):
    monkeypatch.setattr(playback_module, "CAMERA_COMMANDS", CameraCommandQueue())
    monkeypatch.setattr(moto_client, "_DP_REPORT_TIMEOUT_SECONDS", 0.03)
    config = replace(
        camera_config(), wake_direct=True, sd_low_power_wake=True,
        sd_status_mqtt_dp="247", sd_status_lan_dp="" if arrival == "wake" else "247",
        sd_status_lan_error_route="mqtt",
    )
    playback = playback_module.NativeRelayPlaybackSession(config, APP_SESSION)
    playback._on_demand_catalog = True
    key = config.local_key.encode()
    mqtt = moto_client.MotoClient(
        identity=playback._mqtt_identity, uid=config.app_uid, device_id=config.dev_id,
        session_id="session", local_key=config.local_key, protocol_version="2.3",
        on_answer=lambda _: None, on_candidate=lambda _: None, on_disconnect=lambda _: None,
    )

    def report():
        mqtt._consume(encode_payload(
            key, 1, b"test", {"devId": config.dev_id, "dps": {"247": "1"}}, 1, 4, "2.3",
        ))

    async def publish(topic, payload, **kwargs):
        if topic.startswith("m/w/") and arrival == "wake":
            report()

    mqtt._client = SimpleNamespace(publish=publish)
    mqtt._subscription = "granted_qos_1"

    class Lan:
        def __init__(self, **kwargs):
            pass

        async def async_connect(self):
            if arrival == "lan_connect":
                report()

        async def async_query_direct_dp(self, dp):
            if arrival == "lan_close":
                raise NativeLanSignalError("fixture failed delivery")
            await asyncio.Event().wait()

        async def async_close(self):
            if arrival == "lan_close":
                report()

    monkeypatch.setattr(playback_module, "Lan302Client", Lan)
    async with asyncio.timeout(0.1):
        await playback._maybe_send_wake(mqtt)
    assert playback._sd_status_query == "report_received"


@pytest.mark.asyncio
async def test_interactive_entry_checks_sd_status_before_offering(monkeypatch):
    monkeypatch.setattr(playback_module, "CAMERA_COMMANDS", CameraCommandQueue())
    config = replace(
        camera_config(), wake_direct=True, sd_low_power_wake=True,
        wake_capability="low_power", sd_status_mqtt_dp="247",
    )
    playback = playback_module.NativeRelayPlaybackSession(config, APP_SESSION)
    # These are the flags assigned by connect(media=True, allow_wake=True).
    playback._on_demand_catalog = False
    playback._connector_wake_allowed = True
    calls = []

    class Broker(StatusBroker):
        def __init__(self, **kwargs):
            pass

        async def async_connect(self):
            calls.append("subscribe")

        async def async_send_wake(self):
            calls.append("wake")

        async def async_query_direct_dp(self, dp):
            calls.append("status")
            return 5

        async def async_send_offer(self, *args):
            calls.append("offer")

    monkeypatch.setattr(playback_module, "MotoClient", Broker)
    with pytest.raises(playback_module.NativeRelayPlaybackError, match="SD status prevents"):
        await playback._open_signaling("sdp", [], "trace", {}, {})
    assert "offer" not in calls


@pytest.mark.asyncio
async def test_direct_mqtt_report_is_identified_in_diagnostics(monkeypatch):
    monkeypatch.setattr(playback_module, "CAMERA_COMMANDS", CameraCommandQueue())
    config = replace(
        camera_config(), wake_direct=True, sd_low_power_wake=True, sd_status_mqtt_dp="247",
    )
    playback = playback_module.NativeRelayPlaybackSession(config, APP_SESSION)
    playback._on_demand_catalog = True

    class Broker(StatusBroker):
        async def async_send_wake(self):
            pass

        async def async_query_direct_dp(self, dp):
            return 1

    await playback._maybe_send_wake(Broker())
    assert playback._sd_status_query == "report_received"
    assert playback._sd_status_report_route == "mqtt"


@pytest.mark.asyncio
async def test_listener_borrowing_is_same_task_only_and_survives_inner_exit():
    config = camera_config()
    playback = playback_module.NativeRelayPlaybackSession(config, APP_SESSION)
    mqtt = moto_client.MotoClient(
        identity=playback._mqtt_identity, uid=config.app_uid, device_id=config.dev_id,
        session_id="session", local_key=config.local_key,
        on_answer=lambda _: None, on_candidate=lambda _: None, on_disconnect=lambda _: None,
    )
    with mqtt.dp_report_listener("247") as outer:
        with mqtt.dp_report_listener("247") as inner:
            assert inner is outer
        assert mqtt._dp_query[1] is outer and not outer.done()

        async def concurrent():
            with pytest.raises(Exception, match="already pending"):
                with mqtt.dp_report_listener("247"):
                    pytest.fail("Different task must not borrow transaction listener")

        await asyncio.create_task(concurrent())
    assert mqtt._dp_query is None and outer.cancelled()


@pytest.mark.asyncio
async def test_status_deadline_includes_wake_and_cleans_listener(monkeypatch):
    monkeypatch.setattr(playback_module, "CAMERA_COMMANDS", CameraCommandQueue())
    monkeypatch.setattr(playback_module, "_SD_STATUS_TIMEOUT_SECONDS", 0.02)
    config = replace(camera_config(), wake_direct=True, sd_low_power_wake=True, sd_status_mqtt_dp="247")
    playback = playback_module.NativeRelayPlaybackSession(config, APP_SESSION)
    playback._on_demand_catalog = True

    class Broker(StatusBroker):
        async def async_send_wake(self):
            assert self._dp_query is not None
            await asyncio.Event().wait()

    broker = Broker()
    with pytest.raises(playback_module.NativeRelayPlaybackError, match="did not report"):
        await playback._maybe_send_wake(broker)
    assert broker._dp_query is None and playback._sd_status_deadline is None


def test_remote_candidates_survive_until_ice_and_sdp_candidates_are_parsed():
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling.answer import parse_answer
    from test_native_relay_playback import answer_sdp

    playback = playback_module.NativeRelayPlaybackSession(camera_config(), APP_SESSION)
    candidate = "a=candidate:1 1 udp 1234 192.0.2.1 2345 typ host"
    playback._on_remote_candidate(candidate)
    playback._on_remote_candidate(candidate)
    assert playback._remote_candidates == [candidate]
    answer = parse_answer(answer_sdp() + "\r\n" + candidate)
    assert answer.candidates == (candidate,)
    received = []
    playback._ice = SimpleNamespace(add_remote_candidate=received.append)
    playback._on_remote_candidate(candidate)
    assert received == [candidate]
    playback._closed = True
    playback._on_remote_candidate(candidate)
    assert received == [candidate]


@pytest.mark.asyncio
@pytest.mark.parametrize("online", [False, True])
async def test_current_authenticated_online_report_controls_query_route(monkeypatch, online):
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling.envelope import decode_payload

    monkeypatch.setattr(playback_module, "CAMERA_COMMANDS", CameraCommandQueue())
    config = replace(
        camera_config(), wake_direct=True, sd_low_power_wake=True,
        sd_status_mqtt_capable=True,
        sd_status_mqtt_dp="" if online else "247",
        sd_status_http_dp="247" if online else "",
    )
    playback = playback_module.NativeRelayPlaybackSession(config, APP_SESSION)
    playback._on_demand_catalog = True
    mqtt = moto_client.MotoClient(
        identity=playback._mqtt_identity, uid=config.app_uid, device_id=config.dev_id,
        session_id="session", local_key=config.local_key, protocol_version="2.3",
        on_answer=lambda _: None, on_candidate=lambda _: None, on_disconnect=lambda _: None,
    )
    key = config.local_key.encode()
    calls = []

    def receive(protocol, data):
        mqtt._consume(encode_payload(key, 1, b"test", data, 1, protocol, "2.3"))

    async def publish(topic, payload, **kwargs):
        if topic.startswith("m/w/"):
            receive(1, {"devId": "other", "online": not online})
            assert mqtt.device_online is None
            receive(1, {"devId": config.dev_id, "online": online})
            assert not mqtt._dp_query[1].done()
        else:
            protocol, data = decode_payload(key, payload)
            assert protocol == 5 and data["dps"] == {"247": None}
            calls.append("mqtt")
            receive(4, {"devId": config.dev_id, "dps": {"247": 1}})

    async def http(dp):
        assert dp == "247"
        calls.append("http")
        receive(4, {"devId": config.dev_id, "dps": {"247": 1}})

    mqtt._client = SimpleNamespace(publish=publish)
    mqtt._subscription = "granted_qos_1"
    monkeypatch.setattr(playback, "_send_http_sd_status", http)
    await playback._maybe_send_wake(mqtt)
    assert calls == (["mqtt"] if online else ["http"])
    assert playback._sd_status_query == "report_received"
