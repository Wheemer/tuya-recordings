from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, LOGGER, MANUFACTURER, NAME, SIGNAL_RECORDINGS_UPDATED


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Tuya Recordings sensors."""
    LOGGER.debug("Setting up Tuya Recordings cached clip-count sensor for %s", entry.entry_id)
    async_add_entities([TuyaRecordingsClipCountSensor(hass, entry)])


class TuyaRecordingsClipCountSensor(SensorEntity):
    """Cached clip count from Tuya Recordings."""

    _attr_has_entity_name = True
    _attr_name = "Cached clips"
    _attr_icon = "mdi:video-box"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_cached_clips"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title or NAME,
            manufacturer=MANUFACTURER,
            model=NAME,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_RECORDINGS_UPDATED,
                self._handle_recordings_updated,
            )
        )
        self.async_write_ha_state()

    def _handle_recordings_updated(self, entry_id: str) -> None:
        if entry_id == self.entry.entry_id:
            self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        entry_data = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        if not isinstance(entry_data, dict) or not entry_data.get("client"):
            return 0
        index = entry_data["client"].cached_camera_index()
        return sum(len(camera.get("clips", [])) for camera in index.get("cameras", []))

    @property
    def extra_state_attributes(self) -> dict:
        entry_data = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        if not isinstance(entry_data, dict) or not entry_data.get("client"):
            return {}
        diagnostics = entry_data["client"].diagnostics()
        index = entry_data["client"].cached_camera_index()
        media_sync_status = diagnostics.get("media_sync_status") or {}
        return {
            "camera_count": len(index.get("cameras", [])),
            "generated_at": index.get("generatedAt"),
            "cached": index.get("cached", False),
            "stale": index.get("stale", False),
            "cache_expires_at": diagnostics.get("cache_expires_at"),
            "refresh_running": diagnostics.get("refresh_running"),
            "thumbnail_sync_running": entry_data.get("thumbnail_sync_running", False),
            "media_sync_running": entry_data.get("media_sync_running", False),
            "media_sync_state": media_sync_status.get("state"),
            "media_sync_current": media_sync_status.get("current"),
            "media_sync_total": media_sync_status.get("total"),
            "media_sync_downloaded": media_sync_status.get("downloaded"),
            "media_sync_skipped": media_sync_status.get("skipped"),
            "media_sync_failed": media_sync_status.get("failed"),
            "media_sync_deleted_videos": media_sync_status.get("deleted_videos"),
            "media_sync_deleted_thumbnails": media_sync_status.get("deleted_thumbnails"),
            "media_sync_last_error": media_sync_status.get("last_error"),
            "media_sync_last_result": media_sync_status.get("last_result"),
            "media_sync_updated_at": media_sync_status.get("updated_at"),
        }
