import asyncio
from types import SimpleNamespace

from custom_components.tuya_recordings.const import CONF_DEVICE_INVENTORY, DOMAIN
from custom_components.tuya_recordings.diagnostics import (
    async_get_config_entry_diagnostics,
)


class FakeEntry:
    entry_id = "entry"
    data = {
        "native_app_session": {"sid": "secret-session"},
        "device_local_keys": {"camera": "secret-local-key"},
        "device_protocol_versions": {"camera": "2.3"},
    }


class FakeHass:
    data = {DOMAIN: {}}


def test_diagnostics_redact_native_credentials():
    result = asyncio.run(
        async_get_config_entry_diagnostics(FakeHass(), FakeEntry())
    )

    assert result["entry"]["native_app_session"] == "**REDACTED**"
    assert result["entry"]["device_local_keys"] == "**REDACTED**"
    assert result["entry"]["device_protocol_versions"] == {"camera": "2.3"}


def test_diagnostics_expose_camera_work_state():
    class Task:
        @staticmethod
        def done():
            return False

    class Client:
        @staticmethod
        def diagnostics():
            return {"ok": True}

    hass = FakeHass()
    hass.data = {
        DOMAIN: {
            "entry": {
                "client": Client(),
                "camera_work_task": Task(),
                "media_sync_schedule": [object()],
                "thumbnail_sync_running": True,
                "thumbnail_sync_pending": True,
                "recording_trigger_unsub": object(),
            }
        }
    }

    result = asyncio.run(async_get_config_entry_diagnostics(hass, FakeEntry()))

    assert result["runtime"]["camera_work_running"] is True
    assert result["runtime"]["media_sync_scheduled"] is True
    assert result["runtime"]["thumbnail_sync_running"] is True
    assert result["runtime"]["thumbnail_sync_pending"] is True
    assert result["runtime"]["recording_trigger_listener"] is True


def test_diagnostics_use_optional_tuya_sd_metadata_for_exact_camera_ids():
    def device(schema, status):
        return SimpleNamespace(status_range=schema, status=status)

    devices = {
        "camera": device({"sd_status": object()}, {"sd_status": 0}),
        "schema-only": device({"sd_status": object()}, {"sd_status": True}),
        "value-only": device({}, {"sd_status": 5}),
        "unrelated": device({"sd_status": object()}, {"sd_status": 1}),
    }
    loaded = SimpleNamespace(runtime_data=SimpleNamespace(manager=SimpleNamespace(device_map=devices)))
    entry = SimpleNamespace(entry_id="entry", data={
        **FakeEntry.data,
        CONF_DEVICE_INVENTORY: dict.fromkeys(("camera", "schema-only", "value-only", "missing"), "Camera"),
    })
    hass = FakeHass()
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: (
        [SimpleNamespace(), loaded, loaded] if domain == "tuya" else []
    ))
    result = asyncio.run(async_get_config_entry_diagnostics(hass, entry))
    assert result["runtime"]["official_tuya_sd_status"] == {
        "matched_cameras": 3,
        "schema_advertises_sd_status": 2,
        "cached_integer_sd_status": 2,
    }
    hass.config_entries.async_entries = lambda _domain: []
    result = asyncio.run(async_get_config_entry_diagnostics(hass, entry))
    assert set(result["runtime"]["official_tuya_sd_status"].values()) == {0}
