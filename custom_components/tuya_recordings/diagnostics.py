from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

TO_REDACT = {
    "client_secret",
    "cookies",
    "device_local_keys",
    "login_result",
    "native_app_session",
    "protect_cookie",
    "protect_csrf",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry):
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "runtime": {
            "configured": entry.entry_id in hass.data.get(DOMAIN, {}),
            "client_loaded": bool(runtime.get("client")),
            "client": runtime["client"].diagnostics() if runtime.get("client") else None,
            "interactive_playback_active": bool(runtime.get("interactive_playback_active")),
            "camera_work_running": bool(runtime.get("camera_work_task") and not runtime["camera_work_task"].done()),
            "media_sync_running": bool(runtime.get("media_sync_running")),
            "media_sync_pending": bool(runtime.get("media_sync_pending")),
            "media_sync_scheduled": bool(runtime.get("media_sync_schedule")),
            "thumbnail_sync_running": bool(runtime.get("thumbnail_sync_running")),
            "thumbnail_sync_pending": bool(runtime.get("thumbnail_sync_pending")),
            "recording_trigger_listener": bool(runtime.get("recording_trigger_unsub")),
        },
    }
