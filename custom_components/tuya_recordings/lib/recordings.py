"""Recording clip normalization and matching helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def normalize_clip(clip: Any) -> dict[str, Any] | None:
    if not isinstance(clip, dict):
        return None

    start = as_epoch_seconds(clip.get("st") or clip.get("startTime"))
    end = as_epoch_seconds(clip.get("ed") or clip.get("endTime"))
    if not start or not end:
        return None

    start_dt = datetime.fromtimestamp(start)
    end_dt = datetime.fromtimestamp(end)
    normalized = {
        "start": start,
        "end": end,
        "date": start_dt.date().isoformat(),
        "title": f"{start_dt:%H:%M:%S} - {end_dt:%H:%M:%S}",
        "raw": {
            key: value
            for key, value in clip.items()
            if key in {"st", "ed", "startTime", "endTime", "type", "eventType"}
            or looks_like_thumbnail_key(key)
        },
    }
    if thumbnail := first_thumbnail_value(clip):
        normalized["thumbnail"] = thumbnail
    return normalized


def looks_like_thumbnail_key(key: Any) -> bool:
    text = str(key).lower()
    return any(fragment in text for fragment in ("thumb", "snapshot", "cover", "poster", "preview", "image", "pic"))


def first_thumbnail_value(clip: dict[str, Any]) -> str:
    preferred = (
        "thumbnail",
        "thumbnailUrl",
        "thumb",
        "thumbUrl",
        "snapshot",
        "snapshotUrl",
        "cover",
        "coverUrl",
        "poster",
        "posterUrl",
        "preview",
        "previewUrl",
        "image",
        "imageUrl",
        "pic",
        "picUrl",
    )
    for key in preferred:
        value = clip.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key, value in clip.items():
        if looks_like_thumbnail_key(key) and isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def as_epoch_seconds(value: Any) -> int:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return 0
    if timestamp > 10_000_000_000:
        timestamp //= 1000
    return timestamp


def merge_cached_clips(new_clips: list[dict[str, Any]], cached_clips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[int, int], dict[str, Any]] = {}
    for clip in cached_clips:
        key = clip_key(clip)
        if key is not None:
            merged[key] = clip
    for clip in new_clips:
        key = clip_key(clip)
        if key is not None:
            merged[key] = clip
    return sorted(merged.values(), key=lambda clip: int(clip.get("start", 0)), reverse=True)


def best_clip_match(clips: list[dict[str, Any]], start: int, end: int) -> dict[str, Any] | None:
    best_clip: dict[str, Any] | None = None
    best_score: int | None = None
    for clip in clips:
        key = clip_key(clip)
        if key is None:
            continue
        clip_start, clip_end = key
        if not clip_start or not clip_end:
            continue
        if clip_start == start and clip_end == end:
            return clip
        if clip_start > end or clip_end < start:
            continue
        score = abs(clip_start - start) + abs(clip_end - end)
        if best_score is None or score < best_score:
            best_clip = clip
            best_score = score
    return best_clip


def clip_key(clip: dict[str, Any]) -> tuple[int, int] | None:
    try:
        return int(clip.get("start", 0)), int(clip.get("end", 0))
    except (TypeError, ValueError):
        return None
