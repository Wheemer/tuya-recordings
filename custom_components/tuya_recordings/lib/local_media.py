"""Durable local deletion intent and wall-clock retention; no camera access."""

import json
from pathlib import Path
import threading

from .media import CachedClipKey, parse_cached_media_name, safe_segment
from .storage import write_catalog


def local_clip_key(dev_id: str, start: int, end: int) -> CachedClipKey:
    if (not isinstance(dev_id, str) or not dev_id or safe_segment(dev_id) != dev_id
            or type(start) is not int or type(end) is not int or start <= 0 or end <= start):
        raise ValueError("Invalid local recording identifier")
    return CachedClipKey(dev_id, start, end)


class LocalMediaState:
    def __init__(self, root: Path, state_path: Path):
        self.root = root
        self.state_path = state_path
        self.lock = threading.RLock()
        self.known: set[CachedClipKey] = set()
        self.deleted: set[CachedClipKey] = set()
        self.loaded = False

    def _load(self):
        if self.loaded:
            return
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if data.get("version") != 1:
                    raise ValueError("Unknown local media state version")
                known = {local_clip_key(*row) for row in data["known"]}
                deleted = {local_clip_key(*row) for row in data["deleted"]}
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
                # Forgetting a deletion would silently repopulate the disk.
                raise RuntimeError("Cannot safely read local recording deletion history") from error
            self.known, self.deleted = known, deleted
        self.loaded = True

    def _save(self):
        def rows(values):
            return [[key.dev_id, key.start, key.end]
                    for key in sorted(values, key=lambda item: (item.dev_id, item.start, item.end))]
        write_catalog(self.state_path, {"version": 1, "known": rows(self.known), "deleted": rows(self.deleted)})

    def path(self, key: CachedClipKey, thumbnail=False):
        folder = self.root / ("thumbs" if thumbnail else "videos")
        path = folder / f"{key.dev_id}_{key.start}_{key.end}{'.jpg' if thumbnail else '.mp4'}"
        if folder.resolve().parent != self.root.resolve() or path.is_symlink():
            raise ValueError("Local recording path leaves the configured storage folder")
        return path

    def is_deleted(self, key):
        with self.lock:
            self._load()
            if key in self.known and not self.path(key).exists():
                if not (self.root / "videos").is_dir():
                    raise RuntimeError("Local media storage is unavailable; deletion history is unchanged")
                self.deleted.add(key)
                self.known.discard(key)
                self._save()
            return key in self.deleted

    def remember(self, key):
        with self.lock:
            self._load()
            if key not in self.known and self.path(key).is_file():
                self.known.add(key)
                self._save()

    def delete(self, key, *, remember=True, thumbnails=True):
        with self.lock:
            self._load()
            paths = [self.path(key)] + ([self.path(key, True)] if thumbnails else [])
            # Persist intent before deleting anything, including across a crash.
            if remember:
                self.deleted.add(key)
            self.known.discard(key)
            self._save()
            count = 0
            for path in paths:
                if path.exists():
                    path.unlink()
                    count += 1
            return count

    def mark_deleted(self, keys):
        """Commit bulk deletion intent once before unlinking any files."""
        with self.lock:
            self._load()
            keys = set(keys)
            for key in keys:
                self.path(key)
            if keys:
                self.deleted.update(keys)
                self.known.difference_update(keys)
                self._save()

    def restore(self, dev_id=None, start=None, end=None):
        with self.lock:
            self._load()
            exact = local_clip_key(dev_id, start, end) if start is not None or end is not None else None
            selected = {key for key in self.deleted if (dev_id is None or key.dev_id == dev_id)
                        and (exact is None or key == exact)}
            self.deleted.difference_update(selected)
            self._save()
            return len(selected)

    def maintain(self, cutoff, owned_devices, protected, logger):
        with self.lock:
            self._load()
            if self.known and not (self.root / "videos").is_dir():
                return {"storage_unavailable": True, "deleted_videos": 0, "deleted_thumbnails": 0}
            changed = False
            for key in list(self.known):
                if not self.path(key).exists():
                    self.deleted.add(key)
                    self.known.discard(key)
                    changed = True
            result = {"deleted_videos": 0, "deleted_thumbnails": 0, "retention_deferred": 0}
            expired = []
            for thumbnail, suffix, field in ((False, ".mp4", "deleted_videos"), (True, ".jpg", "deleted_thumbnails")):
                folder = self.root / ("thumbs" if thumbnail else "videos")
                if folder.resolve().parent != self.root.resolve():
                    raise ValueError("Local media folder leaves the configured storage folder")
                for path in folder.glob(f"*{suffix}"):
                    key = parse_cached_media_name(path.name, suffix)
                    if key is None or path.is_symlink() or not path.is_file():
                        continue
                    try:
                        local_clip_key(key.dev_id, key.start, key.end)
                    except ValueError:
                        continue
                    if owned_devices is not None and key.dev_id not in owned_devices and key not in self.known:
                        continue
                    if cutoff is not None and key.end < cutoff:
                        if key in protected:
                            result["retention_deferred"] += 1
                            continue
                        # Retention is not manual deletion: raising retention
                        # later may legitimately make this clip eligible again.
                        self.known.discard(key)
                        expired.append((path, field))
                    elif not thumbnail and key not in self.known and key not in self.deleted:
                        self.known.add(key)
                        changed = True
            if changed or expired:
                self._save()
            for path, field in expired:
                try:
                    path.unlink()
                    result[field] += 1
                except OSError as error:
                    logger.warning("Could not expire local recording %s: %s", path.name, error)
            return result
