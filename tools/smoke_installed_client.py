from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/config")

from custom_components.tuya_recordings.client import TuyaRecordingsClient

DEV_ID = "eb30218f0d0e205921fnkz"
ENTRY_ID = "01KW32CMPC78NHFTP279ZT6683"


def main() -> int:
    storage = Path("/config/.storage/core.config_entries")
    data = json.loads(storage.read_text(encoding="utf-8"))
    entries = data.get("data", {}).get("entries", [])
    entry = next(
        (
            item
            for item in entries
            if item.get("domain") == "tuya_recordings"
            and (item.get("entry_id") == ENTRY_ID or not ENTRY_ID)
        ),
        None,
    )
    if not entry:
        raise RuntimeError("tuya_recordings config entry not found")
    client = TuyaRecordingsClient(dict(entry.get("data") or {}))
    clips, days_checked = client.sd_recordings(DEV_ID)
    print(json.dumps({"days_checked": days_checked, "clip_count": len(clips), "first_clip": clips[0] if clips else None}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
