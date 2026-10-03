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
