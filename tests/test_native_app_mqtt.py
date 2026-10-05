from __future__ import annotations

import hashlib

import pytest

from custom_components.tuya_recordings.lib.native_app_mqtt import (
    MQTT_KEEPALIVE,
    NativeAppMqttClient,
    NativeAppMqttConfig,
    NativeAppMqttError,
)
from custom_components.tuya_recordings.lib.native_gateway import (
    CH_KEY,
    CLIENT_ID,
    COMPOSITE_KEY,
    PACKAGE_NAME,
    native_app_profile,
)


SESSION = {
    "sid": "session-id",
    "ecode": "ecode-value",
    "uid": "user-id",
    "partner_identity": "p1000018",
    "mobile_mqtts_url": "ssl://m1.tuyaus.com:8883",
    "device_fingerprint": "device-fingerprint",
}


def md5(value):
    return hashlib.md5(value.encode(), usedforsecurity=False).hexdigest()


def test_config_reproduces_smart_life_consumer_mqtt_identity():
    config = NativeAppMqttConfig.from_saved_session(SESSION)

    tail = md5(md5(CLIENT_ID) + SESSION["ecode"])[-16:]
    assert config.host == "m1.tuyaus.com"
    assert config.port == 8883
    assert config.username == (
        f"p1000018_v1_{CLIENT_ID}_{CH_KEY}_mb_session-id{tail}"
    )
    assert config.password == md5(md5(COMPOSITE_KEY) + "ecode-value")[8:24]
    assert config.client_id == (
        f"{PACKAGE_NAME}_mb_device-fingerprint_"
        f"{md5('user-idsdkfasodifca')}_DEFAULT"
    )
    assert "ecode-value" not in repr(config)


def test_config_uses_the_selected_tuya_smart_identity():
    profile = native_app_profile("tuya_smart")
    config = NativeAppMqttConfig.from_saved_session(SESSION, app_profile_id="tuya_smart")

    assert f"_v1_{profile.client_id}_{profile.ch_key}_mb_" in config.username
    assert config.client_id.startswith(f"{profile.package_name}_mb_")
    assert config.password == md5(md5(profile.composite_key) + SESSION["ecode"])[8:24]


def test_config_uses_authenticated_qr_broker_and_partner():
    config = NativeAppMqttConfig.from_saved_session(
        {
            **SESSION,
            "partner_identity": "p2000027",
            "mobile_mqtts_url": "mqtts://m1-eu.lifeaiot.com:443",
        },
        "eu",
    )

    assert config.host == "m1-eu.lifeaiot.com"
    assert config.port == 443
    assert config.username.startswith("p2000027_v1_")


def test_config_accepts_eu_tuya_smart_broker():
    config = NativeAppMqttConfig.from_saved_session(
        {**SESSION, "mobile_mqtts_url": "ssl://m1.tuyaeu.com:8883"}, "eu"
    )

    assert config.host == "m1.tuyaeu.com"


@pytest.mark.parametrize(
    "updates",
    [
        {"device_fingerprint": ""},
        {"sid": ""},
        {"ecode": ""},
        {"uid": ""},
        {"partner_identity": ""},
        {"mobile_mqtts_url": ""},
    ],
)
def test_config_rejects_incomplete_or_non_mqtt_identity(updates):
    with pytest.raises(NativeAppMqttError):
        NativeAppMqttConfig.from_saved_session({**SESSION, **updates})


def test_config_rejects_unknown_region():
    with pytest.raises(NativeAppMqttError, match="region"):
        NativeAppMqttConfig.from_saved_session(SESSION, "ca")


class FakeClient:
    def __init__(self):
        self.on_connect = None
        self.on_disconnect = None
        self.on_subscribe = None
        self.callbacks = {}
        self.calls = []

    def username_pw_set(self, username, password):
        self.calls.append(("credentials", username, password))

    def tls_set(self, **kwargs):
        self.calls.append(("tls", kwargs))

    def message_callback_add(self, topic, callback):
        self.callbacks[topic] = callback

    def message_callback_remove(self, topic):
        self.callbacks.pop(topic, None)

    def connect(self, host, port, keepalive):
        self.calls.append(("connect", host, port, keepalive))
        return 0

    def loop_start(self):
        self.calls.append(("loop_start",))
        self.on_connect(self, None, {}, 0)

    def subscribe(self, topic, qos):
        self.calls.append(("subscribe", topic, qos))
        self.on_subscribe(self, None, 1, [qos])
        return (0, 1)

    def unsubscribe(self, topic):
        self.calls.append(("unsubscribe", topic))

    def disconnect(self):
        self.calls.append(("disconnect",))

    def loop_stop(self):
        self.calls.append(("loop_stop",))


def test_client_connects_once_subscribes_then_fully_closes():
    raw = FakeClient()
    transport = NativeAppMqttClient(
        NativeAppMqttConfig.from_saved_session(SESSION),
        client_factory=lambda client_id: raw,
    )
    def callback(*args):
        pass

    assert transport.start("smart/mb/in/camera", callback) is raw
    assert raw.calls[-1] == ("subscribe", "smart/mb/in/camera", 1)
    assert ("connect", "m1.tuyaus.com", 8883, MQTT_KEEPALIVE) in raw.calls
    transport.close("smart/mb/in/camera")

    assert raw.callbacks == {}
    assert raw.calls[-2:] == [("disconnect",), ("loop_stop",)]


def test_client_fails_closed_on_connack_and_does_not_retry():
    raw = FakeClient()

    def rejected_loop():
        raw.calls.append(("loop_start",))
        raw.on_connect(raw, None, {}, 5)

    raw.loop_start = rejected_loop
    transport = NativeAppMqttClient(
        NativeAppMqttConfig.from_saved_session(SESSION),
        client_factory=lambda client_id: raw,
        connect_timeout=0.1,
    )

    with pytest.raises(NativeAppMqttError, match="connection failed"):
        transport.start("smart/mb/in/camera", lambda *args: None)
    assert len([call for call in raw.calls if call[0] == "connect"]) == 1
