from types import SimpleNamespace

from custom_components.tuya_recordings import (
    _client_camera_tokens,
    _entries_for_call,
    _localtuya_credential_entries,
    _official_tuya_camera_device_ids,
    _recording_trigger_entity_ids_from_states,
    _required_dependency_errors,
    _state_change_suggests_recording,
)
from custom_components.tuya_recordings.client import TuyaRecordingsClient
from custom_components.tuya_recordings.const import DOMAIN


def test_entries_for_call_returns_all_valid_clients():
    first = {"client": TuyaRecordingsClient({})}
    second = {"client": TuyaRecordingsClient({})}
    hass = SimpleNamespace(data={DOMAIN: {"one": first, "two": second, "bad": {"client": object()}}})

    assert _entries_for_call(hass, None) == [first, second]


def test_entries_for_call_returns_requested_client_only():
    first = {"client": TuyaRecordingsClient({})}
    second = {"client": TuyaRecordingsClient({})}
    hass = SimpleNamespace(data={DOMAIN: {"one": first, "two": second}})

    assert _entries_for_call(hass, "two") == [second]
    assert _entries_for_call(hass, "missing") == []


def _state(entity_id: str, state: str, friendly_name: str = ""):
    return SimpleNamespace(entity_id=entity_id, state=state, attributes={"friendly_name": friendly_name})


class _ConfigEntries:
    def __init__(self, entries_by_domain):
        self._entries_by_domain = entries_by_domain

    def async_entries(self, domain):
        return self._entries_by_domain.get(domain, [])


def test_recording_trigger_entity_filter_uses_event_status_and_storage_only():
    states = [
        _state("camera.porch_cam", "recording", "Porch Cam"),
        _state("event.porch_cam_doorbell_message", "unknown", "Porch Cam Doorbell message"),
        _state("event.ding_dong_motion", "unknown", "Ding Dong Motion"),
        _state("sensor.porch_cam_camera_display_status", "Recording", "Porch Cam Camera Display Status"),
        _state("sensor.porch_cam_local_sd_storage", "1|2|3", "Porch Cam Local SD Storage"),
        _state("switch.porch_cam_local_motion_detection", "on", "Porch Cam Local Motion Detection"),
        _state("switch.porch_cam_local_sd_recording", "on", "Porch Cam Local SD Recording"),
    ]

    assert _recording_trigger_entity_ids_from_states(states, {"porch_cam"}) == [
        "event.porch_cam_doorbell_message",
        "sensor.porch_cam_camera_display_status",
        "sensor.porch_cam_local_sd_storage",
    ]


def test_state_change_suggests_recording_for_camera_events():
    assert _state_change_suggests_recording(
        _state("event.porch_cam_doorbell_message", "unknown"),
        _state("event.porch_cam_doorbell_message", "2026-06-29T12:00:00"),
    )
    assert _state_change_suggests_recording(
        _state("sensor.porch_cam_camera_display_status", "Idle", "Porch Cam Camera Display Status"),
        _state("sensor.porch_cam_camera_display_status", "Recording", "Porch Cam Camera Display Status"),
    )
    assert _state_change_suggests_recording(
        _state("sensor.porch_cam_local_sd_storage", "1|2|3", "Porch Cam Local SD Storage"),
        _state("sensor.porch_cam_local_sd_storage", "1|3|2", "Porch Cam Local SD Storage"),
    )


def test_state_change_does_not_trigger_from_static_settings():
    assert not _state_change_suggests_recording(
        _state("switch.porch_cam_local_motion_detection", "off", "Porch Cam Local Motion Detection"),
        _state("switch.porch_cam_local_motion_detection", "on", "Porch Cam Local Motion Detection"),
    )
    assert not _state_change_suggests_recording(
        _state("sensor.porch_cam_camera_display_status", "Recording", "Porch Cam Camera Display Status"),
        _state("sensor.porch_cam_camera_display_status", "Recording", "Porch Cam Camera Display Status"),
    )


def test_client_camera_tokens_are_derived_from_cached_recording_cameras():
    client = SimpleNamespace(cached_camera_index=lambda: {"cameras": [{"name": "Porch Cam"}, {"name": "Garage Cam"}]})

    assert _client_camera_tokens(client) == {"porch_cam", "garage_cam"}


def test_required_dependency_errors_require_tuya_and_localtuya_cloud_credentials():
    hass = SimpleNamespace(config_entries=_ConfigEntries({}))

    assert _required_dependency_errors(hass) == {"tuya_required", "localtuya_required"}

    hass = SimpleNamespace(
        config_entries=_ConfigEntries(
            {
                "tuya": [SimpleNamespace(data={})],
                "localtuya": [SimpleNamespace(data={"client_id": "id"})],
            }
        )
    )

    assert _required_dependency_errors(hass) == {"localtuya_cloud_credentials_required"}

    localtuya_entry = SimpleNamespace(data={"client_id": "id", "client_secret": "secret", "user_id": "uid"})
    hass = SimpleNamespace(
        config_entries=_ConfigEntries(
            {
                "tuya": [SimpleNamespace(data={})],
                "localtuya": [localtuya_entry],
            }
        )
    )

    assert _required_dependency_errors(hass) == set()
    assert _localtuya_credential_entries(hass) == [localtuya_entry]


def test_official_tuya_camera_device_ids_come_from_tuya_camera_entities():
    entity_entries = [
        SimpleNamespace(platform="tuya", entity_id="camera.porch_cam", device_id="dev-reg-1"),
        SimpleNamespace(platform="tuya", entity_id="switch.porch_cam_privacy_mode", device_id="dev-reg-1"),
        SimpleNamespace(platform="localtuya", entity_id="camera.local_fake", device_id="dev-reg-2"),
        SimpleNamespace(platform="tuya", entity_id="camera.garage_cam_2", device_id="dev-reg-3"),
    ]
    device_entries = [
        SimpleNamespace(id="dev-reg-1", name="Porch Cam", name_by_user=None, identifiers={("tuya", "tuya-porch")}),
        SimpleNamespace(id="dev-reg-2", name="Local Fake", name_by_user=None, identifiers={("tuya", "wrong")}),
        SimpleNamespace(id="dev-reg-3", name="Side Cam", name_by_user="Garage Cam", identifiers={("tuya", "tuya-garage")}),
    ]

    assert _official_tuya_camera_device_ids(entity_entries, device_entries) == {
        "tuya-porch": "Porch Cam",
        "tuya-garage": "Garage Cam",
    }

