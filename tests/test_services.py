import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


from custom_components import tuya_recordings as integration
from custom_components.tuya_recordings import (
    _entries_for_call,
    _official_tuya_camera_device_ids,
)
from custom_components.tuya_recordings.client import (
    TuyaRecordingsClient,
    camera_inventory_from_native_devices,
)
from custom_components.tuya_recordings.const import DOMAIN


def test_runtime_uses_refreshed_pv_even_with_stale_options():
    key = "device_protocol_versions"
    entry = SimpleNamespace(data={key: {"camera": "2.3"}}, options={key: {"camera": "2.2"}})
    assert integration._entry_runtime_data(entry)[key] == {"camera": "2.3"}
    entry.data[key] = {}
    assert integration._entry_runtime_data(entry)[key] == {}
    entry.data = {}
    assert key not in integration._entry_runtime_data(entry)


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


def test_native_camera_inventory_uses_apk_metadata_without_probing_devices():
    inventory = camera_inventory_from_native_devices([
        {"devId": "doorbell", "name": "Front Door", "category": "sp"},
        {"devId": "camera", "name": "Garage Camera", "category": "unknown"},
        {"devId": "light", "name": "Kitchen Light", "category": "dj"},
    ])

    assert inventory == {"doorbell": "Front Door", "camera": "Garage Camera"}

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


def test_setup_prefers_persisted_native_app_camera_inventory(monkeypatch, tmp_path):
    monkeypatch.setattr(integration, "_async_update_camera_repair_issues", lambda *args: None)
    monkeypatch.setattr(integration, "native_app_session", lambda data: None)
    monkeypatch.setattr(integration, "_async_register_services", lambda hass: None)
    monkeypatch.setattr(integration, "_async_register_views", lambda hass: None)
    monkeypatch.setattr(integration, "_async_schedule_media_sync", lambda *args: None)
    monkeypatch.setattr(integration, "async_register_frontend", AsyncMock())
    client = Mock()
    monkeypatch.setattr(integration, "TuyaRecordingsClient", Mock(return_value=client))
    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
        config_entries=SimpleNamespace(async_forward_entry_setups=AsyncMock()),
        bus=SimpleNamespace(async_listen=Mock()),
        async_add_executor_job=AsyncMock(),
        data={},
    )
    entry = SimpleNamespace(
        entry_id="entry",
        data={"device_inventory": {"tuya-porch": "Porch Cam", "tuya-garage": "Garage"}},
        options={},
        async_on_unload=Mock(),
        add_update_listener=Mock(return_value=Mock()),
    )

    assert asyncio.run(integration.async_setup_entry(hass, entry)) is True
    client.set_camera_inventory.assert_called_once_with(
        {"tuya-porch": "Porch Cam", "tuya-garage": "Garage"},
        source="native_app_inventory",
    )
