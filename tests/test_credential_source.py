from types import SimpleNamespace
from unittest.mock import Mock

from custom_components.tuya_recordings import (
    _async_scrub_legacy_entry_data,
    _entry_runtime_data,
)
from custom_components.tuya_recordings.switch import TuyaRecordingsCameraPauseSwitch


def test_runtime_data_uses_saved_smart_life_keys_and_scrubs_legacy_cloud_secrets():
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_update_entry=Mock(),
        )
    )
    entry = SimpleNamespace(
        data={"client_id": "old-id", "client_secret": "old-secret"},
        options={"media_sync_enabled": True},
        version=2,
    )

    runtime = _entry_runtime_data(entry)
    assert runtime["media_sync_enabled"] is True

    _async_scrub_legacy_entry_data(hass, entry, runtime)
    saved = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert "client_secret" not in saved
    assert "client_id" not in saved
    assert "_localtuya_entries" not in saved
    assert "version" not in hass.config_entries.async_update_entry.call_args.kwargs


def test_runtime_data_preserves_camera_pause_without_media_sync():
    entry = SimpleNamespace(
        data={"thumbnail_sync_enabled": False},
        options={"thumbnail_sync_enabled": False, "cloud_activity_paused": True},
    )

    runtime = _entry_runtime_data(entry)

    assert runtime["thumbnail_sync_enabled"] is False
    assert runtime["cloud_activity_paused"] is True


def test_runtime_data_enables_thumbnail_work_with_media_sync():
    entry = SimpleNamespace(
        data={},
        options={"media_sync_enabled": True, "cloud_activity_paused": True},
    )

    runtime = _entry_runtime_data(entry)

    assert runtime["thumbnail_sync_enabled"] is True
    assert runtime["cloud_activity_paused"] is True


def test_scrub_preserves_pause_when_local_caching_is_off():
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=Mock()))
    entry = SimpleNamespace(
        data={"media_sync_enabled": False, "thumbnail_sync_enabled": False},
        options={"cloud_activity_paused": True},
    )

    _async_scrub_legacy_entry_data(hass, entry, _entry_runtime_data(entry))

    saved = hass.config_entries.async_update_entry.call_args.kwargs
    assert saved["options"]["cloud_activity_paused"] is True
    assert "thumbnail_sync_enabled" not in saved["data"]


def test_master_pause_switch_remains_available_without_local_caching():
    entry = SimpleNamespace(
        entry_id="entry",
        title="Tuya Recordings",
        data={"media_sync_enabled": False},
        options={"cloud_activity_paused": True},
    )
    switch = TuyaRecordingsCameraPauseSwitch(SimpleNamespace(), entry)

    assert switch.available is True
    assert switch.is_on is True
