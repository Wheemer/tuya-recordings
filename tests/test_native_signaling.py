import asyncio
import json

from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.models import (
    MqttIdentity,
)
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling import (
    MotoClient,
    relay_candidate_frame,
    relay_offer_frame,
)


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
    )

    async def capture(protocol, data):
        published.append((protocol, data))

    client._async_publish = capture
    asyncio.run(client.async_send_offer("sdp", [], "trace", {}, {}))

    header = published[0][1]["header"]
    assert header["moto_id"] == ""
    assert header["security_level"] == 3


def test_relay_frames_preserve_apk_signaling_header():
    offer = json.loads(
        relay_offer_frame(
            "account", "camera", "session", "trace", "sdp", [], {}, {}
        )
    )
    candidate = json.loads(
        relay_candidate_frame(
            "account", "camera", "session", "trace", "candidate:1"
        )
    )

    assert offer["header"]["moto_id"] == ""
    assert offer["header"]["security_level"] == 3
    assert candidate["header"]["moto_id"] == ""
