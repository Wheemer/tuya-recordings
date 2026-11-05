import base64
import json

from custom_components.tuya_recordings.alerts import (
    decode_notification_message,
    notification_event_code,
    notification_is_cleared,
)


def test_notification_event_code_matches_explicit_ipc_code():
    assert notification_event_code("unknown", {"event_type": "ipc_motion"}) == "ipc_motion"


def test_notification_event_code_matches_nested_json_payload():
    payload = {"message": '{"msgCode":"ipc_human","id":"abc"}'}

    assert notification_event_code("unknown", payload) == "ipc_human"


def test_notification_event_code_keeps_unknown_camera_ipc_notifications():
    assert notification_event_code("unknown", {"cmd": "ipc_bang"}) == "ipc_bang"


def test_notification_event_code_ignores_detection_setting_names():
    assert notification_event_code("on", {"friendly_name": "Camera Motion alarm"}) is None


def test_notification_clear_requires_explicit_alarm_false():
    assert notification_is_cleared({"cmd": "ipc_human", "alarm": False}) is True
    assert notification_is_cleared({"cmd": "ipc_human", "alarm": True}) is False
    assert notification_is_cleared({"cmd": "ipc_human"}) is False


def test_decode_notification_message_extracts_camera_alert():
    encoded = base64.b64encode(
        json.dumps({"cmd": "ipc_human", "time": 12345}).encode()
    ).decode()

    assert decode_notification_message(encoded) == {"cmd": "ipc_human", "time": 12345}


def test_decode_notification_message_rejects_invalid_values():
    assert decode_notification_message("not base64 or json") is None
    assert decode_notification_message(base64.b64encode(b"[]").decode()) is None


def test_notification_discovery_is_account_scoped_and_adds_late_sources(monkeypatch):
    from types import SimpleNamespace
    import custom_components.tuya_recordings.alerts as module

    sources = ["event.first_doorbell_message", "event.other_doorbell_message"]
    listeners, added = {}, []
    monkeypatch.setattr(module, "tuya_camera_event_entities", lambda _: list(sources))
    monkeypatch.setattr(module, "tuya_notification_source",
        lambda _, entity: (entity.split('.')[1].removesuffix('_doorbell_message'), None))
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _: None),
        bus=SimpleNamespace(async_listen=lambda name, callback: listeners.setdefault(name, callback)))
    entry = SimpleNamespace(data={"device_inventory": {"first": "First", "late": "Late"}},
        async_on_unload=lambda _: None)
    module.watch_camera_notification_sources(hass, entry, lambda *args: added.append(args))
    assert [source[2] for source in added] == ["first"]
    sources.append("event.late_doorbell_message")
    listeners["entity_registry_updated"]()
    listeners["entity_registry_updated"]()
    assert [source[2] for source in added] == ["first", "late"]
