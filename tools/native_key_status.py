"""Compare saved and current Smart Life device keys without exposing secrets."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from custom_components.tuya_recordings.const import (
    CONF_DEVICE_LOCAL_KEYS,
    CONF_NATIVE_APP_SESSION,
    CONF_REGION,
    DOMAIN,
)
from custom_components.tuya_recordings.lib.native_gateway import NativeAppGatewayClient


def _fingerprint(value: str) -> str:
    if not value:
        return "missing"
    return hashlib.sha256(value.encode()).hexdigest()[:10]


def _load_entry(config_dir: Path) -> dict[str, Any]:
    payload = json.loads(
        (config_dir / ".storage" / "core.config_entries").read_text(encoding="utf-8")
    )
    matches = [
        entry
        for entry in payload.get("data", {}).get("entries", [])
        if entry.get("domain") == DOMAIN
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one Tuya Recordings entry, found {len(matches)}")
    return dict(matches[0].get("data", {}))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("/config"))
    parser.add_argument("--device-id", action="append", required=True)
    args = parser.parse_args()

    entry_data = _load_entry(args.config)
    session = entry_data[CONF_NATIVE_APP_SESSION]
    gateway = NativeAppGatewayClient(
        region=str(entry_data.get(CONF_REGION) or "us"),
        device_fingerprint=session["device_fingerprint"],
    )
    current_keys, _protocol_versions = gateway.device_connection_data(session)
    saved_keys = entry_data.get(CONF_DEVICE_LOCAL_KEYS, {})

    for device_id in args.device_id:
        saved = str(saved_keys.get(device_id) or "")
        current = str(current_keys.get(device_id) or "")
        print(
            json.dumps(
                {
                    "device_id": device_id,
                    "saved_key": _fingerprint(saved),
                    "current_key": _fingerprint(current),
                    "keys_match": bool(saved and current and saved == current),
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
