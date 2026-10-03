"""Print non-sensitive Tuya Recordings catalog/cache status."""

from __future__ import annotations

import glob
import json
import os
from datetime import datetime
from pathlib import Path


def stamp(value: int) -> str:
    return datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d %I:%M:%S %p %Z")


paths = glob.glob("/config/.storage/tuya_recordings/*_recordings.json")
path = Path(max(paths, key=os.path.getmtime))
data = json.loads(path.read_text(encoding="utf-8"))
print(f"catalog={path.name} modified={stamp(int(path.stat().st_mtime))}")
print(f"top_level_keys={sorted(data)}")
cameras = data.get("cameras", [])
if not cameras and isinstance(data.get("index"), dict):
    cameras = data["index"].get("cameras", [])
if not cameras and isinstance(data.get("devices"), dict):
    cameras = [
        {"devId": dev_id, **value}
        for dev_id, value in data["devices"].items()
        if isinstance(value, dict)
    ]
for camera in cameras:
    clips = camera.get("clips", [])
    starts = [int(clip.get("start") or 0) for clip in clips if int(clip.get("start") or 0)]
    thumbs = sum(
        1
        for clip in clips
        if (
            Path("/media/tuya_recordings/thumbs")
            / f"{camera.get('devId')}_{int(clip.get('start') or 0)}_{int(clip.get('end') or 0)}.jpg"
        ).is_file()
    )
    print(
        f"camera={camera.get('name')} dev={camera.get('devId')} clips={len(clips)} "
        f"thumbnails={thumbs} newest={stamp(max(starts)) if starts else 'none'} "
        f"oldest={stamp(min(starts)) if starts else 'none'} "
        f"online={camera.get('online')} catalog_days={len(camera.get('catalogDays') or {})} "
        f"catalog_error_count={camera.get('catalogErrorCount')} "
        f"catalog_retry_after={camera.get('catalogRetryAfter')} "
        f"bridge_error={(camera.get('bridgeStatus') or {}).get('error')}"
    )
