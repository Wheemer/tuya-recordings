"""Print non-secret APK-native camera connection capabilities."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from custom_components.tuya_recordings.client import TuyaRecordingsClient
from custom_components.tuya_recordings.const import CONF_CLOUD_ACTIVITY_PAUSED


def _fingerprint(value: str) -> str:
    """Return a comparison-safe identifier without exposing a credential."""
    return hashlib.sha256(value.encode()).hexdigest()[:10] if value else "missing"


def _json_keys(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return sorted(str(key) for key in parsed) if isinstance(parsed, dict) else []


def _ice_shape(servers: list[dict[str, Any]]) -> list[list[str]]:
    return sorted(sorted(str(key) for key in server) for server in servers)


def _load_entry(config_dir: Path) -> tuple[str, dict[str, Any]]:
    payload = json.loads(
        (config_dir / ".storage" / "core.config_entries").read_text(encoding="utf-8")
    )
    matches = [
        entry
        for entry in payload.get("data", {}).get("entries", [])
        if entry.get("domain") == "tuya_recordings"
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one Tuya Recordings entry, found {len(matches)}")
    entry = matches[0]
    data = {**entry.get("data", {}), **entry.get("options", {})}
    data[CONF_CLOUD_ACTIVITY_PAUSED] = False
    return str(entry["entry_id"]), data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("/config"))
    parser.add_argument("--device-id", action="append", required=True)
    args = parser.parse_args()

    entry_id, entry_data = _load_entry(args.config)
    cache_path = (
        args.config / ".storage" / "tuya_recordings" / f"{entry_id}_recordings.json"
    )
    client = TuyaRecordingsClient(entry_data, cache_path=cache_path)
    client.load_cache()
    backend = client._recordings_backend
    provider = backend._provider
    try:
        for device_id in args.device_id:
            config = provider.session_config(device_id).validate()
            print(
                json.dumps(
                    {
                        "device_id": device_id,
                        "p2p_type": config.p2p_type,
                        "preconnect": config.preconnect,
                        "p2p_id_matches_device": config.p2p_id == device_id,
                        "has_moto_id": bool(config.p2p_moto_id),
                        "security_level": config.p2p_security_level,
                        "ice_servers": len(config.p2p_ice_servers),
                        "ice_shape": _ice_shape(config.p2p_ice_servers),
                        "mqtt_protocol": config.mqtt_protocol_version,
                        "event_catalog": config.event_catalog_supported,
                        "local_storage": config.local_storage,
                        "catalog_version": config.playback_catalog_version,
                        "uses_connection_params": config.uses_connection_params,
                        "token_keys": _json_keys(config.token),
                        "config_keys": _json_keys(config.config_json),
                        "ext_keys": sorted(str(key) for key in config.ext_config),
                        "session_id_length": len(config.p2p_session_id),
                        "moto_id_fingerprint": _fingerprint(config.p2p_moto_id),
                        "auth_fingerprint": _fingerprint(config.p2p_auth),
                        "local_key_fingerprint": _fingerprint(config.local_key),
                        "camera_password_fingerprint": _fingerprint(config.camera_password),
                        "config_bytes": len(config.config_json),
                        "skill_bytes": len(config.skill),
                    },
                    sort_keys=True,
                )
            )
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
