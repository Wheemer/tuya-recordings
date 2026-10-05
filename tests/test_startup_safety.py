import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components import tuya_recordings as integration
from custom_components.tuya_recordings.client import TuyaRecordingsClient
from custom_components.tuya_recordings.lib.native_backend import (
    NativeBackendNotConfigured,
)


class FakeAvailableBackend:
    name = "apk-native"
    clip_playback_available = True


@pytest.mark.parametrize("paused,thumbnails,media,expected", [
    (False, False, False, 1),
    (True, True, True, 0),
    (False, True, False, 1),
    (False, False, True, 1),
])
def test_startup_schedules_catalog_or_enabled_cache_work(monkeypatch, paused, thumbnails, media, expected):
    client = TuyaRecordingsClient({
        "cloud_activity_paused": paused,
        "thumbnail_sync_enabled": thumbnails,
        "media_sync_enabled": media,
    }, recordings_backend=FakeAvailableBackend())
    timers = []
    intervals = []
    unload = Mock()
    request = AsyncMock()
    catalog_request = AsyncMock()
    monkeypatch.setattr(integration, "_async_request_camera_work", request)
    monkeypatch.setattr(integration, "_async_request_catalog_refresh", catalog_request)

    def schedule(collection):
        def register(hass, *args):
            collection.append(next(arg for arg in args if callable(arg)))
            return Mock()
        return register

    monkeypatch.setattr(integration, "async_call_later", schedule(timers))
    monkeypatch.setattr(integration, "async_track_time_interval", schedule(intervals))
    hass = SimpleNamespace(data={integration.DOMAIN: {"entry": {"client": client}}})
    entry = SimpleNamespace(entry_id="entry", async_on_unload=unload)
    integration._async_schedule_media_sync(hass, entry)
    request.assert_not_called()
    assert len(timers) == expected
    assert len(intervals) == expected
    assert unload.call_count == 0

    async def fire_after_pause():
        client.cloud_activity_paused = True
        for callback in timers + intervals:
            await callback(None)

    asyncio.run(fire_after_pause())
    request.assert_not_called()
    catalog_request.assert_not_called()


def test_disabled_sync_dispatch_does_not_create_a_task():
    client = TuyaRecordingsClient({"thumbnail_sync_enabled": False, "media_sync_enabled": False})
    create = Mock(side_effect=AssertionError("Disabled sync created a background task"))
    hass = SimpleNamespace(
        data={integration.DOMAIN: {"entry": {"client": client}}},
        async_create_task=create,
    )
    asyncio.run(integration._async_request_camera_work(
        hass, "entry", "interval", media=True, thumbnails=True,
    ))
    create.assert_not_called()


def test_disabled_sync_does_not_register_recording_trigger(monkeypatch):
    client = TuyaRecordingsClient(
        {"thumbnail_sync_enabled": False, "media_sync_enabled": False},
        recordings_backend=FakeAvailableBackend(),
    )
    hass = SimpleNamespace(
        data={integration.DOMAIN: {"entry": {"client": client}}},
    )
    entry = SimpleNamespace(entry_id="entry")

    integration._async_setup_recording_triggers(hass, entry)

    assert integration.DATA_RECORDING_TRIGGER_UNSUB not in hass.data[integration.DOMAIN]["entry"]


def test_unconfigured_native_backend_does_not_schedule_startup_work(monkeypatch):
    client = TuyaRecordingsClient({
        "thumbnail_sync_enabled": True,
        "media_sync_enabled": True,
    }, recordings_backend=NativeBackendNotConfigured("APK-native media transport is not configured"))
    timers = []
    intervals = []
    unload = Mock()
    request = AsyncMock()
    monkeypatch.setattr(integration, "_async_request_camera_work", request)

    def schedule(collection):
        def register(hass, *args):
            collection.append(next(arg for arg in args if callable(arg)))
            return Mock()
        return register

    monkeypatch.setattr(integration, "async_call_later", schedule(timers))
    monkeypatch.setattr(integration, "async_track_time_interval", schedule(intervals))
    hass = SimpleNamespace(data={integration.DOMAIN: {"entry": {"client": client}}})
    entry = SimpleNamespace(entry_id="entry", async_on_unload=unload)

    integration._async_schedule_media_sync(hass, entry)

    assert timers == []
    assert intervals == []
    assert unload.call_count == 0


def test_unconfigured_native_backend_dispatch_does_not_create_a_task():
    client = TuyaRecordingsClient(
        {"thumbnail_sync_enabled": True, "media_sync_enabled": True},
        recordings_backend=NativeBackendNotConfigured("APK-native media transport is not configured"),
    )
    create = Mock(side_effect=AssertionError("Unavailable backend created a background task"))
    hass = SimpleNamespace(
        data={integration.DOMAIN: {"entry": {"client": client}}},
        async_create_task=create,
    )

    asyncio.run(integration._async_request_camera_work(
        hass, "entry", "interval", media=True, thumbnails=True,
    ))

    create.assert_not_called()


def test_interactive_playback_blocks_background_dispatch():
    client = TuyaRecordingsClient(
        {"thumbnail_sync_enabled": True}, recordings_backend=FakeAvailableBackend()
    )
    create = Mock(side_effect=AssertionError("Playback queued background camera work"))
    data = {
        "client": client,
        integration.DATA_INTERACTIVE_PLAYBACK_ACTIVE: 7,
    }
    hass = SimpleNamespace(
        data={integration.DOMAIN: {"entry": data}},
        async_create_task=create,
    )

    asyncio.run(integration._async_request_camera_work(
        hass, "entry", "interval", thumbnails=True, require_media_sync=False,
    ))

    create.assert_not_called()
    assert integration.DATA_THUMBNAIL_SYNC_PENDING not in data


def test_sensor_dispatch_schedules_state_write_on_event_loop():
    from custom_components.tuya_recordings.sensor import TuyaRecordingsClipCountSensor

    schedule = Mock()
    hass = SimpleNamespace(loop=SimpleNamespace(call_soon_threadsafe=schedule))
    sensor = TuyaRecordingsClipCountSensor(hass, SimpleNamespace(entry_id="entry", title="Test"))
    sensor.async_write_ha_state = Mock()
    sensor._handle_recordings_updated("other")
    schedule.assert_not_called()
    sensor._handle_recordings_updated("entry")
    schedule.assert_called_once_with(sensor.async_write_ha_state)
    sensor.async_write_ha_state.assert_not_called()


def test_catalog_schedule_uses_configured_safe_interval(monkeypatch):
    client = TuyaRecordingsClient(
        {"catalog_sync_minutes": 30}, recordings_backend=FakeAvailableBackend()
    )
    intervals = []
    monkeypatch.setattr(integration, "async_call_later", lambda *args: Mock())
    monkeypatch.setattr(
        integration,
        "async_track_time_interval",
        lambda hass, callback, interval: intervals.append(interval) or Mock(),
    )
    hass = SimpleNamespace(data={integration.DOMAIN: {"entry": {"client": client}}})
    entry = SimpleNamespace(entry_id="entry", async_on_unload=Mock())

    integration._async_schedule_media_sync(hass, entry)

    assert intervals == [timedelta(minutes=30)]
