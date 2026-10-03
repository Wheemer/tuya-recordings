from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_CLOUD_ACTIVITY_PAUSED,
    CONF_LOOKBACK_DAYS,
    CONF_MEDIA_SYNC_ENABLED,
    CONF_MEDIA_SYNC_HOURS,
    CONF_MEDIA_STORAGE_PATH,
    DEFAULT_CLOUD_ACTIVITY_PAUSED,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MEDIA_SYNC_ENABLED,
    DEFAULT_MEDIA_SYNC_HOURS,
    DEFAULT_MEDIA_STORAGE_PATH,
    DOMAIN,
    LOGGER,
    MANUFACTURER,
    NAME,
)
from . import async_pause_camera_work, async_resume_camera_work


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Tuya Recordings switches."""
    LOGGER.debug("Setting up Tuya Recordings switches for %s", entry.entry_id)
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.platform == DOMAIN and entity.unique_id == f"{entry.entry_id}_thumbnail_sync":
            registry.async_remove(entity.entity_id)
        if (
            entity.platform == DOMAIN
            and entity.unique_id == f"{entry.entry_id}_cloud_activity_paused"
            and entity.entity_id == f"switch.{DOMAIN}_pause_tuya_camera_cloud_activity"
        ):
            try:
                registry.async_update_entity(
                    entity.entity_id,
                    new_entity_id=f"switch.{DOMAIN}_pause_camera_activity",
                )
            except ValueError:
                LOGGER.debug("Could not rename existing Tuya Recordings pause switch entity")
    async_add_entities(
        [
            TuyaRecordingsCameraPauseSwitch(hass, entry),
            TuyaRecordingsMediaSyncSwitch(hass, entry),
        ]
    )


class TuyaRecordingsCameraPauseSwitch(SwitchEntity):
    """Pause all Tuya camera activity from this integration."""

    _attr_has_entity_name = True
    _attr_name = "Pause camera activity"
    _attr_icon = "mdi:camera-off-outline"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_cloud_activity_paused"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title or NAME,
            manufacturer=MANUFACTURER,
            model=NAME,
        )

    @property
    def is_on(self) -> bool:
        if not self._media_sync_enabled:
            return False
        return bool(self.entry.options.get(CONF_CLOUD_ACTIVITY_PAUSED, self.entry.data.get(CONF_CLOUD_ACTIVITY_PAUSED, DEFAULT_CLOUD_ACTIVITY_PAUSED)))

    @property
    def available(self) -> bool:
        return self._media_sync_enabled

    @property
    def _media_sync_enabled(self) -> bool:
        return bool(
            self.entry.options.get(
                CONF_MEDIA_SYNC_ENABLED,
                self.entry.data.get(CONF_MEDIA_SYNC_ENABLED, DEFAULT_MEDIA_SYNC_ENABLED),
            )
        )

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "effect": "Stops all Tuya Recordings camera sessions, including refreshes, polling, thumbnail sync, live playback, media sync, and uncached downloads.",
            "media_sync_enabled": bool(self.entry.options.get(CONF_MEDIA_SYNC_ENABLED, self.entry.data.get(CONF_MEDIA_SYNC_ENABLED, DEFAULT_MEDIA_SYNC_ENABLED))),
            "thumbnail_sync_enabled": self._media_sync_enabled,
        }

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set_paused(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set_paused(False)

    async def _async_set_paused(self, paused: bool) -> None:
        options = dict(self.entry.options)
        options[CONF_CLOUD_ACTIVITY_PAUSED] = paused
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        entry_data = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        if isinstance(entry_data, dict) and (client := entry_data.get("client")):
            client.cloud_activity_paused = paused
        if paused:
            await async_pause_camera_work(self.hass, self.entry.entry_id)
        else:
            await async_resume_camera_work(self.hass, self.entry.entry_id)
        self.async_write_ha_state()


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
        paused = bool(self.entry.options.get(CONF_CLOUD_ACTIVITY_PAUSED, self.entry.data.get(CONF_CLOUD_ACTIVITY_PAUSED, DEFAULT_CLOUD_ACTIVITY_PAUSED)))
        return {
            "lookback_days": self.entry.options.get(CONF_LOOKBACK_DAYS, DEFAULT_LOOKBACK_DAYS),
            "sync_hours": self.entry.options.get(CONF_MEDIA_SYNC_HOURS, DEFAULT_MEDIA_SYNC_HOURS),
            "storage_path": self.entry.options.get(CONF_MEDIA_STORAGE_PATH, DEFAULT_MEDIA_STORAGE_PATH),
            "camera_activity_paused": paused,
            "effective_state": "paused" if paused else ("enabled" if self.is_on else "disabled"),
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
        client = None
        if isinstance(entry_data, dict) and (client := entry_data.get("client")):
            client.media_sync_enabled = enabled
            client.thumbnail_sync_enabled = enabled
        self.async_write_ha_state()
