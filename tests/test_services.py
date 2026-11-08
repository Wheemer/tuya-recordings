import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest


from custom_components import tuya_recordings as integration
from custom_components.tuya_recordings import (
    _entries_for_call,
    _official_tuya_camera_device_ids,
)
from custom_components.tuya_recordings.button import TuyaRecordingsRefreshButton
from custom_components.tuya_recordings.client import (
    TuyaRecordingsClient,
    camera_inventory_from_native_devices,
)
from custom_components.tuya_recordings.const import DOMAIN
from custom_components.tuya_recordings.lib.commands import (
    ON_DEMAND_DAY_CATALOG,
    ON_DEMAND_OFFER_RETRIES,
)


@pytest.mark.parametrize("media_sync", [False, True])
def test_refresh_button_updates_inventory_for_its_entry_only(monkeypatch, media_sync):
    entry = SimpleNamespace(entry_id="target", title="Target", data={"native_app_session": {}}, options={})
    client = TuyaRecordingsClient({})
    other = TuyaRecordingsClient({})
    client.media_sync_enabled = media_sync
    updates = {"device_protocol_versions": {"camera": "2.3"}}
    calls = []

    def refresh_inventory(data):
        calls.append("inventory")
        assert data["native_app_session"] == {}
        return updates

    def refresh_recent():
        assert ON_DEMAND_DAY_CATALOG.get() and ON_DEMAND_OFFER_RETRIES.get()
        calls.append("catalog")

    async def refresh_catalog(hass, entry_id, reason, *, wait):
        assert (entry_id, reason, wait) == ("target", "service", True)
        calls.append("catalog")

    async def execute(function, *args):
        return await asyncio.get_running_loop().run_in_executor(None, function, *args)

    services = SimpleNamespace(has_service=Mock(return_value=False), async_register=Mock())

    async def call_action(domain, service, data, *, blocking):
        assert (domain, service, data, blocking) == (DOMAIN, "refresh_recordings", {"entry_id": "target"}, True)
        handler = next(args[2] for args, _ in services.async_register.call_args_list if args[:2] == (domain, service))
        await handler(SimpleNamespace(data=data))

    services.async_call = call_action
    hass = SimpleNamespace(
        services=services,
        data={DOMAIN: {"target": {"entry": entry, "client": client}, "other": {"client": other}}},
        config_entries=SimpleNamespace(async_update_entry=Mock()),
        async_add_executor_job=execute,
    )
    monkeypatch.setattr(client, "refresh_inventory", refresh_inventory)
    monkeypatch.setattr(client, "refresh_recent_recordings", refresh_recent)
    monkeypatch.setattr(integration, "_async_request_catalog_refresh", refresh_catalog)
    monkeypatch.setattr(integration, "async_dispatcher_send", Mock())
    integration._async_register_services(hass)

    asyncio.run(TuyaRecordingsRefreshButton(hass, entry).async_press())

    assert calls == ["inventory", "catalog"]
    hass.config_entries.async_update_entry.assert_called_once_with(entry, data={**entry.data, **updates})


def test_only_manual_catalog_service_enables_offer_resends(monkeypatch):
    client = TuyaRecordingsClient({})
    observed = []

    def refresh(limit):
        observed.append((ON_DEMAND_DAY_CATALOG.get(), ON_DEMAND_OFFER_RETRIES.get()))
        return {}

    async def execute(function, *args):
        return await asyncio.get_running_loop().run_in_executor(None, function, *args)

    monkeypatch.setattr(client, "refresh_background_catalog", refresh)
    monkeypatch.setattr(integration, "async_dispatcher_send", Mock())
    hass = SimpleNamespace(
        data={DOMAIN: {"entry": {"client": client}}},
        async_add_executor_job=execute,
    )
    for reason in ("catalog_cycle", "service", "catalog_cycle"):
        asyncio.run(integration._async_run_catalog_refresh(hass, "entry", reason))
    assert observed == [(False, False), (True, True), (False, False)]


def test_scheduled_catalog_drops_inherited_manual_context(monkeypatch):
    from custom_components.tuya_recordings.lib.commands import MANUAL_REFRESH_ID, manual_catalog_refresh
    client = TuyaRecordingsClient({})
    observed = []
    def refresh(limit):
        observed.append((MANUAL_REFRESH_ID.get(), ON_DEMAND_DAY_CATALOG.get(), ON_DEMAND_OFFER_RETRIES.get()))
        return {}
    async def execute(function, *args):
        return await asyncio.get_running_loop().run_in_executor(None, function, *args)
    monkeypatch.setattr(client, "refresh_background_catalog", refresh)
    monkeypatch.setattr(integration, "async_dispatcher_send", Mock())
    hass = SimpleNamespace(data={DOMAIN: {"entry": {"client": client}}}, async_add_executor_job=execute)
    async def scenario():
        token = MANUAL_REFRESH_ID.set("old-manual")
        try:
            with manual_catalog_refresh():
                await integration._async_run_catalog_refresh(hass, "entry", "catalog_cycle")
                assert MANUAL_REFRESH_ID.get() == "old-manual"
                assert ON_DEMAND_DAY_CATALOG.get() and ON_DEMAND_OFFER_RETRIES.get()
        finally:
            MANUAL_REFRESH_ID.reset(token)
    asyncio.run(scenario())
    assert observed == [("-", False, False)]


def test_manual_catalog_waits_for_scheduled_pass_then_runs_with_its_own_scope(monkeypatch):
    import threading

    client = TuyaRecordingsClient({})
    observed = []
    release = threading.Event()
    data = {"client": client}
    monkeypatch.setattr(integration, "_client_recording_backend_available", lambda _: True)
    monkeypatch.setattr(integration, "async_dispatcher_send", Mock())

    async def scenario():
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def refresh(limit):
            observed.append((ON_DEMAND_DAY_CATALOG.get(), ON_DEMAND_OFFER_RETRIES.get()))
            if len(observed) == 1:
                loop.call_soon_threadsafe(started.set)
                assert release.wait(3)
            return {}

        async def execute(function, *args):
            return await asyncio.get_running_loop().run_in_executor(None, function, *args)

        monkeypatch.setattr(client, "refresh_background_catalog", refresh)
        hass = SimpleNamespace(
            data={DOMAIN: {"entry": data}}, async_add_executor_job=execute,
            async_create_task=asyncio.create_task,
        )
        await integration._async_request_catalog_refresh(hass, "entry", "catalog_cycle")
        scheduled = data[integration.DATA_CATALOG_REFRESH_TASK]
        manual = None
        try:
            await asyncio.wait_for(started.wait(), 2)
            manual = asyncio.create_task(
                integration._async_request_catalog_refresh(hass, "entry", "service", wait=True)
            )
            await asyncio.sleep(0)
            assert not manual.done() and not scheduled.cancelled()
            assert observed == [(False, False)]
        finally:
            release.set()
            await asyncio.wait_for(scheduled, 3)
            if manual is not None:
                await asyncio.wait_for(manual, 3)
        assert integration.DATA_CATALOG_REFRESH_TASK not in data
        await integration._async_request_catalog_refresh(hass, "entry", "catalog_cycle", wait=True)

    asyncio.run(scenario())
    assert observed == [(False, False), (True, True), (False, False)]


def test_manual_catalog_bypasses_scheduler_backoff_with_short_repeat_cooldown(tmp_path, monkeypatch, caplog):
    import logging
    from datetime import datetime, timedelta, timezone
    from custom_components.tuya_recordings.lib.commands import MANUAL_REFRESH_ID, manual_catalog_refresh

    client = TuyaRecordingsClient(
        {"device_inventory": {"camera": "Camera"}},
        cache_path=tmp_path / "catalog.json",
    )
    now = datetime.now(timezone.utc)
    client.set_camera_inventory({"camera": "Camera"})
    client._camera_index_cache = {"cameras": [{
        "devId": "camera", "clips": [],
        "catalogErrorAt": now.isoformat(),
        "catalogRetryAfter": (now + timedelta(minutes=5)).isoformat(),
        "bridgeStatus": {"error": "old-error-not-a-new-attempt"},
    }]}
    query = Mock(return_value=[])
    monkeypatch.setattr(client, "_recordings_for_day", query)
    caplog.set_level(logging.INFO, logger="custom_components.tuya_recordings")
    token = MANUAL_REFRESH_ID.set("fixture-refresh")
    try:
        with manual_catalog_refresh():
            result = client.refresh_background_catalog(1)
            client.refresh_background_catalog(1)
    finally:
        MANUAL_REFRESH_ID.reset(token)
    assert query.call_count == 1
    assert result["cameras"][0]["bridgeStatus"]["error"] == ""
    assert "request_id=fixture-refresh" in caplog.text
    assert "reason=manual_cooldown" in caplog.text
    assert "catalog query started" in caplog.text
    assert "catalog query failed" not in caplog.text
    assert "old-error-not-a-new-attempt" not in caplog.text
    assert MANUAL_REFRESH_ID.get() == "-"


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
