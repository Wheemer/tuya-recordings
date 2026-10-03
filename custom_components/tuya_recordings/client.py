from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .const import (
    CONF_CLOUD_ACTIVITY_PAUSED,
    CONF_LOOKBACK_DAYS,
    CONF_MEDIA_STORAGE_PATH,
    CONF_MEDIA_SYNC_ENABLED,
    CONF_MEDIA_SYNC_HOURS,
    CONF_MEDIA_VIEW_RECORDINGS_ORDER,
    CONF_REGION,
    CONF_THUMBNAIL_SYNC_ENABLED,
    DEFAULT_CLOUD_ACTIVITY_PAUSED,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MEDIA_STORAGE_PATH,
    DEFAULT_MEDIA_SYNC_ENABLED,
    DEFAULT_REGION,
    DEFAULT_THUMBNAIL_SYNC_ENABLED,
    LOGGER,
)
from .lib import (
    CachedClipKey,
    MediaSyncStatus,
    cleanup_cached_media,
    extract_thumbnail_from_mp4,
    safe_segment,
)
from .lib import (
    as_epoch_seconds as _as_epoch_seconds,
)
from .lib import (
    best_clip_match as _best_clip_match,
)
from .lib import (
    finalize_mp4_for_browser as _finalize_mp4_for_browser,
)
from .lib import (
    merge_cached_clips as _merge_cached_clips,
)
from .lib.catalog import (
    catalog_queries,
    next_catalog_retry_after,
)
from .lib.commands import (
    CameraWorkBusy,
    CameraWorkCancelled,
    Cancellation,
    Deadline,
    camera_work_scope,
)
from .lib.native_backend import RecordingBackend, RecordingBackendAuthError
from .lib.native_playback import native_playback_request
from .lib.native_session import NativePlaybackRequest
from .lib.native_setup import build_recordings_backend
from .lib.storage import write_catalog

CACHE_TTL = timedelta(minutes=10)
STALE_CACHE_TTL = timedelta(hours=12)
RECORDING_SCAN_MAX_DAYS = 31
RECORDING_SCAN_EMPTY_DAY_STOP = 7
RECENT_RECORDING_SCAN_DAYS = 2
THUMBNAIL_FAILURE_COOLDOWN = 6 * 60 * 60
THUMBNAIL_CAMERA_FAILURE_COOLDOWN = 5 * 60
THUMBNAIL_FAILURE_CACHE_LIMIT = 1024
MEDIA_FAILURE_CACHE_VERSION = 2
MEDIA_FAILURE_COOLDOWN = 15 * 60
MEDIA_MAX_ATTEMPTS_PER_CAMERA = 1
MEDIA_SYNC_INTER_ATTEMPT_DELAY = 0.5
MEDIA_SYNC_MIN_CLIP_AGE = 2 * 60
MEDIA_SYNC_CAMERA_PASS_TIMEOUT = 2 * 60
THUMBNAIL_AUTOFILL_LIMIT = 10
THUMBNAIL_AUTOFILL_COOLDOWN = 45
INDEX_SOURCE = "tuya_apk_native_recordings"
CATALOG_DAYS_PER_SESSION = 2
KNOWN_CAMERA_CATEGORIES = {"sp", "dghsxj"}
KNOWN_CAMERA_CATEGORY_HINTS = ("camera", "ipc", "cam", "doorbell")
CAMERA_NAME_HINTS = ("camera", "doorbell", "lobby", "ipc", "ip camera")

TuyaRecordingsAuthError = RecordingBackendAuthError
TuyaRecordingsApiError = RuntimeError


class TuyaRecordingsClient:
    def __init__(
        self,
        entry_data: dict[str, Any],
        cache_path: Path | None = None,
        media_storage_path: Path | None = None,
        recordings_backend: RecordingBackend | None = None,
        hass: Any | None = None,
    ) -> None:
        self._closed = False
        self._camera_inventory: dict[str, str] | None = None
        self._lifecycle_lock = threading.RLock()
        self._cloud_pause_event = threading.Event()
        self._background_pause_event = threading.Event()
        self._media_cancel_event = threading.Event()
        self._thumbnail_cancel_event = threading.Event()
        self._media_sync_lock = threading.Lock()
        self.region = str(entry_data.get(CONF_REGION) or DEFAULT_REGION)
        self._media_storage_path_override = media_storage_path
        self.update_options(entry_data)
        self._cache_path = cache_path
        self._cache_mtime_ns: int | None = None
        self._camera_index_cache: dict[str, Any] | None = None
        self._camera_index_cache_until = datetime.min.replace(tzinfo=timezone.utc)
        self._refresh_lock = threading.Lock()
        self._browse_devices_until = 0.0
        self._browse_days: dict[tuple[str, str], float] = {}
        self._browse_failures: dict[tuple[str, str], str] = {}
        self._browse_discovery_error = ""
        self._thumbnail_autofill_lock = threading.Lock()
        self._thumbnail_create_lock = threading.Lock()
        self._thumbnail_autofill_after = 0.0
        self._thumbnail_failures: dict[tuple[str, int, int], float] = {}
        self._thumbnail_camera_failures: dict[str, float] = {}
        self._media_failures: dict[tuple[str, int, int], float] = {}
        self._media_sync_status = MediaSyncStatus()
        self._backend = recordings_backend or build_recordings_backend(entry_data)

    @property
    def _recordings_backend(self) -> RecordingBackend:
        """Current recordings backend.

        This is injectable for tests; production setup selects only the
        in-process APK-native direct-ICE playback backend.
        """
        return self._backend

    @property
    def _native_clip_playback_available(self) -> bool:
        return bool(getattr(self._recordings_backend, "clip_playback_available", False))

    @property
    def clip_playback_available(self) -> bool:
        """Return whether uncached clip playback can be attempted."""
        return self._native_clip_playback_available

    @property
    def cloud_activity_paused(self) -> bool:
        return self._cloud_pause_event.is_set()

    @cloud_activity_paused.setter
    def cloud_activity_paused(self, paused: bool) -> None:
        with self._lifecycle_lock:
            if paused or self._closed:
                self._cloud_pause_event.set()
            elif self._cloud_pause_event.is_set():
                # Keep cancellation latched for already queued/active operations.
                self._cloud_pause_event = threading.Event()

    def close(self) -> None:
        """Latch cancellation for an unloaded entry without changing its options."""
        with self._lifecycle_lock:
            self._closed = True
            self.cloud_activity_paused = True
        close_backend = getattr(self._recordings_backend, "close", None)
        if callable(close_backend):
            close_backend()

    @property
    def background_work_paused(self) -> bool:
        """Return whether camera-facing background work is suspended for playback."""
        return self._background_pause_event.is_set()

    def pause_background_work(self) -> None:
        """Cancel the current generation of background camera work."""
        with self._lifecycle_lock:
            self._background_pause_event.set()

    def resume_background_work(self) -> None:
        """Allow a new generation of background work after playback finishes."""
        with self._lifecycle_lock:
            if self._background_pause_event.is_set() and not self._closed:
                # Running jobs retain the set event they captured. New jobs get
                # a clean generation and cannot revive cancelled camera work.
                self._background_pause_event = threading.Event()

    def background_work_cancellation(self, *events: object) -> Cancellation:
        """Capture playback suspension with any mode-specific cancellation."""
        with self._lifecycle_lock:
            return Cancellation(self._cloud_pause_event, self._background_pause_event, *events)

    @property
    def media_sync_enabled(self) -> bool:
        return not self._media_cancel_event.is_set()

    @media_sync_enabled.setter
    def media_sync_enabled(self, enabled: bool) -> None:
        with self._lifecycle_lock:
            if not enabled or self._closed:
                self._media_cancel_event.set()
            elif self._media_cancel_event.is_set():
                self._media_cancel_event = threading.Event()

    @property
    def thumbnail_sync_enabled(self) -> bool:
        return not self._thumbnail_cancel_event.is_set()

    @thumbnail_sync_enabled.setter
    def thumbnail_sync_enabled(self, enabled: bool) -> None:
        with self._lifecycle_lock:
            if not enabled or self._closed:
                self._thumbnail_cancel_event.set()
            elif self._thumbnail_cancel_event.is_set():
                self._thumbnail_cancel_event = threading.Event()

    def update_options(self, entry_data: dict[str, Any]) -> None:
        self.lookback_days = int(entry_data.get(CONF_LOOKBACK_DAYS, DEFAULT_LOOKBACK_DAYS) or 0)
        self.cloud_activity_paused = bool(entry_data.get(CONF_CLOUD_ACTIVITY_PAUSED, DEFAULT_CLOUD_ACTIVITY_PAUSED))
        self.media_sync_enabled = bool(entry_data.get(CONF_MEDIA_SYNC_ENABLED, DEFAULT_MEDIA_SYNC_ENABLED))
        self.thumbnail_sync_enabled = bool(entry_data.get(CONF_THUMBNAIL_SYNC_ENABLED, DEFAULT_THUMBNAIL_SYNC_ENABLED))
        self.media_sync_hours = int(entry_data.get(CONF_MEDIA_SYNC_HOURS, 0) or 0)
        self.media_view_recordings_order = entry_data.get(CONF_MEDIA_VIEW_RECORDINGS_ORDER, "Descending")
        configured_media_path = entry_data.get(CONF_MEDIA_STORAGE_PATH, DEFAULT_MEDIA_STORAGE_PATH)
        self.media_storage_path = self._media_storage_path_override or Path(str(configured_media_path))

    def camera_index(self, force_refresh: bool = False) -> dict[str, Any]:
        if self.cloud_activity_paused:
            return self._paused_index()
        if self.background_work_paused:
            return self._busy_cache()
        if not self._refresh_lock.acquire(blocking=False):
            return self._busy_cache()
        try:
            with camera_work_scope(self.background_work_cancellation()):
                return self._camera_index_locked(force_refresh)
        finally:
            self._refresh_lock.release()

    def _camera_index_locked(self, force_refresh: bool = False) -> dict[str, Any]:
        cancellation = self._cloud_pause_event
        now = datetime.now(timezone.utc)
        if not force_refresh and self._camera_index_cache and now < self._camera_index_cache_until:
            cached = dict(self._camera_index_cache)
            cached["cached"] = True
            cached["cacheExpiresAt"] = self._camera_index_cache_until.isoformat()
            return cached
        try:
            devices = self._camera_devices()
        except (CameraWorkBusy, CameraWorkCancelled, TuyaRecordingsAuthError):
            raise
        except Exception as exc:
            if self._camera_index_cache:
                return self._stale_cache(now, str(exc))
            raise
        cameras: list[dict[str, Any]] = []
        previous_by_dev_id = {
            str(camera.get("devId")): camera
            for camera in (self._camera_index_cache or {}).get("cameras", [])
            if camera.get("devId")
        }
        generated_at = now.isoformat()
        camera_devices = self._camera_candidates(devices)
        if not camera_devices:
            camera_devices = self._camera_candidates_fallback(devices, previous_by_dev_id)
        if not camera_devices:
            LOGGER.warning("Tuya Recordings found no camera candidates; no devices will be probed")
        for device in camera_devices:
            if cancellation.is_set():
                raise CameraWorkCancelled("Recording catalog refresh cancelled")
            dev_id = self._device_id(device)
            if not dev_id:
                continue
            clips: list[dict[str, Any]] = []
            error = ""
            days_checked: list[str] = []
            if device.get("online") is False:
                error = "Camera is offline; using cached recordings."
            else:
                try:
                    clips, days_checked = self.sd_recordings(dev_id)
                except (CameraWorkBusy, CameraWorkCancelled, TuyaRecordingsAuthError):
                    raise
                except TuyaRecordingsApiError as exc:
                    error = str(exc)
                except Exception as exc:
                    error = str(exc)
            if error and not clips and (previous := previous_by_dev_id.get(str(dev_id))):
                clips = list(previous.get("clips") or [])
            elif previous := previous_by_dev_id.get(str(dev_id)):
                clips = _merge_cached_clips(clips, previous.get("clips") or [])
            cameras.append(
                {
                    "devId": dev_id,
                    "name": device.get("name") or device.get("deviceName") or dev_id,
                    "category": device.get("category"),
                    "productId": device.get("productId") or device.get("product_id") or device.get("productKey"),
                    "online": device.get("online"),
                    "clips": clips,
                    "bridgeStatus": {
                        "error": error,
                        "listedCount": len(clips),
                        "daysChecked": days_checked,
                    },
                }
            )
        index = {
            "source": INDEX_SOURCE,
            "generatedAt": generated_at,
            "recordingScanMaxDays": RECORDING_SCAN_MAX_DAYS,
            "recordingScanEmptyDayStop": RECORDING_SCAN_EMPTY_DAY_STOP,
            "cameras": cameras,
        }
        if cancellation.is_set():
            raise CameraWorkCancelled("Recording catalog refresh cancelled")
        self._store_cache(index, CACHE_TTL)
        LOGGER.info("Refreshed Tuya recordings cache for %s camera(s)", len(cameras))
        return index

    def _busy_cache(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        if self._camera_index_cache:
            return self._stale_cache(now, "A Tuya recordings refresh is already running.")
        return {
            "source": INDEX_SOURCE,
            "generatedAt": now.isoformat(),
            "warning": "A Tuya recordings refresh is already running.",
            "cameras": [],
        }

    def cached_camera_index(self) -> dict[str, Any]:
        self._load_cache_if_changed()
        now = datetime.now(timezone.utc)
        if self._camera_index_cache:
            cached = dict(self._camera_index_cache)
            cached["cached"] = True
            cached["cacheExpiresAt"] = self._camera_index_cache_until.isoformat()
            if now >= self._camera_index_cache_until:
                cached["stale"] = True
            return cached
        return {
            "source": INDEX_SOURCE,
            "generatedAt": now.isoformat(),
            "warning": "No cached recordings yet. Run the Tuya Recordings refresh_recordings service.",
            "cameras": [],
        }

    def _paused_index(self) -> dict[str, Any]:
        cached = self.cached_camera_index()
        cached["cloudPaused"] = True
        cached["warning"] = "Tuya Recordings camera activity is paused; using cached recordings only."
        return cached

    def clear_cache(self) -> None:
        if not self._refresh_lock.acquire(blocking=False):
            LOGGER.info("Skipping Tuya recordings cache clear because a refresh is running")
            return
        try:
            self._camera_index_cache = None
            self._camera_index_cache_until = datetime.min.replace(tzinfo=timezone.utc)
            self._cache_mtime_ns = None
            if self._cache_path is not None:
                try:
                    self._cache_path.unlink(missing_ok=True)
                except OSError:
                    pass
        finally:
            self._refresh_lock.release()

    def browse_recordings(self, dev_id: str = "", day: date | None = None) -> dict[str, Any]:
        """Read a camera list or one day on demand, without a disk catalog scan."""
        if self.cloud_activity_paused:
            return self._paused_index()
        if self.background_work_paused:
            return self._busy_cache()
        with camera_work_scope(self.background_work_cancellation()):
            return self._browse_recordings(dev_id, day)

    def browse_refresh_due(self, dev_id: str = "", day: date | None = None) -> bool:
        """Return whether an on-demand browse may contact a camera now."""
        if self.cloud_activity_paused or self.background_work_paused:
            return False
        now = time.monotonic()
        if not dev_id:
            return now >= self._browse_devices_until
        if day is None:
            return False
        return now >= self._browse_days.get((dev_id, day.isoformat()), 0)

    def _browse_recordings(self, dev_id: str, day: date | None) -> dict[str, Any]:
        """Run one cancellable on-demand catalog operation."""
        if not self._refresh_lock.acquire(blocking=False):
            return self._busy_cache()
        try:
            now = time.monotonic()
            index = self._camera_index_cache or {"source": INDEX_SOURCE, "cameras": []}
            discovery_refreshed = False
            if now >= self._browse_devices_until:
                # Cache failed discovery briefly too; a UI refresh is not a retry loop.
                self._browse_devices_until = now + 30
                try:
                    devices = self._camera_devices()
                except Exception as exc:
                    self._browse_discovery_error = str(exc)
                    raise
                self._browse_discovery_error = ""
                previous = {camera["devId"]: camera for camera in index.get("cameras", [])}
                candidates = self._camera_candidates(devices)
                if not candidates:
                    candidates = self._camera_candidates_fallback(devices, previous)
                index = {"source": INDEX_SOURCE, "cameras": [
                    {**previous.get(self._device_id(device), {}),
                     "devId": self._device_id(device),
                     "name": device.get("name") or device.get("deviceName") or self._device_id(device),
                     "online": bool(device.get("online")),
                     "clips": list(previous.get(self._device_id(device), {}).get("clips", []))}
                    for device in candidates if self._device_id(device)
                ]}
                self._browse_devices_until = now + 300
                self._camera_index_cache = index
                discovery_refreshed = True
            if self._browse_discovery_error:
                raise RuntimeError(self._browse_discovery_error)
            day_refreshed = False
            if dev_id and day is not None:
                camera = next((item for item in index["cameras"] if item["devId"] == dev_id), None)
                if camera is None:
                    raise ValueError("Unknown camera")
                key = (dev_id, day.isoformat())
                if now < self._browse_days.get(key, 0) and key in self._browse_failures:
                    raise RuntimeError(self._browse_failures[key])
                if camera.get("online") and now >= self._browse_days.get(key, 0):
                    self._browse_days[key] = now + 30
                    if len(self._browse_days) > 64:
                        oldest = next(iter(self._browse_days))
                        self._browse_days.pop(oldest)
                        self._browse_failures.pop(oldest, None)
                    try:
                        clips = self._recordings_for_day(dev_id, day)
                    except Exception as exc:
                        self._browse_failures[key] = str(exc)
                        raise
                    self._browse_failures.pop(key, None)
                    previous_clips = list(camera.get("clips", []))
                    previous_day_clips = [
                        clip
                        for clip in previous_clips
                        if clip.get("date") == day.isoformat()
                    ]
                    if clips or not previous_day_clips:
                        camera["clips"] = [
                            clip
                            for clip in previous_clips
                            if clip.get("date") != day.isoformat()
                        ] + clips
                    refreshed_at = datetime.now(timezone.utc).isoformat()
                    catalog_days = dict(camera.get("catalogDays") or {})
                    catalog_days[day.isoformat()] = refreshed_at
                    camera["catalogDays"] = catalog_days
                    day_refreshed = True
                    self._browse_days[key] = time.monotonic() + 60
            if discovery_refreshed or day_refreshed:
                index["generatedAt"] = datetime.now(timezone.utc).isoformat()
            if day_refreshed:
                self._store_cache(index, CACHE_TTL)
            else:
                self._camera_index_cache = index
            return index
        finally:
            self._refresh_lock.release()

    def clear_video_cache(self) -> dict[str, Any]:
        """Delete cached video files while preserving the index and thumbnails."""
        video_folder = Path(self.media_storage_path) / "videos"
        result = {
            "path": str(video_folder),
            "deleted_videos": 0,
            "deleted_temp_files": 0,
            "deleted_bytes": 0,
        }
        if not video_folder.exists() or not video_folder.is_dir():
            return result

        patterns = ("*.mp4", "*.tmp.mp4")
        candidates: set[Path] = set()
        for pattern in patterns:
            for path in video_folder.glob(pattern):
                if path.is_file():
                    candidates.add(path)

        for path in candidates:
            is_video = path.suffix == ".mp4" and not path.name.endswith(".tmp.mp4")
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError as exc:
                LOGGER.warning("Failed to delete cached Tuya recording file %s: %s", path, exc)
                continue
            result["deleted_bytes"] += size
            if is_video:
                result["deleted_videos"] += 1
            else:
                result["deleted_temp_files"] += 1
        return result

    def diagnostics(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "camera_inventory_source": "official_tuya_registry",
            "region": self.region,
            "camera_inventory_count": len(self._camera_inventory or {}),
            "lookback_days": self.lookback_days,
            "cloud_activity_paused": self.cloud_activity_paused,
            "recording_scan_max_days": RECORDING_SCAN_MAX_DAYS,
            "recording_scan_empty_day_stop": RECORDING_SCAN_EMPTY_DAY_STOP,
            "media_sync_enabled": self.media_sync_enabled,
            "thumbnail_sync_enabled": self.thumbnail_sync_enabled,
            "media_sync_hours": self.media_sync_hours,
            "media_storage_path": str(self.media_storage_path),
            "has_cache": self._camera_index_cache is not None,
            "cache_expires_at": self._camera_index_cache_until.isoformat(),
            "cache_fresh": self._camera_index_cache is not None and now < self._camera_index_cache_until,
            "cache_path_configured": self._cache_path is not None,
            "refresh_running": self._refresh_lock.locked(),
            "media_sync_status": self._media_sync_status.to_dict(),
            "recordings_backend": {
                "name": getattr(self._recordings_backend, "name", "unknown"),
                "apk_native": bool(getattr(self._recordings_backend, "apk_native", False)),
                "available": getattr(self._recordings_backend, "name", "") != "apk-native-not-configured",
                "reason": str(getattr(self._recordings_backend, "reason", "")),
            },
        }

    def clip_path(self, dev_id: str, start: int, end: int) -> Path:
        return Path(self.media_storage_path) / "videos" / f"{_safe_segment(dev_id)}_{start}_{end}.mp4"

    def clip_cached(self, dev_id: str, start: int, end: int) -> bool:
        return self.clip_ready(dev_id, start, end)

    def clip_ready(self, dev_id: str, start: int, end: int) -> bool:
        return _mp4_ready(self.clip_path(dev_id, start, end))

    def thumbnail_path(self, dev_id: str, start: int, end: int) -> Path:
        return Path(self.media_storage_path) / "thumbs" / f"{_safe_segment(dev_id)}_{start}_{end}.jpg"

    def ensure_thumbnail(self, dev_id: str, start: int, end: int) -> Path | None:
        """Render a JPEG from a cached recording without opening a camera session."""
        thumbnail_path = self.thumbnail_path(dev_id, start, end)
        if thumbnail_path.exists() and thumbnail_path.stat().st_size > 0:
            return thumbnail_path
        if not self.clip_ready(dev_id, start, end):
            return None
        return self.create_thumbnail(dev_id, start, end)

    def _direct_thumbnail_available(self, dev_id: str) -> bool:
        check = getattr(self._recordings_backend, "direct_thumbnail_available", None)
        return bool(callable(check) and check(dev_id))

    def _indexed_clip(
        self, dev_id: str, start: int, end: int
    ) -> dict[str, Any] | None:
        """Find one clip in the current normal recording catalog."""
        for camera in (self.cached_camera_index().get("cameras") or []):
            if str(camera.get("devId") or "") != dev_id:
                continue
            for clip in camera.get("clips") or []:
                if (
                    int(clip.get("start") or 0) == int(start)
                    and int(clip.get("end") or 0) == int(end)
                ):
                    return clip
        return None

    def sd_recordings(self, dev_id: str) -> tuple[list[dict[str, Any]], list[str]]:
        cancellation = self.background_work_cancellation()
        self._raise_if_cloud_paused()
        if not self._native_clip_playback_available:
            raise RuntimeError("Smart Life APK-native recording catalog is not available")
        today = date.today()
        all_clips: list[dict[str, Any]] = []
        days_checked: list[str] = []
        empty_days = 0
        scan_days = self.lookback_days if self.lookback_days > 0 else RECORDING_SCAN_MAX_DAYS
        offset = 0
        while offset < scan_days:
            if cancellation.is_set():
                raise CameraWorkCancelled("Recording catalog scan cancelled")
            batch_size = min(CATALOG_DAYS_PER_SESSION, scan_days - offset)
            if self.lookback_days <= 0:
                batch_size = min(batch_size, RECORDING_SCAN_EMPTY_DAY_STOP - empty_days)
            days = [today - timedelta(days=offset + index) for index in range(batch_size)]
            queried = self._recordings_backend.recordings_for_days(
                dev_id, {}, {}, days, cancel_event=cancellation,
            )
            if not queried:
                raise RuntimeError("Recording catalog batch returned no days")
            for day, day_clips in queried:
                days_checked.append(day.isoformat())
                empty_days = 0 if day_clips else empty_days + 1
                all_clips.extend(day_clips)
            offset += len(queried)
            if self.lookback_days <= 0 and empty_days >= RECORDING_SCAN_EMPTY_DAY_STOP:
                break

        all_clips.sort(key=lambda clip: int(clip.get("start", 0)), reverse=True)
        return all_clips, days_checked

    def refresh_recent_recordings(self) -> dict[str, Any]:
        """Refresh the cached index by scanning only the newest recording days."""
        if self.cloud_activity_paused:
            return self._paused_index()
        if self.background_work_paused:
            return self._busy_cache()
        if not self._camera_index_cache:
            return self.camera_index(True)
        if not self._refresh_lock.acquire(blocking=False):
            return self._busy_cache()
        try:
            return self._refresh_recent_recordings_locked()
        finally:
            self._refresh_lock.release()

    def thumbnail_work_cancellation(self, require_media_sync: bool = False) -> Cancellation:
        """Capture the enabling switch before submitting work to HA's executor."""
        with self._lifecycle_lock:
            mode = self._media_cancel_event if require_media_sync or not self.thumbnail_sync_enabled else self._thumbnail_cancel_event
            return Cancellation(self._cloud_pause_event, self._background_pause_event, mode)

    def sync_thumbnails(
        self,
        limit: int,
        cancellation: Cancellation,
        refresh_catalog: bool = True,
    ) -> dict[str, Any]:
        """Render thumbnails from already cached clips without touching cameras."""
        limit = max(1, int(limit or 1))
        with camera_work_scope(cancellation) as active:
            if active.is_set():
                raise CameraWorkCancelled("Thumbnail sync cancelled before local thumbnail work")
            return self.populate_thumbnails(
                limit,
                ignore_catalog_backoff=True,
            )

    def reset_camera_work_backoff(self) -> None:
        """Allow one explicit resume attempt without stale failure delays."""
        with self._refresh_lock:
            self._thumbnail_failures.clear()
            self._thumbnail_camera_failures.clear()
            self._thumbnail_autofill_after = 0.0
            index = self._camera_index_cache
            if not isinstance(index, dict):
                return
            changed = False
            for camera in index.get("cameras", []):
                if not isinstance(camera, dict):
                    continue
                for key in ("catalogErrorAt", "catalogErrorCount", "catalogRetryAfter"):
                    if key in camera:
                        camera.pop(key, None)
                        changed = True
                status = camera.get("bridgeStatus")
                if isinstance(status, dict) and (status.get("error") or status.get("retryAfter")):
                    camera["bridgeStatus"] = {
                        key: value
                        for key, value in status.items()
                        if key not in {"error", "retryAfter"}
                    }
                    changed = True
            if changed:
                self._store_cache(index, CACHE_TTL)

    def refresh_background_catalog(self, limit: int = 2) -> dict[str, Any]:
        """Persist a small catalog pass before the background image worker runs."""
        cancellation = self.background_work_cancellation()
        self._raise_if_cloud_paused()
        if not self._refresh_lock.acquire(blocking=False):
            raise CameraWorkBusy("Recording catalog refresh is already active")
        try:
            with camera_work_scope(cancellation) as active:
                now = datetime.now(timezone.utc)
                today = date.today()
                devices = self._camera_devices()
                previous = {str(camera.get("devId")): camera for camera in (self._camera_index_cache or {}).get("cameras", [])}
                cameras = []
                for device in self._camera_candidates(devices) or self._camera_candidates_fallback(devices, previous):
                    dev_id = self._device_id(device)
                    if not dev_id:
                        continue
                    old = previous.get(dev_id, {})
                    cameras.append({**old, "devId": dev_id, "name": device.get("name") or device.get("deviceName") or dev_id,
                                    "online": device.get("online"), "clips": list(old.get("clips") or [])})
                days = self.lookback_days if self.lookback_days > 0 else RECORDING_SCAN_MAX_DAYS
                index = {"source": INDEX_SOURCE, "generatedAt": now.isoformat(), "cameras": cameras}
                by_id = {camera["devId"]: camera for camera in cameras}
                for dev_id, day in catalog_queries(cameras, today=today, now=now, days=days, limit=limit):
                    if active.is_set():
                        raise CameraWorkCancelled("Recording catalog pass cancelled")
                    camera = by_id[dev_id]
                    try:
                        clips = self._recordings_for_day(dev_id, day)
                        if active.is_set():
                            raise CameraWorkCancelled("Recording catalog pass cancelled")
                        camera["clips"] = _merge_cached_clips(clips, camera["clips"])
                        saved_days = camera.get("catalogDays")
                        scanned = dict(saved_days) if isinstance(saved_days, dict) else {}
                        scanned[day.isoformat()] = now.isoformat()
                        cutoff = (today - timedelta(days=days - 1)).isoformat()
                        camera["catalogDays"] = {key: value for key, value in scanned.items() if isinstance(key, str) and cutoff <= key <= today.isoformat()}
                        camera.pop("catalogErrorAt", None)
                        camera.pop("catalogErrorCount", None)
                        camera.pop("catalogRetryAfter", None)
                        camera["bridgeStatus"] = {"error": "", "listedCount": len(camera["clips"]), "daysChecked": sorted(camera["catalogDays"], reverse=True)}
                    except (CameraWorkBusy, CameraWorkCancelled, TuyaRecordingsAuthError):
                        raise
                    except Exception as error:
                        failure_count, retry_after = next_catalog_retry_after(camera, now)
                        camera["catalogErrorAt"] = now.isoformat()
                        camera["catalogErrorCount"] = failure_count
                        camera["catalogRetryAfter"] = retry_after.isoformat()
                        camera["bridgeStatus"] = {
                            "error": str(error),
                            "listedCount": len(camera["clips"]),
                            "retryAfter": camera["catalogRetryAfter"],
                        }
                        self._store_cache(index, CACHE_TTL)
                        continue
                    self._store_cache(index, CACHE_TTL)
                if active.is_set():
                    raise CameraWorkCancelled("Recording catalog pass cancelled")
                self._store_cache(index, CACHE_TTL)
                return index
        finally:
            self._refresh_lock.release()

    def _refresh_recent_recordings_locked(self) -> dict[str, Any]:
        cancellation = self.background_work_cancellation()
        now = datetime.now(timezone.utc)
        try:
            devices = self._camera_devices()
        except (CameraWorkBusy, CameraWorkCancelled, TuyaRecordingsAuthError):
            raise
        except Exception as exc:
            return self._stale_cache(now, str(exc))

        previous_by_dev_id = {
            str(camera.get("devId")): camera
            for camera in (self._camera_index_cache or {}).get("cameras", [])
            if camera.get("devId")
        }
        cameras: list[dict[str, Any]] = []
        today = date.today()
        recent_days = [today - timedelta(days=offset) for offset in range(RECENT_RECORDING_SCAN_DAYS)]
        generated_at = now.isoformat()

        camera_devices = self._camera_candidates(devices)
        if not camera_devices:
            camera_devices = self._camera_candidates_fallback(devices, previous_by_dev_id)
        if not camera_devices:
            LOGGER.warning("Tuya Recordings found no camera candidates for recent refresh; no devices will be probed")
        for device in camera_devices:
            if cancellation.is_set():
                raise CameraWorkCancelled("Recording catalog refresh cancelled")
            dev_id = self._device_id(device)
            if not dev_id:
                continue
            previous = previous_by_dev_id.get(dev_id)
            previous_clips = list(previous.get("clips") or []) if previous else []
            clips: list[dict[str, Any]] = []
            error = ""
            days_checked: list[str] = []
            if device.get("online") is False:
                error = "Camera is offline; using cached recordings."
                clips = previous_clips
            else:
                try:
                    if not self._native_clip_playback_available:
                        raise RuntimeError("Smart Life APK-native recording catalog is not available")
                    remaining_days = list(recent_days)
                    while remaining_days:
                        queried = self._recordings_backend.recordings_for_days(
                            dev_id, {}, {}, remaining_days, cancel_event=cancellation,
                        )
                        if not queried:
                            raise RuntimeError("Recording catalog batch returned no days")
                        for day, day_clips in queried:
                            days_checked.append(day.isoformat())
                            clips.extend(day_clips)
                        remaining_days = remaining_days[len(queried):]
                    clips = _merge_cached_clips(clips, previous_clips)
                except (CameraWorkCancelled, CameraWorkBusy, TuyaRecordingsAuthError):
                    raise
                except TuyaRecordingsApiError as exc:
                    error = str(exc)
                    clips = previous_clips
                except Exception as exc:
                    error = str(exc)
                    clips = previous_clips
            cameras.append(
                {
                    "devId": dev_id,
                    "name": device.get("name") or device.get("deviceName") or dev_id,
                    "category": device.get("category"),
                    "productId": device.get("productId") or device.get("product_id") or device.get("productKey"),
                    "online": device.get("online"),
                    "clips": clips,
                    "bridgeStatus": {
                        "error": error,
                        "listedCount": len(clips),
                        "daysChecked": days_checked,
                        "recentRefresh": True,
                    },
                }
            )

        index = {
            "source": INDEX_SOURCE,
            "generatedAt": generated_at,
            "recordingScanMaxDays": RECORDING_SCAN_MAX_DAYS,
            "recordingScanEmptyDayStop": RECORDING_SCAN_EMPTY_DAY_STOP,
            "recentRecordingScanDays": RECENT_RECORDING_SCAN_DAYS,
            "cameras": cameras,
        }
        if cancellation.is_set():
            raise CameraWorkCancelled("Recording catalog refresh cancelled")
        self._store_cache(index, CACHE_TTL)
        LOGGER.info("Refreshed recent Tuya recordings cache for %s camera(s)", len(cameras))
        return index

    def download_clip(self, dev_id: str, start: int, end: int, output_path: Path, *,
                      verify_clip: bool = True, log_traceback: bool = True) -> Path:
        with camera_work_scope(self._cloud_pause_event) as cancellation:
            return self._download_clip(dev_id, start, end, output_path, verify_clip=verify_clip,
                                       log_traceback=log_traceback, cancellation=cancellation)

    def _download_clip(
        self,
        dev_id: str,
        start: int,
        end: int,
        output_path: Path,
        *,
        verify_clip: bool = True,
        log_traceback: bool = True,
        cancellation: Cancellation,
    ) -> Path:
        output_path = Path(output_path)
        if _mp4_ready(output_path):
            try:
                self.ensure_thumbnail(dev_id, start, end)
            except Exception as exc:
                LOGGER.debug("Could not create cached Tuya recording thumbnail for %s %s-%s: %s", dev_id, start, end, exc)
            LOGGER.info("Using cached Tuya recording for %s %s-%s at %s", dev_id, start, end, output_path)
            return output_path
        self._raise_if_cloud_paused()
        if output_path.exists():
            LOGGER.warning("Removing invalid cached Tuya recording for %s %s-%s at %s", dev_id, start, end, output_path)
            output_path.unlink(missing_ok=True)
        if not self._native_clip_playback_available:
            raise RuntimeError("Smart Life APK-native playback is not available")

        playback_metadata = self._clip_playback_metadata(dev_id, start, end)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_output_path = output_path.with_suffix(".tmp.mp4")
        output_path.unlink(missing_ok=True)
        temp_output_path.unlink(missing_ok=True)

        LOGGER.info("Downloading APK-native Tuya recording for %s %s-%s to %s", dev_id, start, end, output_path)
        try:
            if cancellation.is_set():
                raise CameraWorkCancelled("Recording download cancelled")
            self._recordings_backend.receive_clip(
                dev_id,
                {},
                {},
                int(start),
                int(end),
                temp_output_path,
                verify_clip=verify_clip,
                cancel_event=cancellation,
                **playback_metadata,
            )
            if cancellation.is_set():
                raise CameraWorkCancelled("Recording download cancelled")
            _finalize_mp4_for_browser(temp_output_path, output_path)
            temp_output_path.unlink(missing_ok=True)
        except (CameraWorkCancelled, CameraWorkBusy, TuyaRecordingsAuthError):
            self._cleanup_failed_download(temp_output_path, output_path)
            raise
        except Exception as exc:
            if cancellation.is_set():
                self._cleanup_failed_download(temp_output_path, output_path)
                raise CameraWorkCancelled("Recording download cancelled") from exc
            self._log_download_failure(dev_id, start, end, temp_output_path, output_path, log_traceback)
            raise
        try:
            self.ensure_thumbnail(dev_id, start, end)
        except Exception as exc:
            LOGGER.debug("Could not create Tuya recording thumbnail for %s %s-%s: %s", dev_id, start, end, exc)
        LOGGER.info(
            "Downloaded APK-native Tuya recording for %s %s-%s; mp4_bytes=%s",
            dev_id,
            start,
            end,
            output_path.stat().st_size,
        )
        return output_path

    def stream_clip(
        self,
        dev_id: str,
        start: int,
        end: int,
        chunk_callback: Callable[[bytes], None],
        *,
        play_time: int | None = None,
        cancel_event: threading.Event | None = None,
        log_traceback: bool = True,
    ) -> None:
        with camera_work_scope(Cancellation(self._cloud_pause_event, cancel_event)) as cancellation:
            self._stream_clip(
                dev_id,
                start,
                end,
                chunk_callback,
                play_time=play_time,
                log_traceback=log_traceback,
                cancellation=cancellation,
            )

    def _stream_clip(
        self,
        dev_id: str,
        start: int,
        end: int,
        chunk_callback: Callable[[bytes], None],
        *,
        play_time: int | None,
        log_traceback: bool,
        cancellation: Cancellation,
    ) -> None:
        self._raise_if_cloud_paused()
        if not self._native_clip_playback_available:
            raise RuntimeError("Smart Life APK-native playback is not available")
        playback_metadata = self._clip_playback_metadata(dev_id, start, end)
        if play_time is not None:
            playback_metadata["play_time"] = play_time
        LOGGER.info("Streaming APK-native Tuya recording for %s %s-%s", dev_id, start, end)
        try:
            if cancellation.is_set():
                raise CameraWorkCancelled("Recording stream cancelled")
            stream = getattr(self._recordings_backend, "stream_clip", None)
            if not callable(stream):
                raise RuntimeError("Smart Life APK-native streaming playback is not available")
            stream(
                dev_id,
                {},
                {},
                int(start),
                int(end),
                chunk_callback,
                cancel_event=cancellation,
                **playback_metadata,
            )
        except (CameraWorkCancelled, CameraWorkBusy, TuyaRecordingsAuthError):
            raise
        except Exception:
            message = "Failed to stream APK-native Tuya recording for %s %s-%s"
            if log_traceback:
                LOGGER.exception(message, dev_id, start, end)
            else:
                LOGGER.warning(message, dev_id, start, end)
            raise

    def timeline_request(
        self,
        dev_id: str,
        start: int,
        end: int,
        play_time: int,
    ) -> NativePlaybackRequest:
        """Build a validated APK playback request for one catalog range."""
        metadata = self._clip_playback_metadata(dev_id, start, end)
        return native_playback_request(
            int(start),
            int(end),
            play_time=int(play_time),
            **metadata,
        )

    def stream_timeline(
        self,
        dev_id: str,
        request: NativePlaybackRequest,
        commands: queue.Queue[tuple[str, NativePlaybackRequest | None]],
        chunk_callback: Callable[[bytes], None],
        *,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Run one persistent APK-style SD-card timeline session."""
        with camera_work_scope(
            Cancellation(self._cloud_pause_event, cancel_event)
        ) as cancellation:
            self._raise_if_cloud_paused()
            stream = getattr(self._recordings_backend, "stream_timeline", None)
            if not callable(stream):
                raise RuntimeError("Smart Life timeline playback is not available")
            stream(
                dev_id,
                {},
                {},
                request,
                commands,
                chunk_callback,
                cancel_event=cancellation,
            )

    def _clip_playback_metadata(self, dev_id: str, start: int, end: int) -> dict[str, Any]:
        clip = self._find_cached_clip(dev_id, start, end)
        raw = clip.get("raw") if isinstance(clip, dict) else {}
        if not isinstance(raw, dict):
            raw = {}
        encrypted = _truthy(raw.get("encrypt")) or bool(
            raw.get("uuid")
            and (raw.get("encryptMD5") or raw.get("encrypt_md5"))
        )
        metadata: dict[str, Any] = {"encrypted": encrypted}
        encryption_uuid = raw.get("uuid")
        if isinstance(encryption_uuid, str) and encryption_uuid.strip():
            metadata["encryption_uuid"] = encryption_uuid.strip()
        if isinstance(clip, dict) and isinstance(clip.get("date"), str):
            try:
                metadata["catalog_day"] = date.fromisoformat(clip["date"])
            except ValueError:
                pass
        # Smart Life constructs play-mode JSON from the selected TimePieceBean,
        # not from an optional nested field in the catalog response.
        metadata["fragments_json"] = _playback_fragments_json(start, end)
        return metadata

    def _find_cached_clip(self, dev_id: str, start: int, end: int) -> dict[str, Any] | None:
        index = self.cached_camera_index()
        for camera in index.get("cameras", []):
            if str(camera.get("devId") or "") != str(dev_id):
                continue
            clips = camera.get("clips")
            if isinstance(clips, list):
                return _best_clip_match(clips, int(start), int(end))
        return None

    @staticmethod
    def _cleanup_failed_download(
        temp_output_path: Path,
        output_path: Path,
    ) -> None:
        temp_output_path.unlink(missing_ok=True)
        if output_path.exists() and output_path.stat().st_size <= 0:
            output_path.unlink(missing_ok=True)

    def _log_download_failure(
        self,
        dev_id: str,
        start: int,
        end: int,
        temp_output_path: Path,
        output_path: Path,
        log_traceback: bool,
    ) -> None:
        output_size = temp_output_path.stat().st_size if temp_output_path.exists() else output_path.stat().st_size if output_path.exists() else 0
        message = "Failed to transfer APK-native Tuya recording for %s %s-%s; output_bytes=%s"
        if log_traceback:
            LOGGER.exception(message, dev_id, start, end, output_size)
        else:
            LOGGER.warning(message, dev_id, start, end, output_size)
        self._cleanup_failed_download(temp_output_path, output_path)

    def media_work_cancellation(self) -> Cancellation:
        with self._lifecycle_lock:
            return Cancellation(self._cloud_pause_event, self._background_pause_event, self._media_cancel_event)

    def sync_recordings(self, cancellation: Cancellation | None = None) -> dict[str, Any]:
        if not self._media_sync_lock.acquire(blocking=False):
            return {"enabled": self.media_sync_enabled, "running": True}
        deadline = Deadline(MEDIA_SYNC_CAMERA_PASS_TIMEOUT)
        try:
            with camera_work_scope(Cancellation(cancellation, self.media_work_cancellation(), deadline)) as cancellation:
                return self._sync_recordings(cancellation, deadline)
        finally:
            self._media_sync_lock.release()

    def _sync_recordings(self, cancellation: Cancellation, deadline: Deadline) -> dict[str, Any]:
        result = {"enabled": self.media_sync_enabled, "downloaded": 0, "skipped": 0, "failed": 0,
                  "deleted_videos": 0, "deleted_thumbnails": 0, "recovered_partials": 0, "stalled_cameras": []}
        if self.cloud_activity_paused:
            result.update(enabled=False, paused=True)
            self._set_media_sync_status("paused", last_result=result)
            return result
        if not self.media_sync_enabled:
            self._set_media_sync_status("disabled", last_result=result)
            return result

        def check_cancelled() -> None:
            if cancellation.is_set():
                raise CameraWorkCancelled("Media sync cancelled")

        current_device = None
        failures_changed = False
        state = "idle"
        try:
            check_cancelled()
            result["recovered_partials"] = self._recover_interrupted_media_sync()
            index = self.refresh_recent_recordings()
            check_cancelled()
            clips = sorted(
                [(str(camera["devId"]), clip) for camera in index.get("cameras", []) if camera.get("devId")
                 for clip in camera.get("clips", [])
                 if int(clip.get("start") or 0) > 0 and int(clip.get("end") or 0) > int(clip.get("start") or 0)],
                key=lambda item: int(item[1]["start"]), reverse=True,
            )
            cutoff = max((int(clip["end"]) for _, clip in clips), default=0) - self.media_sync_hours * 3600 if self.media_sync_hours > 0 else None
            desired_clips = {
                CachedClipKey.from_raw(dev_id, int(clip["start"]), int(clip["end"]))
                for dev_id, clip in clips if cutoff is None or int(clip["end"]) >= cutoff
            }
            self._set_media_sync_status("running", downloaded=0, skipped=0, failed=0,
                                        total=len(desired_clips), current=None, last_error=None)
            newest_sync_end = int(time.time()) - MEDIA_SYNC_MIN_CLIP_AGE
            attempts: dict[str, int] = {}
            for position, (dev_id, clip) in enumerate(clips, start=1):
                check_cancelled()
                current_device = dev_id
                start, end = int(clip["start"]), int(clip["end"])
                key = (dev_id, start, end)
                if (cutoff is not None and end < cutoff) or end > newest_sync_end:
                    result["skipped"] += 1
                    continue
                output_path = self.clip_path(dev_id, start, end)
                if _mp4_ready(output_path):
                    self.ensure_thumbnail(dev_id, start, end)
                    failures_changed |= self._media_failures.pop(key, None) is not None
                    result["skipped"] += 1
                    continue
                failed_at = self._media_failures.get(key)
                if attempts.get(dev_id, 0) >= MEDIA_MAX_ATTEMPTS_PER_CAMERA or (
                    failed_at is not None and time.time() - failed_at < MEDIA_FAILURE_COOLDOWN
                ):
                    result["skipped"] += 1
                    continue
                if attempts and MEDIA_SYNC_INTER_ATTEMPT_DELAY > 0:
                    time.sleep(MEDIA_SYNC_INTER_ATTEMPT_DELAY)
                check_cancelled()
                attempts[dev_id] = attempts.get(dev_id, 0) + 1
                self._set_media_sync_status("running", **{key: result[key] for key in ("downloaded", "skipped", "failed")},
                                           current={"dev_id": dev_id, "start": start, "end": end,
                                                    "position": position, "total": len(desired_clips)})
                try:
                    self.download_clip(dev_id, start, end, output_path, verify_clip=False, log_traceback=False)
                    check_cancelled()
                except (CameraWorkCancelled, CameraWorkBusy, TuyaRecordingsAuthError):
                    raise
                except Exception as exc:
                    check_cancelled()
                    self._media_failures[key] = time.time()
                    failures_changed = True
                    result["failed"] += 1
                    result["skipped"] += len(clips) - position
                    self._set_media_sync_status("idle", last_error=str(exc))
                    LOGGER.warning("Media Sync stopped after recording failure for %s %s-%s: %s", dev_id, start, end, exc)
                    break
                self._media_failures.pop(key, None)
                failures_changed = True
                result["downloaded"] += 1
            check_cancelled()
            if desired_clips and not result["failed"]:
                result.update(self.cleanup_cached_media(desired_clips, cutoff))
        except CameraWorkCancelled:
            result["cancelled"] = True
            if deadline.is_set():
                state = "stalled"
                result["timed_out"] = True
                result["stalled_cameras"] = [current_device] if current_device else []
            else:
                state = "paused" if self.cloud_activity_paused else ("disabled" if not self.media_sync_enabled else "cancelled")
        except (CameraWorkBusy, TuyaRecordingsAuthError):
            self._set_media_sync_status("idle", current=None)
            raise
        finally:
            if failures_changed:
                self._store_media_failures()

        self._set_media_sync_status(state, downloaded=result["downloaded"], skipped=result["skipped"],
                                    failed=result["failed"], current=None,
                                    deleted_videos=result["deleted_videos"], deleted_thumbnails=result["deleted_thumbnails"],
                                    recovered_partials=result["recovered_partials"],
                                    stalled_cameras=result["stalled_cameras"], last_result=result)
        return result

    @staticmethod
    def _round_robin_clips(clips_by_camera: dict[str, list[dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
        work: list[tuple[str, dict[str, Any]]] = []
        remaining = {dev_id: list(clips) for dev_id, clips in clips_by_camera.items()}
        while remaining:
            for dev_id in list(remaining):
                clips = remaining[dev_id]
                if not clips:
                    remaining.pop(dev_id, None)
                    continue
                work.append((dev_id, clips.pop(0)))
        return work

    def cleanup_cached_media(self, desired_clips: set[CachedClipKey], cutoff: int | None) -> dict[str, int]:
        return cleanup_cached_media(Path(self.media_storage_path), desired_clips, cutoff, LOGGER)

    def _recover_interrupted_media_sync(self) -> int:
        video_folder = Path(self.media_storage_path) / "videos"
        if not video_folder.exists():
            return 0
        recovered = 0
        for temp_path in video_folder.glob("*.tmp.mp4"):
            if not _mp4_ready(temp_path):
                continue
            output_name = temp_path.name[: -len(".tmp.mp4")] + ".mp4"
            output_path = temp_path.with_name(output_name)
            try:
                temp_path.replace(output_path)
            except OSError as exc:
                LOGGER.debug("Could not recover interrupted Tuya recording %s: %s", temp_path, exc)
                continue
            try:
                dev_id, start, end = output_path.stem.rsplit("_", 2)
                CachedClipKey.from_raw(dev_id, int(start), int(end))
            except (TypeError, ValueError):
                LOGGER.debug("Recovered Tuya recording has an unrecognized cache name: %s", output_path)
                recovered += 1
                continue
            recovered += 1
            LOGGER.info("Recovered interrupted Tuya recording cache file %s", output_path)
        return recovered

    def _set_media_sync_status(self, state: str, **updates: Any) -> None:
        self._media_sync_status.update(state, **updates)

    def populate_thumbnails(
        self,
        limit: int = 3,
        *,
        ignore_catalog_backoff: bool = False,
    ) -> dict[str, Any]:
        index = self.cached_camera_index()
        candidates = [(camera["devId"], clip)
                      for camera in index.get("cameras", []) if camera.get("devId")
                      for clip in camera.get("clips", [])]
        return self._populate_thumbnail_candidates(
            candidates,
            limit,
            ignore_catalog_backoff=ignore_catalog_backoff,
        )

    def _thumbnail_failure_is_recent(self, key: tuple[str, int, int]) -> bool:
        failed_at = self._thumbnail_failures.get(key)
        if failed_at is None:
            return False
        if time.monotonic() - failed_at < THUMBNAIL_FAILURE_COOLDOWN:
            return True
        self._thumbnail_failures.pop(key, None)
        return False

    def _thumbnail_camera_failure_is_recent(self, dev_id: str) -> bool:
        failed_at = self._thumbnail_camera_failures.get(dev_id)
        if failed_at is None:
            return False
        if time.monotonic() - failed_at < THUMBNAIL_CAMERA_FAILURE_COOLDOWN:
            return True
        self._thumbnail_camera_failures.pop(dev_id, None)
        return False

    def populate_thumbnails_for_clips(
        self,
        dev_id: str,
        clips: list[dict[str, Any]],
        limit: int = THUMBNAIL_AUTOFILL_LIMIT,
    ) -> dict[str, Any]:
        return self._populate_thumbnail_candidates([(dev_id, clip) for clip in clips], limit, autofill=True)

    def _populate_thumbnail_candidates(
        self,
        candidates,
        limit: int,
        *,
        autofill: bool = False,
        ignore_catalog_backoff: bool = False,
    ) -> dict[str, Any]:
        with camera_work_scope(self.background_work_cancellation(
            self._thumbnail_cancel_event if self.thumbnail_sync_enabled else None
        )) as cancellation:
            return self._populate_thumbnail_candidates_inner(
                candidates,
                limit,
                autofill,
                cancellation,
                ignore_catalog_backoff,
            )

    def _populate_thumbnail_candidates_inner(
        self,
        candidates,
        limit,
        autofill,
        cancellation,
        ignore_catalog_backoff,
    ) -> dict[str, Any]:
        result = {"created": 0, "skipped": 0, "failed": 0, "checked": 0, "limit": limit}
        if self.cloud_activity_paused:
            return {**result, "paused": True}
        if not self._thumbnail_autofill_lock.acquire(blocking=False):
            return {**result, "running": True}
        try:
            now = time.monotonic()
            if autofill:
                if now < self._thumbnail_autofill_after:
                    return {**result, "throttled": True}
                self._thumbnail_autofill_after = now + THUMBNAIL_AUTOFILL_COOLDOWN
            max_checks = max(0, int(limit or 0))
            for dev_id, clip in sorted(candidates, key=lambda item: int(item[1].get("start") or 0), reverse=True):
                if cancellation.is_set() or self.cloud_activity_paused or (max_checks and result["checked"] >= max_checks):
                    break
                start = int(clip.get("start") or 0)
                end = int(clip.get("end") or 0)
                if not start or end <= start:
                    result["skipped"] += 1
                    continue
                key = (dev_id, start, end)
                thumbnail_path = self.thumbnail_path(dev_id, start, end)
                if thumbnail_path.exists() and thumbnail_path.stat().st_size > 0:
                    self._thumbnail_failures.pop(key, None)
                    result["skipped"] += 1
                    continue
                if self._thumbnail_failure_is_recent(key):
                    result["skipped"] += 1
                    continue
                local_source_ready = self.clip_ready(dev_id, start, end)
                if not local_source_ready:
                    result["skipped"] += 1
                    continue
                result["checked"] += 1
                try:
                    if cancellation.is_set():
                        break
                    if self.media_sync_enabled:
                        if not self.create_thumbnail(
                            dev_id, start, end, cancel_event=cancellation
                        ):
                            raise ValueError("Thumbnail request produced no image")
                        result["created"] += 1
                    else:
                        result["skipped"] += 1
                    self._thumbnail_failures.pop(key, None)
                except (CameraWorkBusy, CameraWorkCancelled, TuyaRecordingsAuthError):
                    raise
                except Exception as exc:
                    if cancellation.is_set():
                        break
                    LOGGER.debug("Could not render local Tuya recording thumbnail for %s %s-%s: %s", dev_id, start, end, exc)
                    self._thumbnail_failures.pop(key, None)
                    if len(self._thumbnail_failures) >= THUMBNAIL_FAILURE_CACHE_LIMIT:
                        self._thumbnail_failures.pop(next(iter(self._thumbnail_failures)))
                    self._thumbnail_failures[key] = time.monotonic()
                    result["failed"] += 1
            if cancellation.is_set() or self.cloud_activity_paused:
                result["paused"] = True
            return result
        finally:
            self._thumbnail_autofill_lock.release()

    def create_thumbnail(
        self,
        dev_id: str,
        start: int,
        end: int,
        *,
        cancel_event: Cancellation | threading.Event | None = None,
    ) -> Path | None:
        """Obtain a recording thumbnail through the safest available native path."""
        with self._thumbnail_create_lock:
            return self._create_thumbnail_locked(
                dev_id, start, end, cancel_event=cancel_event
            )

    def _create_thumbnail_locked(
        self,
        dev_id: str,
        start: int,
        end: int,
        *,
        cancel_event: Cancellation | threading.Event | None = None,
    ) -> Path | None:
        """Create one JPEG from a local MP4 after concurrent callers collapse."""
        thumbnail_path = self.thumbnail_path(dev_id, start, end)
        if thumbnail_path.exists() and thumbnail_path.stat().st_size > 0:
            return thumbnail_path
        if int(end) <= int(start):
            return None
        clip_path = self.clip_path(dev_id, start, end)
        if not _mp4_ready(clip_path):
            return None
        thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            extract_thumbnail_from_mp4(clip_path, thumbnail_path)
            return thumbnail_path
        except BaseException:
            thumbnail_path.unlink(missing_ok=True)
            raise

    def _recordings_for_day(self, dev_id: str, day: date) -> list[dict[str, Any]]:
        return self._recordings_backend.recordings_for_day(dev_id, {}, {}, day)

    def validate_session(self) -> dict[str, Any]:
        return {
            "source": "official_tuya_registry",
            "devices": len(self._camera_inventory or {}),
        }

    def load_cache(self) -> None:
        if self._cache_path is None or not self._cache_path.exists():
            return
        try:
            stat = self._cache_path.stat()
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
            index = payload.get("index")
            expires_at = _parse_datetime(payload.get("expiresAt"))
            media_failures = payload.get("mediaFailures") if payload.get("mediaFailureVersion") == MEDIA_FAILURE_CACHE_VERSION else None
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(index, dict):
            return
        self._camera_index_cache = index
        self._camera_index_cache_until = expires_at or datetime.now(timezone.utc)
        self._cache_mtime_ns = stat.st_mtime_ns
        if isinstance(media_failures, dict):
            self._media_failures = _parse_media_failures(media_failures)

    def _store_cache(self, index: dict[str, Any], ttl: timedelta) -> None:
        self._camera_index_cache = index
        self._camera_index_cache_until = datetime.now(timezone.utc) + ttl
        if self._cache_path is None:
            return
        payload = {
            "expiresAt": self._camera_index_cache_until.isoformat(),
            "index": index,
            "mediaFailureVersion": MEDIA_FAILURE_CACHE_VERSION,
            "mediaFailures": _serialize_media_failures(self._media_failures),
        }
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            write_catalog(self._cache_path, payload)
            self._cache_mtime_ns = self._cache_path.stat().st_mtime_ns
        except OSError:
            pass

    def _store_media_failures(self) -> None:
        if self._cache_path is None:
            return
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8")) if self._cache_path.exists() else {}
            if not isinstance(payload, dict):
                payload = {}
            payload["mediaFailureVersion"] = MEDIA_FAILURE_CACHE_VERSION
            payload["mediaFailures"] = _serialize_media_failures(self._media_failures)
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            write_catalog(self._cache_path, payload)
            self._cache_mtime_ns = self._cache_path.stat().st_mtime_ns
        except (OSError, ValueError, TypeError):
            pass

    def _load_cache_if_changed(self) -> None:
        if self._cache_path is None or not self._cache_path.exists():
            return
        try:
            mtime_ns = self._cache_path.stat().st_mtime_ns
        except OSError:
            return
        if self._cache_mtime_ns != mtime_ns:
            self.load_cache()

    def _stale_cache(self, now: datetime, reason: str) -> dict[str, Any]:
        if not self._camera_index_cache:
            return {"source": INDEX_SOURCE, "generatedAt": now.isoformat(), "warning": reason, "cameras": []}
        stale = dict(self._camera_index_cache)
        stale["cached"] = True
        stale["stale"] = True
        stale["warning"] = reason
        self._camera_index_cache = stale
        self._camera_index_cache_until = max(self._camera_index_cache_until, now + STALE_CACHE_TTL)
        stale["cacheExpiresAt"] = self._camera_index_cache_until.isoformat()
        if self._cache_path is not None:
            try:
                self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                write_catalog(self._cache_path, {
                    "expiresAt": self._camera_index_cache_until.isoformat(),
                    "index": stale,
                    "mediaFailureVersion": MEDIA_FAILURE_CACHE_VERSION,
                    "mediaFailures": _serialize_media_failures(self._media_failures),
                })
                self._cache_mtime_ns = self._cache_path.stat().st_mtime_ns
            except OSError:
                pass
        return stale

    def set_camera_inventory(self, cameras: dict[str, str]) -> None:
        """Replace the HA registry snapshot without accessing HA from workers."""
        snapshot = dict(cameras)
        if snapshot != self._camera_inventory:
            self._camera_inventory = snapshot
            self._camera_index_cache_until = datetime.min.replace(tzinfo=timezone.utc)
            self._browse_devices_until = 0.0

    def _camera_devices(self) -> list[dict[str, Any]]:
        self._raise_if_cloud_paused()
        inventory = self._camera_inventory
        if not inventory:
            return []
        return [
            {"devId": dev_id, "name": name, "online": True}
            for dev_id, name in inventory.items()
        ]

    def _camera_candidates(self, devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self._camera_inventory is not None:
            return self._unique_devices([device for device in devices if self._device_id(device) in self._camera_inventory])
        return self._unique_devices(
            [device for device in devices if self._is_camera_category_device(device) or self._is_camera_name_device(device)]
        )

    def _camera_candidates_fallback(self, devices: list[dict[str, Any]], previous_by_dev_id: dict[str, Any]) -> list[dict[str, Any]]:
        if previous_by_dev_id:
            candidates = [device for device in devices if self._device_id(device) in previous_by_dev_id]
            if candidates:
                return self._unique_devices(candidates)
        return []

    @staticmethod
    def _is_camera_category_device(device: dict[str, Any]) -> bool:
        category = str(device.get("category") or "").strip().lower()
        if not category:
            return False
        if category in KNOWN_CAMERA_CATEGORIES:
            return True
        return any(fragment in category for fragment in KNOWN_CAMERA_CATEGORY_HINTS)

    @staticmethod
    def _is_camera_name_device(device: dict[str, Any]) -> bool:
        fields = (
            str(device.get("name") or ""),
            str(device.get("product_name") or device.get("productName") or device.get("product") or ""),
            str(device.get("device_name") or ""),
            str(device.get("product_id") or ""),
        )
        text = " ".join(fields).lower()
        return any(hint in text for hint in CAMERA_NAME_HINTS)

    @staticmethod
    def _unique_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        unique_devices = []
        for device in devices:
            dev_id = str(device.get("devId") or device.get("deviceId") or device.get("device_id") or device.get("id") or "")
            if not dev_id or dev_id in seen:
                continue
            seen.add(dev_id)
            unique_devices.append(device)
        return unique_devices

    @staticmethod
    def _device_id(device: dict[str, Any]) -> str:
        for key in ("id", "deviceId", "device_id", "devId"):
            value = device.get(key)
            if value:
                return str(value)
        return str(device.get("id", ""))

    def _raise_if_cloud_paused(self) -> None:
        if self.cloud_activity_paused:
            raise RuntimeError("Tuya Recordings camera activity is paused")

def _safe_segment(value: str) -> str:
    return safe_segment(value)


def _mp4_ready(path: Path) -> bool:
    try:
        if not path.exists() or path.stat().st_size < 1024:
            return False
        with path.open("rb") as file:
            head = file.read(4096)
            file.seek(max(path.stat().st_size - 1024 * 1024, 0))
            tail = file.read()
    except OSError:
        return False
    return b"ftyp" in head and (b"moov" in head or b"moov" in tail)


def _mp4_cached(path: Path) -> bool:
    try:
        if not path.exists() or path.stat().st_size < 1024:
            return False
        with path.open("rb") as file:
            head = file.read(4096)
    except OSError:
        return False
    return b"ftyp" in head


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _clip_has_event_thumbnail(clip: dict[str, Any]) -> bool:
    """Match ``TimePieceBean.hasEvents()`` from Smart Life's SD event list."""
    raw = clip.get("raw") if isinstance(clip, dict) else None
    if not isinstance(raw, dict):
        return False
    for name in ("eventTypeArr", "event_types", "event_type_arr"):
        value = raw.get(name)
        if isinstance(value, (list, tuple)) and value:
            return True
    return False


def _playback_fragments_json(start: int, end: int) -> str:
    """Serialize one Smart Life ``TimePieceBean`` for play-mode playback."""
    start = _as_epoch_seconds(start)
    end = _as_epoch_seconds(end)
    if start <= 0 or end <= start:
        raise ValueError("Playback fragment bounds are invalid")
    return json.dumps(
        {"fragments": [{"start": start, "end": end}]},
        separators=(",", ":"),
    )


def _serialize_media_failures(failures: dict[tuple[str, int, int], float]) -> dict[str, float]:
    return {f"{dev_id}|{start}|{end}": failed_at for (dev_id, start, end), failed_at in failures.items()}


def _parse_media_failures(raw: dict[str, Any]) -> dict[tuple[str, int, int], float]:
    failures: dict[tuple[str, int, int], float] = {}
    for key, value in raw.items():
        try:
            dev_id, start, end = str(key).split("|", 2)
            failures[(dev_id, int(start), int(end))] = float(value)
        except (TypeError, ValueError):
            continue
    return failures


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
