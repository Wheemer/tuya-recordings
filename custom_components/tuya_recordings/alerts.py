"""Shared parsing and discovery helpers for Tuya camera notifications."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er

ALARM_MESSAGE = "alarm_message"
INITIATIVE_MESSAGE = "initiative_message"
NOTIFICATION_STATUS_CODES = (ALARM_MESSAGE, INITIATIVE_MESSAGE)
TUYA_DOMAIN = "tuya"
TUYA_HA_SIGNAL_UPDATE_ENTITY = "tuya_entry_update"
EVENT_ENTITY_SUFFIX = "_doorbell_message"


def decode_notification_message(value: str | bytes) -> Mapping[str, Any] | None:
    """Decode a Tuya IPC notification status value."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    text = value.strip()
    if not text:
        return None
    if not text.startswith(("{", "[")):
        try:
            padding = "=" * (-len(text) % 4)
            text = base64.b64decode(text + padding, validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return None
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        return {"message": text} if text.startswith("ipc_") else None
    if isinstance(decoded, Mapping):
        return decoded
    if isinstance(decoded, str) and decoded.startswith("ipc_"):
        return {"message": decoded}
    return None


def notification_event_code(state: str, attributes: Mapping[str, Any]) -> str | None:
    """Return the raw IPC code carried by a camera notification, if present."""
    for value in _candidate_event_values(state, attributes):
        normalized = str(value).strip()
        if normalized.startswith("ipc_"):
            return normalized
    return None


def matching_ipc_alert_code(
    state: str, attributes: Mapping[str, Any], codes: frozenset[str]
) -> str | None:
    """Return a requested camera notification code, if it is present."""
    code = notification_event_code(state, attributes)
    return code if code in codes else None


def notification_is_cleared(attributes: Mapping[str, Any]) -> bool:
    """Return whether the camera explicitly marked this notification cleared."""
    return attributes.get("alarm") is False


def notification_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return useful, non-media metadata from a camera notification."""
    metadata: dict[str, Any] = {}
    for key in ("cmd", "type", "time", "timestamp", "msgCode", "msg_code", "id", "alarm"):
        value = payload.get(key)
        if isinstance(value, (str, int, float, bool)):
            metadata[key] = value
    return metadata


def tuya_camera_event_entities(hass: HomeAssistant) -> list[str]:
    """Return the official Tuya camera notification entities."""
    return sorted(
        state.entity_id
        for state in hass.states.async_all("event")
        if state.entity_id.endswith(EVENT_ENTITY_SUFFIX)
    )


def tuya_notification_source(hass: HomeAssistant, entity_id: str) -> tuple[str | None, Any]:
    """Resolve an official Tuya event entity to its cloud device and manager."""
    entity_entry = er.async_get(hass).async_get(entity_id)
    if entity_entry is None or entity_entry.platform != TUYA_DOMAIN or entity_entry.device_id is None:
        return None, None
    device_entry = dr.async_get(hass).async_get(entity_entry.device_id)
    if device_entry is None:
        return None, None
    tuya_device_id = next(
        (identifier for domain, identifier in device_entry.identifiers if domain == TUYA_DOMAIN),
        None,
    )
    if tuya_device_id is None:
        return None, None
    for tuya_entry in hass.config_entries.async_loaded_entries(TUYA_DOMAIN):
        runtime_data = getattr(tuya_entry, "runtime_data", None)
        manager = getattr(runtime_data, "manager", None)
        if manager is not None and tuya_device_id in manager.device_map:
            return tuya_device_id, manager
    return tuya_device_id, None


def camera_name_from_event_state(state: State | None, entity_id: str) -> str:
    """Return the camera name from its official Tuya event entity."""
    friendly_name = str(state.attributes.get("friendly_name") or "") if state else ""
    if friendly_name.endswith(" Doorbell message"):
        return friendly_name.removesuffix(" Doorbell message")
    object_id = entity_id.split(".", 1)[-1]
    if object_id.endswith(EVENT_ENTITY_SUFFIX):
        object_id = object_id.removesuffix(EVENT_ENTITY_SUFFIX)
    return object_id.replace("_", " ").title()


def safe_unique_fragment(value: str) -> str:
    """Return a registry-safe identifier fragment."""
    return "".join(character if character.isalnum() else "_" for character in value.lower()).strip("_")


def _candidate_event_values(state: str, attributes: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("event_type", "event", "type", "msgCode", "msg_code", "code", "cmd", "message"):
        value = attributes.get(key)
        if isinstance(value, str):
            values.append(value)
    if state not in {"unknown", "unavailable"}:
        values.append(state)
    for value in attributes.values():
        values.extend(_extract_ipc_codes(value))
    return values


def _extract_ipc_codes(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("{", "[")):
            try:
                return _extract_ipc_codes(json.loads(text))
            except (TypeError, ValueError):
                pass
        return [text] if text.startswith("ipc_") else []
    if isinstance(value, Mapping):
        return [code for nested in value.values() for code in _extract_ipc_codes(nested)]
    if isinstance(value, list):
        return [code for nested in value for code in _extract_ipc_codes(nested)]
    return []
