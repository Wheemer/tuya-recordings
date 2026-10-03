import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.components.binary_sensor import BinarySensorDeviceClass

from custom_components.tuya_recordings.binary_sensor import (
    MOTION_CODES,
    PERSON_CODES,
    TuyaRecordingAlertBinarySensor,
)


def _sensor(*, key: str, codes: frozenset[str], raw_value: str):
    device = SimpleNamespace(status={"initiative_message": raw_value})
    sensor = TuyaRecordingAlertBinarySensor(
        hass=SimpleNamespace(),
        entry=SimpleNamespace(entry_id="test"),
        event_entity_id="event.camera_doorbell_message",
        camera_name="Test camera",
        key=key,
        label=f"{key.title()} detected",
        codes=codes,
        device_class=BinarySensorDeviceClass.MOTION,
        tuya_device_id="camera-id",
        tuya_manager=SimpleNamespace(device_map={"camera-id": device}),
    )
    sensor.async_write_ha_state = Mock()
    return sensor, device


def test_notification_dispatcher_marks_motion_and_person_detected():
    raw_value = base64.b64encode(
        json.dumps({"cmd": "ipc_human", "alarm": True, "time": 12345}).encode()
    ).decode()
    motion, _ = _sensor(key="motion", codes=MOTION_CODES, raw_value=raw_value)
    person, _ = _sensor(key="person", codes=PERSON_CODES, raw_value=raw_value)

    motion._handle_tuya_device_update(["initiative_message"], {"initiative_message": 456})
    person._handle_tuya_device_update(["initiative_message"], {"initiative_message": 456})

    assert motion.is_on is True
    assert person.is_on is True
    assert motion.extra_state_attributes["last_event_type"] == "ipc_human"
    assert person.extra_state_attributes["last_event_attributes"]["dp_timestamp"] == 456


def test_non_person_motion_notification_does_not_set_person_sensor():
    raw_value = base64.b64encode(json.dumps({"cmd": "ipc_car"}).encode()).decode()
    motion, _ = _sensor(key="motion", codes=MOTION_CODES, raw_value=raw_value)
    person, _ = _sensor(key="person", codes=PERSON_CODES, raw_value=raw_value)

    motion._handle_tuya_device_update(["initiative_message"], None)
    person._handle_tuya_device_update(["initiative_message"], None)

    assert motion.is_on is True
    assert person.is_on is False


def test_explicit_camera_clear_turns_off_the_matching_sensor():
    detected = base64.b64encode(
        json.dumps({"cmd": "ipc_human", "alarm": True}).encode()
    ).decode()
    cleared = base64.b64encode(
        json.dumps({"cmd": "ipc_human", "alarm": False}).encode()
    ).decode()
    sensor, device = _sensor(key="person", codes=PERSON_CODES, raw_value=detected)

    sensor._handle_tuya_device_update(["initiative_message"], None)
    device.status["initiative_message"] = cleared
    sensor._handle_tuya_device_update(["initiative_message"], None)

    assert sensor.is_on is False
