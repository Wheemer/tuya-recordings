from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_LOOKBACK_DAYS,
    CONF_MEDIA_SYNC_ENABLED,
    CONF_MEDIA_SYNC_HOURS,
    CONF_MEDIA_STORAGE_PATH,
    CONF_THUMBNAIL_SYNC_ENABLED,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MEDIA_SYNC_ENABLED,
    DEFAULT_MEDIA_SYNC_HOURS,
    DEFAULT_MEDIA_STORAGE_PATH,
    DEFAULT_THUMBNAIL_SYNC_ENABLED,
    DOMAIN,
    LOGGER,
    MANUFACTURER,
    NAME,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Tuya Recordings switches."""
    LOGGER.debug("Setting up Tuya Recordings switches for %s", entry.entry_id)
    async_add_entities(
        [
            TuyaRecordingsMediaSyncSwitch(hass, entry),
            TuyaRecordingsThumbnailSyncSwitch(hass, entry),
        ]
    )


class TuyaRecordingsMediaSyncSwitch(SwitchEntity):
    """Enable Tapo-style background media synchronization."""

    _attr_has_entity_name = True
    _attr_name = "Media Sync"
    _attr_icon = "mdi:sync"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_media_sync"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title or NAME,
            manufacturer=MANUFACTURER,
            model=NAME,
        )

    @property
    def is_on(self) -> bool:
        return bool(self.entry.options.get(CONF_MEDIA_SYNC_ENABLED, self.entry.data.get(CONF_MEDIA_SYNC_ENABLED, DEFAULT_MEDIA_SYNC_ENABLED)))

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "lookback_days": self.entry.options.get(CONF_LOOKBACK_DAYS, DEFAULT_LOOKBACK_DAYS),
            "sync_hours": self.entry.options.get(CONF_MEDIA_SYNC_HOURS, DEFAULT_MEDIA_SYNC_HOURS),
            "storage_path": self.entry.options.get(CONF_MEDIA_STORAGE_PATH, DEFAULT_MEDIA_STORAGE_PATH),
        }

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set_enabled(False)

    async def _async_set_enabled(self, enabled: bool) -> None:
        options = dict(self.entry.options)
        options[CONF_MEDIA_SYNC_ENABLED] = enabled
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        entry_data = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        if isinstance(entry_data, dict) and (client := entry_data.get("client")):
            client.media_sync_enabled = enabled
        self.async_write_ha_state()
        if enabled and self.hass.services.has_service(DOMAIN, "sync_media"):
            self.hass.async_create_task(
                self.hass.services.async_call(
                    DOMAIN,
                    "sync_media",
                    {"entry_id": self.entry.entry_id},
                    blocking=False,
                )
            )


class TuyaRecordingsThumbnailSyncSwitch(SwitchEntity):
    """Enable lightweight background thumbnail previews."""

    _attr_has_entity_name = True
    _attr_name = "Thumbnail Sync"
    _attr_icon = "mdi:image-sync"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_thumbnail_sync"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title or NAME,
            manufacturer=MANUFACTURER,
            model=NAME,
        )

    @property
    def is_on(self) -> bool:
        return bool(self.entry.options.get(CONF_THUMBNAIL_SYNC_ENABLED, self.entry.data.get(CONF_THUMBNAIL_SYNC_ENABLED, DEFAULT_THUMBNAIL_SYNC_ENABLED)))

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "lookback_days": self.entry.options.get(CONF_LOOKBACK_DAYS, DEFAULT_LOOKBACK_DAYS),
            "storage_path": self.entry.options.get(CONF_MEDIA_STORAGE_PATH, DEFAULT_MEDIA_STORAGE_PATH),
            "keeps_full_videos": bool(self.entry.options.get(CONF_MEDIA_SYNC_ENABLED, self.entry.data.get(CONF_MEDIA_SYNC_ENABLED, DEFAULT_MEDIA_SYNC_ENABLED))),
        }

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set_enabled(False)

    async def _async_set_enabled(self, enabled: bool) -> None:
        options = dict(self.entry.options)
        options[CONF_THUMBNAIL_SYNC_ENABLED] = enabled
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        entry_data = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        if isinstance(entry_data, dict) and (client := entry_data.get("client")):
            client.thumbnail_sync_enabled = enabled
        self.async_write_ha_state()
        if enabled and self.hass.services.has_service(DOMAIN, "populate_thumbnails"):
            self.hass.async_create_task(
                self.hass.services.async_call(
                    DOMAIN,
                    "populate_thumbnails",
                    {"entry_id": self.entry.entry_id},
                    blocking=False,
                )
            )
