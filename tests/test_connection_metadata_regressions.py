"""APK metadata and independent answer-channel regressions."""

import asyncio
import json
import socket
import struct
from dataclasses import replace

import pytest
import tinytuya

from test_native_relay_playback import APP_SESSION, answer_sdp, camera_config
from custom_components.tuya_recordings.client import camera_wake_capabilities_from_native_devices
from custom_components.tuya_recordings.lib import native_relay_playback as runtime
from custom_components.tuya_recordings.lib.native_gateway import (
    _apply_sd_wake_metadata, _device_mqtt_protocol_version,
)
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.exceptions import TuyaIpcP2pError
from custom_components.tuya_recordings.lib.native_lan_signaling import Lan302Client
from custom_components.tuya_recordings.lib.native_provider import NativeAppCameraConfigProvider
from test_native_provider import camera_info


def metadata(*, modes=(0, 1), low_power=True):
    return {
        "devId": "camera", "category": "sp", "cloudOnline": True, "cadv": "1.0.2",
        "communication": {"communicationNode": "camera", "dataModel": 1,
                          "communicationModes": [{"type": kind, "pv": "2.3"} for kind in modes]},
        "skills": {"p2pType": 2}, "dataPointInfo": {"dps": {"149": False} if low_power else {}},
        "productBean": {"schemaInfo": {"schema": json.dumps([{"id": "247", "code": "sd_status"}])}},
    }


def test_communication_pv_overrides_legacy_summary():
    assert _device_mqtt_protocol_version({**metadata(), "pv": "2.2"}) == "2.3"
    assert _device_mqtt_protocol_version({"pv": "2.2"}, allow_legacy=False) is None


@pytest.mark.parametrize("modes", [(1,), (2,), (0, 1)])
def test_sd_route_snapshot_replaces_obsolete_dp_and_route(modes):
    row = {"sdStatusLanDp": "110", "sdStatusLanErrorRoute": "http", "sdStatusHttpDp": "110"}
    _apply_sd_wake_metadata(row, metadata(modes=modes), "camera", source="detail")
    routes = {key: value for key, value in row.items() if key.endswith("Dp")}
    assert "110" not in routes.values()
    assert ("sdStatusLanDp" in row) == (0 in modes)
    if 0 not in modes:
        assert "sdStatusLanErrorRoute" not in row


def test_lan_capability_survives_ineligible_sd_low_power_metadata():
    row = {"devId": "camera", "category": "sp"}
    _apply_sd_wake_metadata(row, metadata(low_power=False), "camera")
    assert row["sdLowPowerWake"] is False
    assert row["lanSignalingCapable"] is True
    assert "sdStatusLanDp" not in row
    assert camera_wake_capabilities_from_native_devices([row])["camera"]["lan_signaling_capable"] is True


@pytest.mark.parametrize("profile", ["smart_life", "tuya_smart"])
@pytest.mark.parametrize("capable", [True, False])
def test_provider_preserves_lan_capability_separately_from_sd_eligibility(profile, capable):
    provider = NativeAppCameraConfigProvider(
        call_api=lambda *args: camera_info(), local_key_lookup=lambda _: "local-key",
        app_profile_id=profile,
        wake_capability_lookup=lambda _: {"direct": True, "sd_low_power": False,
                                          "lan_signaling_capable": capable},
    )
    config = provider.session_config("camera")
    assert config.lan_signaling_capable is capable
    assert config.sd_low_power_wake is False
    assert config.sd_status_lan_dp == ""


@pytest.mark.asyncio
async def test_failed_auxiliary_broker_does_not_block_lan_offer_or_repeat_wake(monkeypatch):
    events = []

    class Broker:
        def __init__(self, **kwargs):
            pass

        async def async_connect(self):
            events.append("mqtt-connect")
            raise TuyaIpcP2pError("fixture broker offline")

        async def async_close(self):
            events.append("mqtt-close")

    class Lan:
        def __init__(self, **kwargs):
            self.answer = kwargs["on_answer"]

        async def async_connect(self):
            events.append("lan-connect")

        async def async_send_offer(self, *args):
            events.append("lan-offer")
            self.answer(answer_sdp())

        async def async_send_disconnect(self):
            events.append("disconnect")

        async def async_close(self):
            events.append("lan-close")

    monkeypatch.setattr(runtime, "MotoClient", Broker)
    monkeypatch.setattr(runtime, "Lan302Client", Lan)
    monkeypatch.setattr(runtime, "_DISCONNECT_GRACE_SECONDS", 0)
    session = runtime.NativeRelayPlaybackSession(replace(camera_config(), p2p_policy=1), APP_SESSION)
    session._answer = asyncio.get_running_loop().create_future()
    session._ended = asyncio.get_running_loop().create_future()
    try:
        await session._open_signaling("sdp", [], "trace", {}, {})
        assert (await session._wait_for_answer()).aes_key
        assert session._answer_source == "lan"
        assert session._answer_mqtt is None
        assert not session._ended.done()
    finally:
        await session.close()
    assert events == ["lan-connect", "mqtt-connect", "mqtt-close", "lan-offer", "disconnect", "lan-close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("capable", [True, False])
async def test_passive_lan_listener_uses_transport_capability_not_sd_schema(monkeypatch, capable):
    events = []

    class Lan:
        def __init__(self, **kwargs):
            pass

        async def async_connect(self):
            events.append("connect")

        async def async_listen(self):
            events.append("listen")

        async def async_close(self):
            events.append("close")

    monkeypatch.setattr(runtime, "Lan302Client", Lan)
    monkeypatch.setattr(runtime, "_DISCONNECT_GRACE_SECONDS", 0)
    config = replace(camera_config(), wake_direct=True, sd_low_power_wake=False,
                     lan_signaling_capable=capable)
    session = runtime.NativeRelayPlaybackSession(config, APP_SESSION)
    session._answer = asyncio.get_running_loop().create_future()
    try:
        await session._ensure_lan_answer_listener()
        assert events == (["connect", "listen"] if capable else [])
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [3.3, 3.4, 3.5])
async def test_passive_answer_uses_real_tinytuya_encrypted_receive(version):
    """Exercise the byte decoder and real reader through a local socket pair."""
    from tinytuya.core import header as protocol
    from tinytuya.core.message_helper import TuyaMessage, pack_message
    from tinytuya.core.crypto_helper import AESCipher

    key = b"0123456789abcdef"
    client_socket, peer_socket = socket.socketpair()
    device = tinytuya.Device("camera", "192.0.2.9", key.decode(), version=version)
    device.set_socketPersistent(True)
    device.set_socketRetryLimit(0)
    device.set_retry(False)
    device.socket = client_socket
    received = []
    listener = Lan302Client(
        uid="account", device_id="camera", session_id="current", local_key=key.decode(),
        on_answer=received.append, on_candidate=lambda _: None, on_disconnect=lambda _: None,
        preconnect=False, security_level=3,
    )
    listener._device = device
    try:
        frames = []
        for sequence, session_id in enumerate(("wrong", "current"), start=1):
            payload = json.dumps({"header": {"type": "answer", "sessionid": session_id},
                                  "msg": {"sdp": answer_sdp()}}).encode()
            if version >= 3.5:
                message = TuyaMessage(sequence, 32, 0, payload, 0, True, protocol.PREFIX_6699_VALUE, True)
                frames.append(pack_message(message, hmac_key=key))
            else:
                ciphertext = AESCipher(key).encrypt(payload, use_base64=False)
                message = TuyaMessage(sequence, 32, None, struct.pack(">I", 0) + ciphertext, 0)
                frames.append(pack_message(message, hmac_key=key if version >= 3.4 else None))
        peer_socket.sendall(b"".join(frames))
        await listener.async_listen()
        async with asyncio.timeout(2):
            while not received:
                await asyncio.sleep(0.01)
        assert received == [answer_sdp()]
        assert listener._session_mismatches == 1
        assert "received=2" in listener.diagnostics
        assert "'answer': 1" in listener.diagnostics
        peer_socket.setblocking(False)
        with pytest.raises(BlockingIOError):
            peer_socket.recv(1)
    finally:
        await listener.async_close()
        peer_socket.close()
        client_socket.close()
