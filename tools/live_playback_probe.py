"""Run one bounded Tuya Recordings playback probe from a Home Assistant host.

This intentionally reads the installed config entry without displaying any
credentials. It is a developer diagnostic, not integration runtime code.
"""

from __future__ import annotations

import argparse
import json
import struct
import threading
import time
from pathlib import Path
from typing import Any

from custom_components.tuya_recordings.client import TuyaRecordingsClient
from custom_components.tuya_recordings.const import CONF_CLOUD_ACTIVITY_PAUSED
from custom_components.tuya_recordings.lib.native_mux import (
    BROWSER_STREAM_AUDIO_G711U,
    BROWSER_STREAM_AUDIO_PCM_S16LE,
    BROWSER_STREAM_END,
    BROWSER_STREAM_MAGIC,
    BROWSER_STREAM_VIDEO,
)

_HEADER = struct.Struct(">BQI")


def _load_entry(config_dir: Path) -> tuple[str, dict[str, Any]]:
    payload = json.loads(
        (config_dir / ".storage" / "core.config_entries").read_text(encoding="utf-8")
    )
    entries = payload.get("data", {}).get("entries", [])
    matches = [entry for entry in entries if entry.get("domain") == "tuya_recordings"]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one Tuya Recordings entry, found {len(matches)}")
    entry = matches[0]
    data = {**entry.get("data", {}), **entry.get("options", {})}
    data[CONF_CLOUD_ACTIVITY_PAUSED] = False
    return str(entry["entry_id"]), data


def _select_clip(index: dict[str, Any], device_id: str) -> tuple[str, int, int]:
    for camera in index.get("cameras", []):
        if str(camera.get("devId")) != device_id:
            continue
        clips = sorted(
            camera.get("clips", []), key=lambda clip: int(clip.get("start") or 0), reverse=True
        )
        for clip in clips:
            start = int(clip.get("start") or 0)
            end = int(clip.get("end") or 0)
            if start > 0 and end > start:
                return str(camera.get("name") or device_id), start, end
    raise RuntimeError("No indexed recording was found for the requested camera")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("/config"))
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    entry_id, entry_data = _load_entry(args.config)
    cache_path = args.config / ".storage" / "tuya_recordings" / f"{entry_id}_recordings.json"
    client = TuyaRecordingsClient(entry_data, cache_path=cache_path)
    client.load_cache()
    name, start, end = _select_clip(client.cached_camera_index(), args.device_id)
    client.set_camera_inventory({args.device_id: name})

    counts = {"magic": 0, "video": 0, "audio": 0, "end": 0, "other": 0}
    sizes = {"video": 0, "audio": 0}
    cancel = threading.Event()
    timer = threading.Timer(args.timeout, cancel.set)
    started = time.monotonic()
    first_video_at: float | None = None
    first_audio_at: float | None = None
    output_handle = args.output.open("wb") if args.output else None

    def receive(chunk: bytes) -> None:
        nonlocal first_video_at, first_audio_at
        if output_handle is not None:
            output_handle.write(chunk)
            output_handle.flush()
        if chunk == BROWSER_STREAM_MAGIC:
            counts["magic"] += 1
            return
        if len(chunk) < _HEADER.size:
            counts["other"] += 1
            return
        kind, _timestamp, size = _HEADER.unpack_from(chunk)
        payload = chunk[_HEADER.size :]
        if len(payload) != size:
            counts["other"] += 1
            return
        if kind == BROWSER_STREAM_VIDEO:
            if first_video_at is None:
                first_video_at = time.monotonic() - started
            counts["video"] += 1
            sizes["video"] += size
        elif kind in {BROWSER_STREAM_AUDIO_PCM_S16LE, BROWSER_STREAM_AUDIO_G711U}:
            if first_audio_at is None:
                first_audio_at = time.monotonic() - started
            counts["audio"] += 1
            sizes["audio"] += size
        elif kind == BROWSER_STREAM_END:
            counts["end"] += 1
        else:
            counts["other"] += 1

    print(f"camera={name} clip={start}-{end} duration={end - start}s")
    timer.start()
    error = ""
    error_exc: BaseException | None = None
    try:
        client.stream_clip(
            args.device_id,
            start,
            end,
            receive,
            cancel_event=cancel,
            log_traceback=False,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic boundary
        error_exc = exc
        error = f"{type(exc).__name__}: {exc}"
    finally:
        timer.cancel()
        client.close()
        if output_handle is not None:
            output_handle.close()

    elapsed = time.monotonic() - started
    print(
        "result "
        f"elapsed={elapsed:.2f}s magic={counts['magic']} video_records={counts['video']} "
        f"video_bytes={sizes['video']} audio_records={counts['audio']} "
        f"audio_bytes={sizes['audio']} end_records={counts['end']} other={counts['other']}"
    )
    print(f"first_video={first_video_at} first_audio={first_audio_at}")
    if error:
        print(f"error={error}")
        cause = error_exc.__cause__ if error_exc is not None else None
        while cause is not None:
            print(f"caused_by={type(cause).__name__}: {cause}")
            cause = cause.__cause__
    if counts["video"] <= 0 or sizes["video"] <= 0:
        return 2
    if counts["audio"] <= 0 or sizes["audio"] <= 0:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
