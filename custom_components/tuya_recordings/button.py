from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import TuyaRecordingsAuthError
from .const import DOMAIN, LOGGER, MANUFACTURER, NAME, SIGNAL_RECORDINGS_UPDATED


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Tuya Recordings buttons."""
    LOGGER.debug("Setting up Tuya Recordings refresh button for %s", entry.entry_id)
    async_add_entities([TuyaRecordingsRefreshButton(hass, entry)])


class TuyaRecordingsRefreshButton(ButtonEntity):
    """Refresh the cached Tuya recordings index."""

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
        """Refresh the cached recording index."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        if not isinstance(entry_data, dict) or not entry_data.get("client"):
            return

        try:
            await self.hass.async_add_executor_job(entry_data["client"].camera_index, True)
        except TuyaRecordingsAuthError as exc:
            self.entry.async_start_reauth(self.hass)
            raise ConfigEntryAuthFailed("Tuya Recordings session expired") from exc
        async_dispatcher_send(self.hass, SIGNAL_RECORDINGS_UPDATED, self.entry.entry_id)
