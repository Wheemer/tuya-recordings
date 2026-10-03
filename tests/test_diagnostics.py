import asyncio

from custom_components.tuya_recordings.const import DOMAIN
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
