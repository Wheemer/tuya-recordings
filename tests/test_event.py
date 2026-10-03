import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock

from custom_components.tuya_recordings.event import TuyaRecordingAlertEvent


def test_camera_event_uses_the_camera_notification_without_a_hold_timer():
    raw_value = base64.b64encode(
        json.dumps({"cmd": "ipc_human", "alarm": True, "time": 12345}).encode()
    ).decode()
    device = SimpleNamespace(status={"initiative_message": raw_value})
    event = TuyaRecordingAlertEvent(
        hass=SimpleNamespace(),
        entry=SimpleNamespace(entry_id="test"),
        event_entity_id="event.camera_doorbell_message",
        camera_name="Test camera",
        tuya_device_id="camera-id",
        tuya_manager=SimpleNamespace(device_map={"camera-id": device}),
    )
    event._trigger_event = Mock()
    event.async_write_ha_state = Mock()

    event._handle_tuya_device_update(["initiative_message"], {"initiative_message": 456})

    event._trigger_event.assert_called_once_with(
        "detected",
        {
            "cmd": "ipc_human",
            "alarm": True,
            "time": 12345,
            "notification_dp": "initiative_message",
            "dp_timestamp": 456,
            "tuya_event_code": "ipc_human",
        },
    )
    event.async_write_ha_state.assert_called_once()


def test_camera_event_keeps_the_raw_camera_alert_code():
    raw_value = base64.b64encode(json.dumps({"cmd": "ipc_bang"}).encode()).decode()
    device = SimpleNamespace(status={"initiative_message": raw_value})
    event = TuyaRecordingAlertEvent(
        hass=SimpleNamespace(),
        entry=SimpleNamespace(entry_id="test"),
        event_entity_id="event.camera_doorbell_message",
        camera_name="Test camera",
        tuya_device_id="camera-id",
        tuya_manager=SimpleNamespace(device_map={"camera-id": device}),
    )
    event._trigger_event = Mock()
    event.async_write_ha_state = Mock()

    event._handle_tuya_device_update(["initiative_message"], None)

    event._trigger_event.assert_called_once_with(
        "detected",
        {"cmd": "ipc_bang", "notification_dp": "initiative_message", "tuya_event_code": "ipc_bang"},
    )
    event.async_write_ha_state.assert_called_once()


def test_camera_event_records_only_an_explicit_camera_clear():
    raw_value = base64.b64encode(
        json.dumps({"cmd": "ipc_human", "alarm": False}).encode()
    ).decode()
    device = SimpleNamespace(status={"initiative_message": raw_value})
    event = TuyaRecordingAlertEvent(
        hass=SimpleNamespace(),
        entry=SimpleNamespace(entry_id="test"),
        event_entity_id="event.camera_doorbell_message",
        camera_name="Test camera",
        tuya_device_id="camera-id",
        tuya_manager=SimpleNamespace(device_map={"camera-id": device}),
    )
    event._trigger_event = Mock()
    event.async_write_ha_state = Mock()

    event._handle_tuya_device_update(["initiative_message"], None)

    event._trigger_event.assert_called_once_with(
        "cleared",
        {"cmd": "ipc_human", "alarm": False, "notification_dp": "initiative_message", "tuya_event_code": "ipc_human"},
    )
    event.async_write_ha_state.assert_called_once()
