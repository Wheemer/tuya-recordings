"""APK protocol-1 transitions remain separate from SD and IMM replies."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from test_native_relay_playback import APP_SESSION, StatusBroker, camera_config
from custom_components.tuya_recordings.lib import native_relay_playback as playback_module
from custom_components.tuya_recordings.lib.commands import CameraCommandQueue
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling import moto_client
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling.envelope import (
    decode_payload,
    encode_payload,
)


def make_client():
    config = camera_config()
    playback = playback_module.NativeRelayPlaybackSession(config, APP_SESSION)
    return moto_client.MotoClient(
        identity=playback._mqtt_identity, uid=config.app_uid,
        device_id=config.dev_id, session_id="session", local_key=config.local_key,
        on_answer=lambda _: None, on_candidate=lambda _: None, on_disconnect=lambda _: None,
        protocol_version="2.3",
    )


def receive(client, data, protocol=1):
    client._consume(encode_payload(
        client._key, 1, b"test", data, 1, protocol, "2.3",
    ))


def connection(client, **data):
    receive(client, {"devId": "camera-device", **data})


@pytest.mark.asyncio
async def test_low_power_online_does_not_complete_sd_query_or_confirm_wake():
    client = make_client()
    with client.dp_report_listener("110") as report:
        connection(client, online=True, lowPowerConnect=True)
        assert client.device_online is True
        assert client.low_power_connect is True
        assert not client.low_power_wake_confirmed
        assert not report.done()
        assert client._inbound["low_power_connected"] == 1
        assert client._inbound["other_protocol"] == 0
        connection(client, online=True, lowPowerConnect=False, lastConnectChangeTime=100)
        assert client.low_power_wake_confirmed
        assert not report.done()
        receive(client, {"devId": "camera-device", "dps": {"110": "1"}}, 4)
        assert report.result() == 1


@pytest.mark.parametrize("stamp", [None, False, "invalid", -1, 0, 1 << 63])
def test_missing_or_invalid_timestamp_cannot_confirm_wake(stamp):
    client = make_client()
    connection(client, online=True, lowPowerConnect=False, lastOnlineChangeTime=stamp)
    assert not client.low_power_wake_confirmed


def test_false_then_online_requires_nonstale_timestamp():
    client = make_client()
    connection(client, online=False, lowPowerConnect=False, lastConnectChangeTime=100)
    assert not client.low_power_wake_confirmed
    connection(client, online=True, lastOnlineChangeTime=99)
    assert not client.low_power_wake_confirmed
    connection(client, online=True, lastOnlineChangeTime=100)
    assert client.low_power_wake_confirmed
    connection(client, online=False)
    assert not client.low_power_wake_confirmed
    connection(client, online=True, lowPowerConnect=True, lastConnectChangeTime=101)
    connection(client, online=True, lastOnlineChangeTime=102)
    assert not client.low_power_wake_confirmed


def test_android_long_timestamps_and_explicit_online_timestamp_precedence():
    client = make_client()
    connection(client, online=True, lowPowerConnect=False, lastConnectChangeTime="1794050000000")
    assert client.low_power_wake_confirmed
    connection(client, online=True, lowPowerConnect=False,
               lastOnlineChangeTime=None, lastConnectChangeTime=1794050000000)
    assert not client.low_power_wake_confirmed


@pytest.mark.parametrize("data", [
    {"online": True, "lowPowerConnect": False, "lastConnectChangeTime": 100},
    {"devId": "other", "online": True, "lowPowerConnect": False, "lastConnectChangeTime": 100},
    {"devId": "camera-device", "cid": "child", "online": True, "lowPowerConnect": False},
    {"devId": "camera-device", "online": "true", "lowPowerConnect": False},
])
def test_unmatched_or_malformed_connection_report_does_not_confirm(data):
    client = make_client()
    receive(client, data)
    assert client.device_online is None
    assert not client.low_power_wake_confirmed


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["ready", "decimal_ready", "no_card", "decimal_no_card", "low_power_only", "awake_only"])
async def test_wake_sd_status_offer_sequence(monkeypatch, outcome):
    config = replace(
        camera_config(), wake_direct=True, wake_capability="low_power",
        sd_low_power_wake=True, sd_status_mqtt_dp="110", sd_status_mqtt_capable=True,
    )
    playback = playback_module.NativeRelayPlaybackSession(config, APP_SESSION)
    playback._on_demand_catalog = True
    playback._connector_wake_allowed = True
    events = []
    brokers = []
    monkeypatch.setattr(playback_module, "CAMERA_COMMANDS", CameraCommandQueue())
    monkeypatch.setattr(playback_module, "_SD_STATUS_TIMEOUT_SECONDS", 0.05)

    class Broker:
        def __init__(self, **kwargs):
            self.inbound = asyncio.Queue()
            brokers.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            events.append("closed")

        async def subscribe(self, topic, **kwargs):
            return (1,)

        def put(self, data, protocol=1):
            self.inbound.put_nowait(SimpleNamespace(
                topic="smart/mb/in/camera-device",
                payload=encode_payload(config.local_key.encode(), 1, b"test", data, 1, protocol, "2.3"),
            ))

        async def publish(self, topic, payload, **kwargs):
            if topic.startswith("m/w/"):
                events.append("wake")
                self.put({"devId": config.dev_id, "online": True, "lowPowerConnect": True})
                await asyncio.sleep(0)
                return
            protocol, data = decode_payload(config.local_key.encode(), payload)
            if protocol == 5:
                events.append("sd-query")
                assert data["dps"] == {"110": None}
                if outcome != "low_power_only":
                    self.put({"devId": config.dev_id, "online": True,
                              "lowPowerConnect": False, "lastConnectChangeTime": 100})
                if outcome in {"ready", "decimal_ready", "no_card", "decimal_no_card"}:
                    status = 5.0 if outcome == "decimal_no_card" else 5 if outcome == "no_card" else "1.0" if outcome == "decimal_ready" else 1
                    self.put({"devId": config.dev_id, "dps": {"110": status}}, 4)
                return
            assert protocol == 302
            events.append(data["header"]["type"])
            assert playback._sd_status_query == "report_received"
            assert playback._moto.low_power_wake_confirmed

        @property
        async def messages(self):
            while True:
                yield await self.inbound.get()

    monkeypatch.setattr(moto_client.aiomqtt, "Client", Broker)
    try:
        if outcome in {"ready", "decimal_ready"}:
            await playback._open_signaling("sdp", [], "trace", {}, {})
            assert events == ["wake", "sd-query", "offer"]
            assert "wake_status=confirmed" in playback.transport_diagnostics
        else:
            with pytest.raises(playback_module.NativeRelayPlaybackError):
                await playback._open_signaling("sdp", [], "trace", {}, {})
            assert events == ["wake", "sd-query"]
            assert playback._sd_status_query == ("report_no_card" if outcome in {"no_card", "decimal_no_card"} else "report_timeout")
        assert len(brokers) == 1
        assert playback._moto._dp_query is None
        assert playback._moto._dp_query_owner is None
        assert "other_protocol" not in playback._moto.diagnostics
    finally:
        await playback._moto.async_close()
    assert events[-1] == "closed"
    assert playback._moto._reader is None


@pytest.mark.asyncio
async def test_sd_report_diagnostics_distinguish_missing_key_invalid_shape_and_value():
    client = make_client()
    with client.dp_report_listener("110") as report:
        receive(client, {"devId": "camera-device", "dps": {"111": 1}}, 4)
        receive(client, {"devId": "camera-device", "dps": {"110": True}}, 4)
        receive(client, {"devId": "camera-device", "dps": []}, 4)
        assert not report.done()
        assert client._inbound["dp_report_requested_key_missing"] == 1
        assert client._inbound["dp_report_invalid_value_bool"] == 1
        assert client._inbound["dp_report_invalid_dps_shape"] == 1
        assert client._inbound["dp_report_missing_or_non_integer"] == 3
        assert "dp_query=110, dp_report_keys=()" in client.diagnostics
        receive(client, {"devId": "camera-device", "dps": {"110": "1.0"}}, 4)
        assert report.result() == 1
        assert "dp_report_keys=('110',)" in client.diagnostics


@pytest.mark.asyncio
async def test_sd_report_diagnostic_keys_are_numeric_bounded_and_not_values():
    client = make_client()
    with client.dp_report_listener("110"):
        values = {str(i): "private-value" for i in range(100, 140)}
        values["password"] = "secret"
        values["110"] = True
        receive(client, {"devId": "camera-device", "dps": values}, 4)
        assert len(client._last_dp_report_keys) == 16
        assert all(key.isdecimal() for key in client._last_dp_report_keys)
        assert "password" not in client.diagnostics
        assert "secret" not in client.diagnostics
        assert "private-value" not in client.diagnostics


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["lan", "mqtt_fallback", "no_card", "status_timeout", "sd_send_error", "missing_route"])
async def test_native_lan_cannot_bypass_or_duplicate_sd_preparation(monkeypatch, outcome):
    config = replace(
        camera_config(), p2p_policy=1, wake_direct=True, wake_capability="low_power",
        sd_low_power_wake=True, sd_status_mqtt_dp="" if outcome == "missing_route" else "110",
    )
    playback = playback_module.NativeRelayPlaybackSession(config, APP_SESSION)
    playback._on_demand_catalog = True
    playback._connector_wake_allowed = True
    events = []
    monkeypatch.setattr(playback_module, "CAMERA_COMMANDS", CameraCommandQueue())
    monkeypatch.setattr(playback_module, "_SD_STATUS_TIMEOUT_SECONDS", 0.03)

    class Broker(StatusBroker):
        diagnostics = "fixture-status-broker"

        def __init__(self, **kwargs):
            pass

        async def async_connect(self):
            events.append("subscribe")

        async def async_send_wake(self):
            events.append("wake")

        async def async_query_direct_dp(self, dp):
            assert dp == "110"
            events.append("status")
            if outcome == "sd_send_error":
                raise playback_module.TuyaIpcP2pError("fixture query publication failed")
            if outcome == "status_timeout":
                await asyncio.Event().wait()
            return 5 if outcome == "no_card" else 1

        async def async_close(self):
            events.append("mqtt-closed")

        async def async_send_offer(self, *args):
            events.append("mqtt-offer")

    class Lan:
        def __init__(self, **kwargs):
            pass

        async def async_connect(self):
            assert playback._sd_status_prepared
            assert events == ["subscribe", "wake", "status"]
            events.append("lan-connect")
            if outcome == "mqtt_fallback":
                raise playback_module.NativeLanUnavailableError("fixture unavailable")

        async def async_send_offer(self, *args):
            events.append("lan-offer")

        async def async_close(self):
            events.append("lan-closed")

    monkeypatch.setattr(playback_module, "MotoClient", Broker)
    monkeypatch.setattr(playback_module, "Lan302Client", Lan)
    try:
        if outcome in {"no_card", "status_timeout", "sd_send_error", "missing_route"}:
            with pytest.raises(playback_module.NativeRelayPlaybackError):
                await playback._open_signaling("sdp", [], "trace", {}, {})
            assert events == (["subscribe", "wake"] if outcome == "missing_route" else ["subscribe", "wake", "status"])
        else:
            await playback._open_signaling("sdp", [], "trace", {}, {})
            if outcome == "lan":
                assert events[-1] == "lan-offer"
            else:
                assert events[-2:] == ["lan-closed", "mqtt-offer"]
            assert events.count("wake") == 1
            assert events.count("status") == 1
    finally:
        await playback.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["wake_cooldown", "wake_failure", "sd_send_failure", "missing_route"])
async def test_wake_throttle_or_failure_does_not_bypass_sd_query(monkeypatch, outcome):
    config = replace(
        camera_config(), wake_direct=True, wake_capability="low_power",
        sd_low_power_wake=True, sd_status_mqtt_dp="" if outcome == "missing_route" else "110",
    )
    playback = playback_module.NativeRelayPlaybackSession(config, APP_SESSION)
    playback._on_demand_catalog = True
    playback._connector_wake_allowed = True
    events = []
    commands = CameraCommandQueue()
    if outcome == "wake_cooldown":
        assert commands.reserve_wake(config.dev_id)
        commands._wake_after[config.dev_id] = playback_module.time.monotonic() + 0.01
    monkeypatch.setattr(playback_module, "CAMERA_COMMANDS", commands)

    class Broker(StatusBroker):
        def __init__(self, **kwargs):
            pass

        async def async_connect(self):
            pass

        async def async_send_wake(self):
            events.append("wake")
            if outcome == "wake_failure":
                raise playback_module.TuyaIpcP2pError("fixture wake publication failed")

        async def async_query_direct_dp(self, dp):
            events.append("query")
            if outcome == "sd_send_failure":
                raise playback_module.TuyaIpcP2pError("fixture query publication failed")
            return 1

        async def async_send_offer(self, *args):
            assert playback._sd_status_prepared
            events.append("offer")

    monkeypatch.setattr(playback_module, "MotoClient", Broker)
    if outcome in {"sd_send_failure", "missing_route"}:
        with pytest.raises(playback_module.NativeRelayPlaybackError):
            await playback._open_signaling("sdp", [], "trace", {}, {})
        assert events == (["wake", "query"] if outcome == "sd_send_failure" else ["wake"])
    else:
        await playback._open_signaling("sdp", [], "trace", {}, {})
        assert events == ["wake", "query", "offer"]
