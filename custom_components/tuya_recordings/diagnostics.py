from __future__ import annotations

from collections.abc import Mapping

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_DEVICE_INVENTORY, DOMAIN

TO_REDACT = {
    "client_secret",
    "cookies",
    "device_local_keys",
    "login_result",
    "native_app_session",
    "protect_cookie",
    "protect_csrf",
}


def _official_tuya_sd_status_evidence(hass: HomeAssistant, inventory: object) -> dict[str, int]:
    """Summarize only already-loaded Tuya metadata for matching camera IDs."""
    camera_ids = set(inventory) if isinstance(inventory, Mapping) else set()
    matched: set[str] = set()
    advertised: set[str] = set()
    reported: set[str] = set()
    if camera_ids:
        for entry in hass.config_entries.async_entries("tuya"):
            manager = getattr(getattr(entry, "runtime_data", None), "manager", None)
            devices = getattr(manager, "device_map", None)
            if not isinstance(devices, Mapping):
                continue
            for camera_id in camera_ids & devices.keys():
                device = devices[camera_id]
                matched.add(camera_id)
                schema = getattr(device, "status_range", None)
                if isinstance(schema, Mapping) and "sd_status" in schema:
                    advertised.add(camera_id)
                status = getattr(device, "status", None)
                if isinstance(status, Mapping) and type(status.get("sd_status")) is int:
                    reported.add(camera_id)
    return {
        "matched_cameras": len(matched),
        "schema_advertises_sd_status": len(advertised),
        "cached_integer_sd_status": len(reported),
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry):
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "runtime": {
            "configured": entry.entry_id in hass.data.get(DOMAIN, {}),
            "client_loaded": bool(runtime.get("client")),
            "client": runtime["client"].diagnostics() if runtime.get("client") else None,
            "official_tuya_sd_status": _official_tuya_sd_status_evidence(
                hass, entry.data.get(CONF_DEVICE_INVENTORY),
            ),
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
