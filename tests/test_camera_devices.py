"""Inventory-backed camera devices with the real HA registries, no camera I/O."""

from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry, ConfigEntries
from homeassistant.helpers import device_registry as dr, entity_registry as er, area_registry as ar
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.tuya_recordings.client import TuyaRecordingsClient
from custom_components.tuya_recordings.const import DOMAIN, SIGNAL_RECORDINGS_UPDATED
from custom_components.tuya_recordings.devices import (
    async_setup_camera_devices, camera_identifier,
)
from custom_components.tuya_recordings import event as event_platform, binary_sensor
from custom_components.tuya_recordings.alerts import safe_unique_fragment


@pytest_asyncio.fixture
async def environment(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    dr.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    await dr.async_load(hass, load_empty=True)
    await er.async_load(hass, load_empty=True)
    await ar.async_load(hass, load_empty=True)
    monkeypatch.setattr(hass.config_entries, "async_setup", AsyncMock(return_value=True))
    entries = []

    async def entry(domain=DOMAIN, inventory=None):
        value = ConfigEntry(version=1, minor_version=1, domain=domain,
                            data={"device_inventory": inventory or {}}, options={},
                            title="Camera account", source="user", unique_id=None,
                            discovery_keys={}, subentries_data=[])
        await hass.config_entries.async_add(value)
        entries.append(value)
        return value

    try:
        yield hass, entry
    finally:
        for value in entries:
            await value._async_process_on_unload(hass)
        await hass.async_stop(force=True)


def cameras(hass, entry):
    return [device for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
            if (DOMAIN, entry.entry_id) not in device.identifiers]


def device_for(hass, entry, identifier):
    return next(device for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
                if identifier in device.identifiers)


@pytest.mark.asyncio
async def test_all_inventory_cameras_exist_without_notifications(environment):
    hass, create_entry = environment
    inventory = {"one": "Front", "two": "Back", "three": "Side"}
    entry = await create_entry(inventory=inventory)
    client = TuyaRecordingsClient({})
    client.set_camera_inventory(inventory)
    client.refresh_inventory = Mock(side_effect=AssertionError("No account or camera calls"))
    client.cached_camera_index = Mock(side_effect=AssertionError("No catalog calls"))
    async_setup_camera_devices(hass, entry, client)
    devices = cameras(hass, entry)
    assert {device.name for device in devices} == set(inventory.values())
    hub = device_for(hass, entry, (DOMAIN, entry.entry_id))
    assert all(device.via_device_id == hub.id for device in devices)
    assert len(er.async_get(hass).entities) == 0
    original_ids = {device.id for device in devices}
    async_dispatcher_send(hass, SIGNAL_RECORDINGS_UPDATED, entry.entry_id)
    await hass.async_block_till_done()
    assert {device.id for device in cameras(hass, entry)} == original_ids
    assert client.camera_inventory == inventory
    snapshot = client.camera_inventory
    snapshot.clear()
    assert client.camera_inventory == inventory


@pytest.mark.asyncio
async def test_legacy_notification_device_keeps_identity_and_user_metadata(environment):
    hass, create_entry = environment
    entry = await create_entry(inventory={"one": "Front", "two": "Back"})
    tuya_entry = await create_entry("tuya")
    registry = dr.async_get(hass)
    official = registry.async_get_or_create(config_entry_id=tuya_entry.entry_id,
                                            identifiers={("tuya", "one")}, name="Official camera")
    source = er.async_get(hass).async_get_or_create(
        "event", "tuya", "one_doorbell_message", device_id=official.id,
        config_entry=tuya_entry, suggested_object_id="front_doorbell_message",
    )
    legacy = (DOMAIN, f"{entry.entry_id}_{safe_unique_fragment(source.entity_id)}")
    previous = registry.async_get_or_create(config_entry_id=entry.entry_id,
                                            identifiers={legacy}, name="Front")
    area = ar.async_get(hass).async_get_or_create("Patio")
    registry.async_update_device(previous.id, name_by_user="My front camera", area_id=area.id)
    existing = er.async_get(hass).async_get_or_create(
        "binary_sensor", DOMAIN, f"{legacy[1]}_person_alert",
        device_id=previous.id, config_entry=entry, suggested_object_id="my_person_sensor",
    )
    client = TuyaRecordingsClient({})
    client.set_camera_inventory({"one": "Front", "two": "Back"})
    async_setup_camera_devices(hass, entry, client)
    updated = device_for(hass, entry, camera_identifier(entry.entry_id, "one"))
    assert updated.id == previous.id
    assert legacy in updated.identifiers
    assert updated.name_by_user == "My front camera" and updated.area_id == area.id
    assert er.async_get(hass).async_get(existing.entity_id).device_id == previous.id
    assert len(cameras(hass, entry)) == 2
    assert registry.async_get(official.id).identifiers == {("tuya", "one")}
    assert registry.async_get(official.id).name == "Official camera"


@pytest.mark.asyncio
async def test_late_notifications_attach_to_existing_inventory_device(environment):
    hass, create_entry = environment
    entry = await create_entry(inventory={"one": "Front", "two": "Back", "three": "Side"})
    client = TuyaRecordingsClient({})
    client.set_camera_inventory(dict(entry.data["device_inventory"]))
    async_setup_camera_devices(hass, entry, client)
    registry = dr.async_get(hass)
    initial = device_for(hass, entry, camera_identifier(entry.entry_id, "one"))
    events, sensors = [], []
    await event_platform.async_setup_entry(hass, entry, events.extend)
    await binary_sensor.async_setup_entry(hass, entry, sensors.extend)
    tuya_entry = await create_entry("tuya")
    official = registry.async_get_or_create(config_entry_id=tuya_entry.entry_id,
                                            identifiers={("tuya", "one")})
    source = er.async_get(hass).async_get_or_create(
        "event", "tuya", "one_doorbell_message", config_entry=tuya_entry,
        device_id=official.id, suggested_object_id="front_doorbell_message",
    )
    hass.states.async_set(source.entity_id, "unknown", {"friendly_name": "Front Doorbell message"})
    await hass.async_block_till_done()
    assert len(events) == 1 and len(sensors) == 2
    for entity in [*events, *sensors]:
        attached = registry.async_get_or_create(config_entry_id=entry.entry_id,
                                                **dict(entity.device_info))
        assert attached.id == initial.id
    assert events[0].unique_id == f"{entry.entry_id}_{safe_unique_fragment(source.entity_id)}_camera_events"
    assert {sensor.name for sensor in sensors} == {"Front Motion detected", "Front Person detected"}
    assert len(cameras(hass, entry)) == 3


@pytest.mark.asyncio
async def test_inventory_refresh_and_accounts_are_isolated_and_unload_stops_updates(environment):
    hass, create_entry = environment
    first = await create_entry(inventory={"one": "Front"})
    second = await create_entry(inventory={"one": "Other account"})
    client = TuyaRecordingsClient({})
    other = TuyaRecordingsClient({})
    client.set_camera_inventory({"one": "Front"})
    other.set_camera_inventory({"one": "Other account"})
    async_setup_camera_devices(hass, first, client)
    async_setup_camera_devices(hass, second, other)
    assert cameras(hass, first)[0].id != cameras(hass, second)[0].id
    client.set_camera_inventory({"one": "Renamed", "two": "Back"})
    async_dispatcher_send(hass, SIGNAL_RECORDINGS_UPDATED, second.entry_id)
    await hass.async_block_till_done()
    assert len(cameras(hass, first)) == 1
    async_dispatcher_send(hass, SIGNAL_RECORDINGS_UPDATED, first.entry_id)
    await hass.async_block_till_done()
    assert {device.name for device in cameras(hass, first)} == {"Renamed", "Back"}
    assert [device.name for device in cameras(hass, second)] == ["Other account"]
    await first._async_process_on_unload(hass)
    client.set_camera_inventory({"one": "Renamed", "two": "Back", "three": "Side"})
    async_dispatcher_send(hass, SIGNAL_RECORDINGS_UPDATED, first.entry_id)
    await hass.async_block_till_done()
    assert len(cameras(hass, first)) == 2
