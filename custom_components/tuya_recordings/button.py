from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CONF_ENTRY_ID, SERVICE_REFRESH
from .const import DOMAIN, LOGGER, MANUFACTURER, NAME


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Tuya Recordings buttons."""
    LOGGER.debug("Setting up Tuya Recordings refresh button for %s", entry.entry_id)
    async_add_entities([TuyaRecordingsRefreshButton(hass, entry)])


class TuyaRecordingsRefreshButton(ButtonEntity):
    """Refresh camera inventory and the Tuya recordings catalog."""

    _attr_has_entity_name = True
    _attr_name = "Refresh recordings"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_refresh_recordings"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title or NAME,
            manufacturer=MANUFACTURER,
            model=NAME,
        )

    async def async_press(self) -> None:
        """Run the same entry-scoped refresh as the manual action."""
        await self.hass.services.async_call(
            DOMAIN, SERVICE_REFRESH, {CONF_ENTRY_ID: self.entry.entry_id}, blocking=True,
        )
