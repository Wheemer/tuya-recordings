"""
JSON shapes and the accessors that narrow them.

The gateway and the signaling channel both answer with free-form JSON, and the
SDK types its whole surface, so every value that crosses that boundary is read
through one of the accessors here rather than indexed straight out of a
permissive mapping.
"""

from __future__ import annotations

import json

from .exceptions import TuyaIpcP2pProtocolError

type JsonPrimitive = str | int | float | bool | None
type JsonValue = JsonPrimitive | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


def parse_json_object(raw: str | bytes) -> JsonObject:
    """Parse a JSON document that must be an object."""
    try:
        decoded: object = json.loads(raw)
    except ValueError as exception:
        raise TuyaIpcP2pProtocolError(f"Failed to parse JSON: {exception}") from exception
    if not isinstance(decoded, dict):
        raise TuyaIpcP2pProtocolError("Failed to parse JSON: top level is not an object")
    return decoded


def dump_json(value: JsonValue) -> bytes:
    """Serialize a JSON value the way the app does, without whitespace padding."""
    return json.dumps(value, separators=(",", ":")).encode()


def optional_object(source: JsonObject, key: str) -> JsonObject | None:
    """Return a nested object, or None when the key is absent or not an object."""
    value = source.get(key)
    return value if isinstance(value, dict) else None


def require_object(source: JsonObject, key: str) -> JsonObject:
    """Return a nested object, or fail when it is missing."""
    value = optional_object(source, key)
    if value is None:
        raise TuyaIpcP2pProtocolError(f"Failed to read {key}: not an object")
    return value


def optional_str(source: JsonObject, key: str) -> str | None:
    """Return a string field, or None when it is absent or empty."""
    value = source.get(key)
    return value if isinstance(value, str) and value else None


def require_str(source: JsonObject, key: str) -> str:
    """Return a string field, or fail when it is missing."""
    value = optional_str(source, key)
    if value is None:
        raise TuyaIpcP2pProtocolError(f"Failed to read {key}: not a non-empty string")
    return value


def optional_int(source: JsonObject, key: str) -> int | None:
    """Return an integer field, or None when it is absent or not a number."""
    value = source.get(key)
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def object_list(source: JsonObject, key: str) -> list[JsonObject]:
    """Return the objects of a list field, skipping entries of any other shape."""
    value = source.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def str_list(source: JsonObject, key: str) -> list[str]:
    """Return the strings of a list field, skipping entries of any other shape."""
    value = source.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
