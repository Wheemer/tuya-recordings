"""SD query serialization checked against both APK local-control branches."""

import json
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
import tinytuya
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from custom_components.tuya_recordings.lib.native_lan_signaling import Lan302Client, NativeLanSignalError
from custom_components.tuya_recordings.lib import native_relay_playback as playback_module
from custom_components.tuya_recordings.lib.commands import CameraCommandQueue
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling import moto_client
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling.envelope import decode_payload, encode_payload
from test_native_relay_playback import APP_SESSION, camera_config


def decrypt_lan_request(device, payload, *, protocol_header=True):
    """Inspect TinyTuya's actual bytes with an independent crypto decoder."""
    wire = device._encode_message(payload)
    if device.version >= 3.5:
        plain = AESGCM(device.local_key).decrypt(wire[18:30], wire[30:-4], wire[4:18])
    else:
        ciphertext = wire[16:-36] if device.version >= 3.4 else wire[31 if protocol_header else 16:-8]
        decryptor = Cipher(algorithms.AES(device.local_key), modes.ECB()).decryptor()
        plain = decryptor.update(ciphertext) + decryptor.finalize()
        plain = plain[:-plain[-1]]
    if device.version >= 3.4 and protocol_header:
        assert plain[:3] == str(device.version).encode()
        plain = plain[15:]
    return json.loads(plain)


@pytest.mark.parametrize("version", [3.3, 3.4, 3.5])
@pytest.mark.parametrize("send_failure", [False, True])
def test_lan_302_uses_raw_normal_control_body_and_restores_device_header(version, send_failure):
    device = tinytuya.Device("camera", "192.0.2.9", "0123456789abcdef", version=version)
    original_header = device.version_header
    message = {"header": {"type": "offer", "sessionid": "session"}, "msg": {"sdp": "fixture"}}
    sent = []

    def send(payload):
        assert payload.cmd == 32
        sent.append(decrypt_lan_request(device, payload, protocol_header=False))
        if send_failure:
            raise OSError("fixture send failed")

    device.send = send
    lan = Lan302Client(
        uid="account", device_id="camera", session_id="session",
        local_key="0123456789abcdef", on_answer=lambda _: None,
        on_candidate=lambda _: None, on_disconnect=lambda _: None,
        preconnect=False, security_level=3,
    )
    if send_failure:
        with pytest.raises(NativeLanSignalError):
            lan._send(device, message)
    else:
        lan._send(device, message)
    assert sent == [message]
    assert device.version_header == original_header


@pytest.mark.parametrize("version", [3.3, 3.4, 3.5])
def test_lan_sd_query_uses_version_specific_apk_body(version):
    device = tinytuya.Device("camera", "192.0.2.9", "0123456789abcdef", version=version)
    sent = []
    message = {"devId": "camera", "uid": "account", "t": 1794050000, "dps": {"110": None}}

    def send_receive(payload):
        sent.append(payload)
        device.cmd_retcode = 0
        body = json.loads(payload.payload)
        assert payload.cmd == 7
        assert body == (
            {"protocol": 5, "t": 1794050000, "data": message}
            if version >= 3.4 else message
        )
        assert decrypt_lan_request(device, payload) == body
        return {"data": {"dps": {"110": "1"}}}

    device._send_receive = send_receive
    device.receive = lambda: pytest.fail("Immediate SD report must not trigger another read")
    lan = Lan302Client(
        uid="account", device_id="camera", session_id="session",
        local_key="0123456789abcdef", on_answer=lambda _: None,
        on_candidate=lambda _: None, on_disconnect=lambda _: None,
        preconnect=False, security_level=3,
    )
    assert lan._send_query(device, message) == {"data": {"dps": {"110": "1"}}}
    assert len(sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [3.3, 3.4, 3.5])
@pytest.mark.parametrize("outcome", ["report", "delivery_failure", "ack_only"])
async def test_actual_lan_query_to_sd_status_and_offer(monkeypatch, version, outcome):
    config = replace(
        camera_config(), wake_direct=True, wake_capability="low_power",
        sd_low_power_wake=True, sd_status_lan_dp="110",
        sd_status_mqtt_dp="110", sd_status_mqtt_capable=True,
        sd_status_lan_error_route="mqtt",
    )
    playback = playback_module.NativeRelayPlaybackSession(config, APP_SESSION)
    playback._on_demand_catalog = True
    playback._connector_wake_allowed = True
    events = []
    device = tinytuya.Device(config.dev_id, "192.0.2.9", config.local_key, version=version)
    device._get_socket = lambda renew: True
    device.close = lambda: events.append("lan-closed")

    def send_receive(payload):
        events.append("lan-query")
        body = decrypt_lan_request(device, payload)
        data = body["data"] if version >= 3.4 else body
        assert data["dps"] == {"110": None}
        if version >= 3.4:
            assert body["protocol"] == 5
        device.cmd_retcode = 1 if outcome == "delivery_failure" else 0
        return {"data": {"dps": {"110": 1}}} if outcome == "report" else None

    device._send_receive = send_receive
    device.receive = lambda: None
    monkeypatch.setattr(Lan302Client, "_open_device", lambda self: device)
    monkeypatch.setattr(playback_module, "CAMERA_COMMANDS", CameraCommandQueue())
    monkeypatch.setattr(playback_module, "_SD_STATUS_TIMEOUT_SECONDS", 0.2)

    class Broker:
        def __init__(self, **kwargs):
            self.queue = asyncio.Queue()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            events.append("mqtt-closed")

        async def subscribe(self, *args, **kwargs):
            return (1,)

        async def publish(self, topic, payload, **kwargs):
            if topic.startswith("m/w/"):
                events.append("wake")
                reply = encode_payload(
                    config.local_key.encode(), 1, b"test",
                    {"devId": config.dev_id, "online": True, "lowPowerConnect": True}, 1, 1, "2.3",
                )
                self.queue.put_nowait(SimpleNamespace(topic="smart/mb/in/camera-device", payload=reply))
                return
            protocol, data = decode_payload(config.local_key.encode(), payload)
            if protocol == 5:
                assert outcome == "delivery_failure"
                assert data["dps"] == {"110": None}
                events.append("mqtt-query")
                reply = encode_payload(config.local_key.encode(), 2, b"test",
                                       {"devId": config.dev_id, "dps": {"110": 1}}, 1, 4, "2.3")
                self.queue.put_nowait(SimpleNamespace(topic="smart/mb/in/camera-device", payload=reply))
            else:
                assert protocol == 302 and data["header"]["type"] == "offer"
                assert playback._sd_status_query == "report_received"
                events.append("offer")

        @property
        async def messages(self):
            while True:
                yield await self.queue.get()

    monkeypatch.setattr(moto_client.aiomqtt, "Client", Broker)
    try:
        if outcome == "ack_only":
            with pytest.raises(playback_module.NativeRelayPlaybackError, match="did not report SD status"):
                await playback._open_signaling("sdp", [], "trace", {}, {})
            assert events == ["wake", "lan-query", "lan-closed"]
        else:
            await playback._open_signaling("sdp", [], "trace", {}, {})
            assert events == ["wake", "lan-query", "lan-closed"] + (
                ["mqtt-query"] if outcome == "delivery_failure" else []
            ) + ["offer"]
            assert playback._sd_status_report_route == ("lan" if outcome == "report" else "mqtt")
        assert playback._moto._dp_query is None
        assert playback._status_lan is None
        assert f"version={version}" in playback.transport_diagnostics
        assert f"query_protocol={'5' if version >= 3.4 else 'legacy'}" in playback.transport_diagnostics
        expected_state = {"report": "report_received", "delivery_failure": "delivery_failed",
                          "ack_only": "acknowledged"}[outcome]
        assert f"query_state={expected_state}" in playback.transport_diagnostics
    finally:
        await playback._moto.async_close()
    assert events[-1] == "mqtt-closed"
