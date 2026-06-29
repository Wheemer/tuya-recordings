"""Media cache helpers for Tuya SD-card recordings."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


class LoggerLike(Protocol):
    def debug(self, msg: str, *args: Any) -> None: ...


@dataclass(frozen=True)
class CachedClipKey:
    """Stable cache key for one recording clip."""

    dev_id: str
    start: int
    end: int

    @classmethod
    def from_raw(cls, dev_id: str, start: int, end: int) -> "CachedClipKey":
        return cls(safe_segment(dev_id), int(start), int(end))


@dataclass
class MediaSyncStatus:
    """Small serializable progress object for media sync."""

    state: str = "idle"
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    deleted_videos: int = 0
    deleted_thumbnails: int = 0
    total: int = 0
    current: dict[str, Any] | None = None
    last_error: str | None = None
    last_result: dict[str, Any] | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def update(self, state: str, **updates: Any) -> None:
        for key, value in updates.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                self.extra[key] = value
        self.state = state
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        data = {
            "state": self.state,
            "downloaded": self.downloaded,
            "skipped": self.skipped,
            "failed": self.failed,
            "deleted_videos": self.deleted_videos,
            "deleted_thumbnails": self.deleted_thumbnails,
            "total": self.total,
            "current": self.current,
            "last_error": self.last_error,
            "last_result": self.last_result,
            "updated_at": self.updated_at,
        }
        data.update(self.extra)
        return data


def safe_segment(value: str) -> str:
    return "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in str(value))


def parse_cached_media_name(name: str, extension: str) -> CachedClipKey | None:
    if not name.endswith(extension):
        return None
    stem = name[: -len(extension)]
    try:
        dev_id, start, end = stem.rsplit("_", 2)
        return CachedClipKey(dev_id, int(start), int(end))
    except (TypeError, ValueError):
        return None


def cleanup_cached_media(
    media_storage_path: Path,
    desired_clips: set[CachedClipKey],
    cutoff: int | None,
    logger: LoggerLike,
) -> dict[str, int]:
    deleted_videos = _cleanup_cached_folder(media_storage_path / "videos", ".mp4", desired_clips, cutoff, logger)
    deleted_videos += _cleanup_temp_files(media_storage_path / "videos", "*.tmp.mp4", logger)
    deleted_videos += _cleanup_temp_files(media_storage_path / "videos", "*.mp4.h264.pipe", logger)
    deleted_thumbnails = _cleanup_cached_folder(media_storage_path / "thumbs", ".jpg", desired_clips, cutoff, logger)
    deleted_thumbnails += _cleanup_temp_files(media_storage_path / "thumbs", "*.tmp.jpg", logger)
    return {"deleted_videos": deleted_videos, "deleted_thumbnails": deleted_thumbnails}


def _cleanup_cached_folder(
    folder: Path,
    extension: str,
    desired_clips: set[CachedClipKey],
    cutoff: int | None,
    logger: LoggerLike,
) -> int:
    if not folder.exists():
        return 0
    deleted = 0
    for path in folder.glob(f"*{extension}"):
        parsed = parse_cached_media_name(path.name, extension)
        if parsed is None:
            continue
        if parsed in desired_clips:
            continue
        if cutoff is None or parsed.end < cutoff:
            try:
                path.unlink()
            except OSError as exc:
                logger.debug("Could not delete stale Tuya cached media %s: %s", path, exc)
                continue
            deleted += 1
    return deleted


def _cleanup_temp_files(folder: Path, pattern: str, logger: LoggerLike) -> int:
    if not folder.exists():
        return 0
    deleted = 0
    for path in folder.glob(pattern):
        try:
            path.unlink()
        except OSError as exc:
            logger.debug("Could not delete temporary Tuya cached media %s: %s", path, exc)
            continue
        deleted += 1
    return deleted
