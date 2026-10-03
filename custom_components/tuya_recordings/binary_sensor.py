"""Tuya camera motion and person sensors backed by camera notifications."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .alerts import (
    NOTIFICATION_STATUS_CODES,
    TUYA_HA_SIGNAL_UPDATE_ENTITY,
    camera_name_from_event_state,
    decode_notification_message,
    matching_ipc_alert_code,
    notification_is_cleared,
    notification_metadata,
    safe_unique_fragment,
    tuya_camera_event_entities,
    tuya_notification_source,
)
from .const import DOMAIN, MANUFACTURER, NAME

PERSON_CODES = frozenset({"ipc_human", "ipc_linger", "ipc_passby"})
MOTION_CODES = frozenset(
    {"ipc_car", "ipc_cat", "ipc_human", "ipc_linger", "ipc_motion", "ipc_passby"}
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up detection sensors from the official Tuya event sources."""
    entities: list[TuyaRecordingAlertBinarySensor] = []
    for entity_id in tuya_camera_event_entities(hass):
        camera_name = camera_name_from_event_state(hass.states.get(entity_id), entity_id)
        tuya_device_id, tuya_manager = tuya_notification_source(hass, entity_id)
        entities.extend(
            (
                TuyaRecordingAlertBinarySensor(
                    hass=hass,
                    entry=entry,
                    event_entity_id=entity_id,
                    camera_name=camera_name,
                    key="motion",
                    label="Motion detected",
                    codes=MOTION_CODES,
                    device_class=BinarySensorDeviceClass.MOTION,
                    tuya_device_id=tuya_device_id,
                    tuya_manager=tuya_manager,
                ),
                TuyaRecordingAlertBinarySensor(
                    hass=hass,
                    entry=entry,
                    event_entity_id=entity_id,
                    camera_name=camera_name,
                    key="person",
                    label="Person detected",
                    codes=PERSON_CODES,
                    device_class=BinarySensorDeviceClass.OCCUPANCY,
                    tuya_device_id=tuya_device_id,
                    tuya_manager=tuya_manager,
                ),
            )
        )
    async_add_entities(entities)


class TuyaRecordingAlertBinarySensor(BinarySensorEntity):
    """Expose a camera's classified detection notification as a binary sensor."""

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(
        self,
        *,
        hass: HomeAssistant,
        entry: ConfigEntry,
        event_entity_id: str,
        camera_name: str,
        key: str,
        label: str,
        codes: frozenset[str],
        device_class: BinarySensorDeviceClass,
        tuya_device_id: str | None,
        tuya_manager: Any,
    ) -> None:
        self.hass = hass
        self.event_entity_id = event_entity_id
        self.codes = codes
        self.tuya_device_id = tuya_device_id
        self.tuya_manager = tuya_manager
        self._attr_name = f"{camera_name} {label}"
        self._attr_unique_id = (
            f"{entry.entry_id}_{safe_unique_fragment(event_entity_id)}_{key}_alert"
        )
        self._attr_device_class = device_class
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{safe_unique_fragment(event_entity_id)}")},
            name=camera_name,
            manufacturer=MANUFACTURER,
            model=NAME,
        )
        self._is_on = False
        self._last_event_at: str | None = None
        self._last_event_type: str | None = None
        self._last_event_attributes: dict[str, Any] = {}
        self._last_notification_values: dict[str, str | bytes] = {}

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "source_event_entity_id": self.event_entity_id,
            "source_tuya_device_id": self.tuya_device_id,
            "source_notification_dps": list(NOTIFICATION_STATUS_CODES),
            "alert_codes": sorted(self.codes),
            "last_event_at": self._last_event_at,
            "last_event_type": self._last_event_type,
            "last_event_attributes": self._last_event_attributes,
        }

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.event_entity_id], self._handle_event_state_change
            )
        )
        if self.tuya_device_id is not None and self.tuya_manager is not None:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    f"{TUYA_HA_SIGNAL_UPDATE_ENTITY}_{self.tuya_device_id}",
                    self._handle_tuya_device_update,
                )
            )
        self.async_write_ha_state()

    @callback
    def _handle_event_state_change(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if not isinstance(new_state, State):
            return
        code = matching_ipc_alert_code(new_state.state, new_state.attributes, self.codes)
        if code is not None:
            self._record_notification(code, dict(new_state.attributes))

    @callback
    def _handle_tuya_device_update(
        self,
        updated_status_properties: list[str] | None,
        dp_timestamps: dict[str, int] | None,
    ) -> None:
        """Handle a camera notification from the official Tuya dispatcher."""
        if not updated_status_properties or self.tuya_manager is None or self.tuya_device_id is None:
            return
        device = self.tuya_manager.device_map.get(self.tuya_device_id)
        if device is None:
            return
        for status_code in NOTIFICATION_STATUS_CODES:
            if status_code not in updated_status_properties:
                continue
            raw_value = device.status.get(status_code)
            if not isinstance(raw_value, (str, bytes)) or raw_value in ("", b""):
                continue
            if raw_value == self._last_notification_values.get(status_code):
                continue
            self._last_notification_values[status_code] = raw_value
            payload = decode_notification_message(raw_value)
            if payload is None:
                continue
            code = matching_ipc_alert_code("unknown", payload, self.codes)
            if code is None:
                continue
            attributes = notification_metadata(payload)
            attributes["notification_dp"] = status_code
            if dp_timestamps and status_code in dp_timestamps:
                attributes["dp_timestamp"] = dp_timestamps[status_code]
            self._record_notification(code, attributes)
            return

    @callback
    def _record_notification(self, code: str, attributes: Mapping[str, Any]) -> None:
        """Reflect only explicit camera notifications on Home Assistant's loop."""
        self._is_on = not notification_is_cleared(attributes)
        self._last_event_at = dt_util.utcnow().isoformat()
        self._last_event_type = code
        self._last_event_attributes = dict(attributes)
        self.async_write_ha_state()
