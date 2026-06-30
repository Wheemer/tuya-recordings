from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_LOOKBACK_DAYS,
    CONF_MEDIA_SYNC_ENABLED,
    CONF_MEDIA_SYNC_HOURS,
    CONF_MEDIA_STORAGE_PATH,
    CONF_MEDIA_VIEW_RECORDINGS_ORDER,
    CONF_REGION,
    CONF_THUMBNAIL_SYNC_ENABLED,
    CONF_USER_ID,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MEDIA_SYNC_ENABLED,
    DEFAULT_MEDIA_STORAGE_PATH,
    DEFAULT_THUMBNAIL_SYNC_ENABLED,
    DEFAULT_REGION,
    LOGGER,
)
from .lib import (
    CachedClipKey,
    MediaSyncStatus,
    TuyaIpcRecordingBackend,
    as_epoch_seconds as _as_epoch_seconds,
    best_clip_match as _best_clip_match,
    browser_relay_candidate_list as _browser_relay_candidate_list,
    clip_key as _clip_key,
    cleanup_cached_media,
    create_webrtc_offer as _lib_create_webrtc_offer,
    extract_h264_thumbnail as _extract_h264_thumbnail,
    extract_mp4_thumbnail as _extract_mp4_thumbnail,
    finalize_mp4_for_browser as _finalize_mp4_for_browser,
    filter_webrtc_candidates as _filter_webrtc_candidates,
    finish_live_h264_remux as _finish_live_h264_remux,
    first_thumbnail_value as _first_thumbnail_value,
    looks_like_thumbnail_key as _looks_like_thumbnail_key,
    merge_cached_clips as _merge_cached_clips,
    normalize_clip as _normalize_clip,
    normalize_outgoing_candidate as _normalize_outgoing_candidate,
    drain_helper_events as _lib_drain_helper_events,
    pion_helper_path as _lib_pion_helper_path,
    remux_h264_to_mp4 as _remux_h264_to_mp4,
    safe_segment,
    start_live_h264_remux as _start_live_h264_remux,
    start_pion_helper as _lib_start_pion_helper,
    strip_sdp_candidates as _strip_sdp_candidates,
)
from .lib.openapi import TuyaOpenApiAuthError as TuyaRecordingsAuthError
from .lib.openapi import TuyaOpenApiClient, TuyaOpenApiError as TuyaRecordingsApiError

CACHE_TTL = timedelta(minutes=10)
STALE_CACHE_TTL = timedelta(hours=12)
P2P_QUERY_TIMEOUT = 25
P2P_PLAYBACK_TIMEOUT = 45
P2P_PLAYBACK_MAX_TIMEOUT = 180
RECORDING_SCAN_MAX_DAYS = 31
RECORDING_SCAN_EMPTY_DAY_STOP = 7
RECENT_RECORDING_SCAN_DAYS = 2
THUMBNAIL_FAILURE_COOLDOWN = 6 * 60 * 60
MEDIA_FAILURE_CACHE_VERSION = 2
MEDIA_FAILURE_COOLDOWN = 15 * 60
MEDIA_MAX_ATTEMPTS_PER_CAMERA = 80
MEDIA_SYNC_INTER_ATTEMPT_DELAY = 0.5
MEDIA_SYNC_MIN_CLIP_AGE = 2 * 60
MEDIA_SYNC_MAX_CAMERA_WORKERS = 2
MEDIA_SYNC_CAMERA_PASS_TIMEOUT = 10 * 60
THUMBNAIL_AUTOFILL_LIMIT = 10
THUMBNAIL_AUTOFILL_COOLDOWN = 45
THUMBNAIL_SAMPLE_SECONDS = 2
THUMBNAIL_SAMPLE_TIMEOUT = 8
INDEX_SOURCE = "tuya_ipc_recordings"


class TuyaRecordingsClient:
    def __init__(
        self,
        entry_data: dict[str, Any],
        cache_path: Path | None = None,
        media_storage_path: Path | None = None,
    ) -> None:
        self.region = str(entry_data.get(CONF_REGION) or DEFAULT_REGION)
        self.client_id = str(entry_data.get(CONF_CLIENT_ID) or "")
        self.client_secret = str(entry_data.get(CONF_CLIENT_SECRET) or "")
        self.user_id = str(entry_data.get(CONF_USER_ID) or "")
        self._api: TuyaOpenApiClient | None = None
        if self.client_id and self.client_secret and self.user_id:
            self._api = TuyaOpenApiClient(
                region=self.region,
                client_id=self.client_id,
                client_secret=self.client_secret,
                user_id=self.user_id,
            )
        self._media_storage_path_override = media_storage_path
        self.update_options(entry_data)
        self._cache_path = cache_path
        self._cache_mtime_ns: int | None = None
        self._camera_index_cache: dict[str, Any] | None = None
        self._camera_index_cache_until = datetime.min.replace(tzinfo=timezone.utc)
        self._refresh_lock = threading.Lock()
        self._thumbnail_autofill_lock = threading.Lock()
        self._thumbnail_autofill_after = 0.0
        self._thumbnail_failures: dict[tuple[str, int, int], float] = {}
        self._media_failures: dict[tuple[str, int, int], float] = {}
        self._media_sync_status = MediaSyncStatus()
        self._ipc = TuyaIpcRecordingBackend(
            logger=LOGGER,
            query_timeout=P2P_QUERY_TIMEOUT,
            playback_timeout=P2P_PLAYBACK_TIMEOUT,
            playback_max_timeout=P2P_PLAYBACK_MAX_TIMEOUT,
        )
    def update_options(self, entry_data: dict[str, Any]) -> None:
        self.lookback_days = int(entry_data.get(CONF_LOOKBACK_DAYS, DEFAULT_LOOKBACK_DAYS) or 0)
        self.media_sync_enabled = bool(entry_data.get(CONF_MEDIA_SYNC_ENABLED, DEFAULT_MEDIA_SYNC_ENABLED))
        self.thumbnail_sync_enabled = bool(entry_data.get(CONF_THUMBNAIL_SYNC_ENABLED, DEFAULT_THUMBNAIL_SYNC_ENABLED))
        self.media_sync_hours = int(entry_data.get(CONF_MEDIA_SYNC_HOURS, 0) or 0)
        self.media_view_recordings_order = entry_data.get(CONF_MEDIA_VIEW_RECORDINGS_ORDER, "Descending")
        configured_media_path = entry_data.get(CONF_MEDIA_STORAGE_PATH, DEFAULT_MEDIA_STORAGE_PATH)
        self.media_storage_path = self._media_storage_path_override or Path(str(configured_media_path))

    def camera_index(self, force_refresh: bool = False) -> dict[str, Any]:
        if not self._refresh_lock.acquire(blocking=False):
            return self._busy_cache()
        try:
            return self._camera_index_locked(force_refresh)
        finally:
            self._refresh_lock.release()

    def _camera_index_locked(self, force_refresh: bool = False) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        if not force_refresh and self._camera_index_cache and now < self._camera_index_cache_until:
            cached = dict(self._camera_index_cache)
            cached["cached"] = True
            cached["cacheExpiresAt"] = self._camera_index_cache_until.isoformat()
            return cached
        try:
            devices = self._camera_devices()
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
        for device in devices:
            if device.get("category") not in {"sp", "dghsxj"}:
                continue
            dev_id = device.get("devId") or device.get("deviceId") or device.get("id")
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

    def diagnostics(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "api_source": "tuya_openapi",
            "region": self.region,
            "user_id": self.user_id,
            "has_client_id": bool(self.client_id),
            "has_client_secret": bool(self.client_secret),
            "lookback_days": self.lookback_days,
            "recording_scan_max_days": RECORDING_SCAN_MAX_DAYS,
            "recording_scan_empty_day_stop": RECORDING_SCAN_EMPTY_DAY_STOP,
            "media_sync_enabled": self.media_sync_enabled,
            "media_sync_hours": self.media_sync_hours,
            "media_storage_path": str(self.media_storage_path),
            "has_cache": self._camera_index_cache is not None,
            "cache_expires_at": self._camera_index_cache_until.isoformat(),
            "cache_fresh": self._camera_index_cache is not None and now < self._camera_index_cache_until,
            "cache_path_configured": self._cache_path is not None,
            "refresh_running": self._refresh_lock.locked(),
            "media_sync_status": self._media_sync_status.to_dict(),
        }

    def clip_path(self, dev_id: str, start: int, end: int) -> Path:
        return Path(self.media_storage_path) / "videos" / f"{_safe_segment(dev_id)}_{start}_{end}.mp4"

    def clip_cached(self, dev_id: str, start: int, end: int) -> bool:
        return _mp4_cached(self.clip_path(dev_id, start, end))

    def clip_ready(self, dev_id: str, start: int, end: int) -> bool:
        return _mp4_ready(self.clip_path(dev_id, start, end))

    def thumbnail_path(self, dev_id: str, start: int, end: int) -> Path:
        return Path(self.media_storage_path) / "thumbs" / f"{_safe_segment(dev_id)}_{start}_{end}.jpg"

    def ensure_thumbnail(self, dev_id: str, start: int, end: int) -> Path | None:
        video_path = self.clip_path(dev_id, start, end)
        thumbnail_path = self.thumbnail_path(dev_id, start, end)
        if thumbnail_path.exists() and thumbnail_path.stat().st_size > 0:
            return thumbnail_path
        if not _mp4_ready(video_path):
            return None
        thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
        _extract_mp4_thumbnail(video_path, thumbnail_path)
        return thumbnail_path if thumbnail_path.exists() and thumbnail_path.stat().st_size > 0 else None

    def _ipc_bootstrap(self, dev_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Fetch the Tuya IPC signaling credentials used by the camera stack."""
        api = self._require_api()
        config = api.get_webrtc_config(dev_id)
        mqtt_auth = api.get_open_iot_hub_config()
        return config, mqtt_auth

    def sd_recordings(self, dev_id: str) -> tuple[list[dict[str, Any]], list[str]]:
        today = date.today()
        all_clips: list[dict[str, Any]] = []
        days_checked: list[str] = []
        config, mqtt_auth = self._ipc_bootstrap(dev_id)
        empty_days = 0
        scan_days = self.lookback_days if self.lookback_days > 0 else RECORDING_SCAN_MAX_DAYS
        for offset in range(scan_days):
            day = today - timedelta(days=offset)
            days_checked.append(day.isoformat())
            day_clips = self._ipc_recordings_for_day(dev_id, config, mqtt_auth, day)
            if day_clips:
                empty_days = 0
                all_clips.extend(day_clips)
                continue
            empty_days += 1
            if self.lookback_days <= 0 and empty_days >= RECORDING_SCAN_EMPTY_DAY_STOP:
                break

        all_clips.sort(key=lambda clip: int(clip.get("start", 0)), reverse=True)
        return all_clips, days_checked

    def refresh_recent_recordings(self) -> dict[str, Any]:
        """Refresh the cached index by scanning only the newest recording days."""
        if not self._camera_index_cache:
            return self.camera_index(True)
        if not self._refresh_lock.acquire(blocking=False):
            return self._busy_cache()
        try:
            return self._refresh_recent_recordings_locked()
        finally:
            self._refresh_lock.release()

    def _refresh_recent_recordings_locked(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        try:
            devices = self._camera_devices()
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

        for device in devices:
            if device.get("category") not in {"sp", "dghsxj"}:
                continue
            dev_id = str(device.get("devId") or device.get("deviceId") or device.get("id") or "")
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
                    config, mqtt_auth = self._ipc_bootstrap(dev_id)
                    for day in recent_days:
                        days_checked.append(day.isoformat())
                        clips.extend(self._ipc_recordings_for_day(dev_id, config, mqtt_auth, day))
                    clips = _merge_cached_clips(clips, previous_clips)
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
        self._store_cache(index, CACHE_TTL)
        LOGGER.info("Refreshed recent Tuya recordings cache for %s camera(s)", len(cameras))
        return index

    def download_clip(
        self,
        dev_id: str,
        start: int,
        end: int,
        output_path: Path,
        *,
        verify_clip: bool = True,
        log_traceback: bool = True,
    ) -> Path:
        output_path = Path(output_path)
        if _mp4_ready(output_path):
            try:
                self.ensure_thumbnail(dev_id, start, end)
            except Exception as exc:
                LOGGER.debug("Could not create cached Tuya recording thumbnail for %s %s-%s: %s", dev_id, start, end, exc)
            LOGGER.info("Using cached Tuya recording for %s %s-%s at %s", dev_id, start, end, output_path)
            return output_path
        if output_path.exists():
            LOGGER.warning("Removing invalid cached Tuya recording for %s %s-%s at %s", dev_id, start, end, output_path)
            output_path.unlink(missing_ok=True)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        h264_path = output_path.with_name(f"{output_path.name}.h264.pipe")
        temp_output_path = output_path.with_suffix(".tmp.mp4")
        h264_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        temp_output_path.unlink(missing_ok=True)

        LOGGER.info("Downloading Tuya IPC recording for %s %s-%s to %s", dev_id, start, end, output_path)
        config, mqtt_auth = self._ipc_bootstrap(dev_id)
        attempts = [verify_clip]
        if not verify_clip:
            attempts.append(True)
        for attempt_index, attempt_verify_clip in enumerate(attempts, start=1):
            remux_proc: subprocess.Popen[str] | None = None
            h264_path.unlink(missing_ok=True)
            temp_output_path.unlink(missing_ok=True)
            try:
                remux_proc = _start_live_h264_remux(h264_path, temp_output_path)
                self._ipc_download_clip_h264(dev_id, config, mqtt_auth, int(start), int(end), h264_path, verify_clip=attempt_verify_clip)
                _finish_live_h264_remux(remux_proc, temp_output_path)
                _finalize_mp4_for_browser(temp_output_path, output_path)
                temp_output_path.unlink(missing_ok=True)
                break
            except Exception as exc:
                if self._try_finalize_partial_remux(temp_output_path, output_path, remux_proc):
                    LOGGER.info(
                        "Recovered partial Tuya IPC recording for %s %s-%s; mp4_bytes=%s",
                        dev_id,
                        start,
                        end,
                        output_path.stat().st_size,
                    )
                    break
                should_retry = attempt_index < len(attempts)
                if should_retry:
                    LOGGER.warning(
                        "Tuya IPC recording primary stream failed for %s %s-%s; retrying with camera-queried playback bounds: %s",
                        dev_id,
                        start,
                        end,
                        exc,
                    )
                else:
                    self._log_download_failure(
                        dev_id,
                        start,
                        end,
                        h264_path,
                        temp_output_path,
                        output_path,
                        remux_proc,
                        log_traceback,
                    )
                    raise
                self._cleanup_failed_remux(h264_path, temp_output_path, output_path, remux_proc)
                continue
        else:
            raise RuntimeError("Tuya IPC recording download did not run")
        h264_path.unlink(missing_ok=True)
        try:
            self.ensure_thumbnail(dev_id, start, end)
        except Exception as exc:
            LOGGER.debug("Could not create Tuya recording thumbnail for %s %s-%s: %s", dev_id, start, end, exc)
        LOGGER.info(
            "Downloaded Tuya IPC recording for %s %s-%s; mp4_bytes=%s",
            dev_id,
            start,
            end,
            output_path.stat().st_size,
        )
        return output_path

    @staticmethod
    def _cleanup_failed_remux(
        h264_path: Path,
        temp_output_path: Path,
        output_path: Path,
        remux_proc: subprocess.Popen[str] | None,
    ) -> None:
        if remux_proc is not None and remux_proc.poll() is None:
            remux_proc.terminate()
            try:
                remux_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                remux_proc.kill()
        if h264_path.exists() and h264_path.stat().st_size <= 0:
            h264_path.unlink(missing_ok=True)
        temp_output_path.unlink(missing_ok=True)
        if output_path.exists() and output_path.stat().st_size <= 0:
            output_path.unlink(missing_ok=True)

    @staticmethod
    def _try_finalize_partial_remux(
        temp_output_path: Path,
        output_path: Path,
        remux_proc: subprocess.Popen[str] | None,
    ) -> bool:
        def promote_fragmented_mp4() -> bool:
            if _mp4_ready(temp_output_path):
                temp_output_path.replace(output_path)
                return _mp4_ready(output_path)
            return False

        try:
            if remux_proc is None:
                return False
            if not temp_output_path.exists() or temp_output_path.stat().st_size < 1024:
                return False
            try:
                _finish_live_h264_remux(remux_proc, temp_output_path)
            except Exception:
                return promote_fragmented_mp4()
            try:
                _finalize_mp4_for_browser(temp_output_path, output_path)
                temp_output_path.unlink(missing_ok=True)
                return _mp4_ready(output_path)
            except Exception:
                return promote_fragmented_mp4()
        except Exception as exc:
            LOGGER.debug("Could not recover partial Tuya recording remux at %s: %s", temp_output_path, exc)
            return False

    def _log_download_failure(
        self,
        dev_id: str,
        start: int,
        end: int,
        h264_path: Path,
        temp_output_path: Path,
        output_path: Path,
        remux_proc: subprocess.Popen[str] | None,
        log_traceback: bool,
    ) -> None:
            h264_size = h264_path.stat().st_size if h264_path.exists() else 0
            output_size = temp_output_path.stat().st_size if temp_output_path.exists() else output_path.stat().st_size if output_path.exists() else 0
            message = "Failed to transfer Tuya IPC recording for %s %s-%s; temp_h264_bytes=%s output_bytes=%s"
            if log_traceback:
                LOGGER.exception(message, dev_id, start, end, h264_size, output_size)
            else:
                LOGGER.warning(message, dev_id, start, end, h264_size, output_size)
            self._cleanup_failed_remux(h264_path, temp_output_path, output_path, remux_proc)

    def sync_recordings(self) -> dict[str, Any]:
        if not self.media_sync_enabled:
            result = {"enabled": False, "downloaded": 0, "skipped": 0, "failed": 0, "deleted_videos": 0, "deleted_thumbnails": 0}
            self._set_media_sync_status("disabled", last_result=result)
            return result

        recovered_partials = self._recover_interrupted_media_sync()
        index = self.refresh_recent_recordings()
        clips_by_camera: dict[str, list[dict[str, Any]]] = {}
        for camera in index.get("cameras", []):
            dev_id = camera.get("devId")
            if not dev_id:
                continue
            camera_clips: list[dict[str, Any]] = []
            for clip in camera.get("clips", []):
                camera_clips.append(clip)
            camera_clips.sort(key=lambda item: int(item.get("start") or 0), reverse=True)
            clips_by_camera[str(dev_id)] = camera_clips

        if not clips_by_camera:
            result = {"enabled": True, "downloaded": 0, "skipped": 0, "failed": 0, "deleted_videos": 0, "deleted_thumbnails": 0}
            self._set_media_sync_status("idle", total=0, current=None, last_result=result)
            return result

        clips = self._round_robin_clips(clips_by_camera)

        cutoff = None
        if self.media_sync_hours > 0:
            newest_end = max(int(clip.get("end") or 0) for _, clip in clips)
            cutoff = newest_end - (self.media_sync_hours * 60 * 60)
        desired_clips = {
            CachedClipKey.from_raw(str(dev_id), int(clip.get("start") or 0), int(clip.get("end") or 0))
            for dev_id, clip in clips
            if int(clip.get("start") or 0) and int(clip.get("end") or 0) and (cutoff is None or int(clip.get("end") or 0) >= cutoff)
        }
        downloaded = 0
        skipped = 0
        failed = 0
        total = len(desired_clips)
        newest_sync_end = int(time.time()) - MEDIA_SYNC_MIN_CLIP_AGE
        media_failures_changed = False
        progress_position = 0
        progress_lock = threading.Lock()

        def mark_skipped(count: int = 1) -> None:
            nonlocal skipped
            with progress_lock:
                skipped += count

        def set_current(dev_id: str, start: int, end: int) -> None:
            nonlocal progress_position
            with progress_lock:
                progress_position += 1
                self._set_media_sync_status(
                    "running",
                    downloaded=downloaded,
                    skipped=skipped,
                    failed=failed,
                    current={"dev_id": dev_id, "start": start, "end": end, "position": min(progress_position, total), "total": total},
                )

        def clear_failure(key: tuple[str, int, int]) -> None:
            nonlocal media_failures_changed
            with progress_lock:
                if self._media_failures.pop(key, None) is not None:
                    media_failures_changed = True

        def remember_failure(key: tuple[str, int, int], exc: Exception) -> None:
            nonlocal failed, media_failures_changed
            with progress_lock:
                failed += 1
                self._media_failures[key] = time.time()
                media_failures_changed = True
                self._set_media_sync_status("running", failed=failed, last_error=str(exc))

        def mark_downloaded(key: tuple[str, int, int]) -> None:
            nonlocal downloaded, media_failures_changed
            with progress_lock:
                self._media_failures.pop(key, None)
                media_failures_changed = True
                downloaded += 1
                self._set_media_sync_status("running", downloaded=downloaded, skipped=skipped, failed=failed)

        def recently_failed(key: tuple[str, int, int]) -> bool:
            with progress_lock:
                failed_at = self._media_failures.get(key)
                if failed_at is None:
                    return False
                if time.time() - failed_at < MEDIA_FAILURE_COOLDOWN:
                    return True
                self._media_failures.pop(key, None)
                return False

        def sync_camera(dev_id: str, camera_clips: list[dict[str, Any]]) -> None:
            attempted = 0
            for clip in camera_clips:
                start = int(clip.get("start") or 0)
                end = int(clip.get("end") or 0)
                if not start or not end or (cutoff is not None and end < cutoff):
                    mark_skipped()
                    continue
                if end > newest_sync_end:
                    mark_skipped()
                    continue
                key = (str(dev_id), start, end)
                set_current(str(dev_id), start, end)
                output_path = self.clip_path(dev_id, start, end)
                if _mp4_ready(output_path):
                    try:
                        self.ensure_thumbnail(dev_id, start, end)
                    except Exception as exc:
                        LOGGER.debug("Could not create cached Tuya recording thumbnail for %s %s-%s: %s", dev_id, start, end, exc)
                    mark_skipped()
                    clear_failure(key)
                    continue
                if attempted >= MEDIA_MAX_ATTEMPTS_PER_CAMERA:
                    mark_skipped()
                    continue
                if recently_failed(key):
                    mark_skipped()
                    continue
                if attempted and MEDIA_SYNC_INTER_ATTEMPT_DELAY > 0:
                    time.sleep(MEDIA_SYNC_INTER_ATTEMPT_DELAY)
                attempted += 1
                try:
                    self.download_clip(dev_id, start, end, output_path, verify_clip=False, log_traceback=False)
                except Exception as exc:
                    remember_failure(key, exc)
                    LOGGER.warning("Media Sync skipped Tuya recording %s %s-%s: %s", dev_id, start, end, exc)
                    continue
                mark_downloaded(key)

        self._set_media_sync_status("running", downloaded=0, skipped=0, failed=0, total=total, current=None, last_error=None)
        max_workers = min(MEDIA_SYNC_MAX_CAMERA_WORKERS, len(clips_by_camera))
        stalled_cameras: list[str] = []
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            futures = {executor.submit(sync_camera, dev_id, camera_clips): dev_id for dev_id, camera_clips in clips_by_camera.items()}
            for future in as_completed(futures, timeout=MEDIA_SYNC_CAMERA_PASS_TIMEOUT):
                future.result()
        except TimeoutError:
            stalled_cameras = [dev_id for future, dev_id in futures.items() if not future.done()]
            failed += len(stalled_cameras)
            self._set_media_sync_status(
                "stalled",
                downloaded=downloaded,
                skipped=skipped,
                failed=failed,
                current=None,
                stalled_cameras=stalled_cameras,
                last_error=f"Timed out waiting for camera sync: {', '.join(stalled_cameras)}",
            )
            LOGGER.warning("Media Sync timed out waiting for Tuya camera workers: %s", ", ".join(stalled_cameras))
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        cleanup = self.cleanup_cached_media(desired_clips, cutoff)
        if media_failures_changed:
            self._store_media_failures()
        result = {
            "enabled": True,
            "downloaded": downloaded,
            "skipped": skipped,
            "failed": failed,
            "recovered_partials": recovered_partials,
            "stalled_cameras": stalled_cameras,
            **cleanup,
        }
        self._set_media_sync_status(
            "stalled" if stalled_cameras else "idle",
            downloaded=downloaded,
            skipped=skipped,
            failed=failed,
            current=None,
            deleted_videos=cleanup["deleted_videos"],
            deleted_thumbnails=cleanup["deleted_thumbnails"],
            recovered_partials=recovered_partials,
            stalled_cameras=stalled_cameras,
            last_result=result,
        )
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
                parsed = CachedClipKey.from_raw(dev_id, int(start), int(end))
            except (TypeError, ValueError):
                LOGGER.debug("Recovered Tuya recording has an unrecognized cache name: %s", output_path)
                recovered += 1
                continue
            try:
                self.ensure_thumbnail(parsed.dev_id, parsed.start, parsed.end)
            except Exception as exc:
                LOGGER.debug("Could not create thumbnail for recovered Tuya recording %s: %s", output_path, exc)
            recovered += 1
            LOGGER.info("Recovered interrupted Tuya recording cache file %s", output_path)
        for pipe_path in video_folder.glob("*.mp4.h264.pipe"):
            try:
                pipe_path.unlink()
            except OSError as exc:
                LOGGER.debug("Could not delete interrupted Tuya recording pipe %s: %s", pipe_path, exc)
        return recovered

    def _set_media_sync_status(self, state: str, **updates: Any) -> None:
        self._media_sync_status.update(state, **updates)

    def populate_thumbnails(self, limit: int = 3) -> dict[str, Any]:
        index = self.cached_camera_index()
        created = 0
        created_from_cache = 0
        skipped = 0
        failed = 0
        checked = 0
        max_checks = max(0, int(limit or 0))
        for camera in index.get("cameras", []):
            dev_id = camera.get("devId")
            if not dev_id:
                continue
            for clip in camera.get("clips", []):
                if max_checks and checked >= max_checks:
                    return {
                        "created": created,
                        "created_from_cache": created_from_cache,
                        "skipped": skipped,
                        "failed": failed,
                        "checked": checked,
                        "limit": limit,
                    }
                start = int(clip.get("start") or 0)
                end = int(clip.get("end") or 0)
                if not start or not end:
                    skipped += 1
                    continue
                thumbnail_path = self.thumbnail_path(dev_id, start, end)
                if thumbnail_path.exists() and thumbnail_path.stat().st_size > 0:
                    skipped += 1
                    continue
                checked += 1
                try:
                    if self.ensure_thumbnail(dev_id, start, end):
                        created_from_cache += 1
                    elif self.thumbnail_sync_enabled and self.create_thumbnail_sample(dev_id, start, end):
                        created += 1
                    else:
                        skipped += 1
                except Exception as exc:
                    LOGGER.debug("Could not populate Tuya recording thumbnail for %s %s-%s: %s", dev_id, start, end, exc)
                    failed += 1
        return {
            "created": created,
            "created_from_cache": created_from_cache,
            "skipped": skipped,
            "failed": failed,
            "checked": checked,
            "limit": limit,
        }

    def _thumbnail_failure_is_recent(self, key: tuple[str, int, int]) -> bool:
        failed_at = self._thumbnail_failures.get(key)
        if failed_at is None:
            return False
        if time.time() - failed_at < THUMBNAIL_FAILURE_COOLDOWN:
            return True
        self._thumbnail_failures.pop(key, None)
        return False

    def populate_thumbnails_for_clips(
        self,
        dev_id: str,
        clips: list[dict[str, Any]],
        limit: int = THUMBNAIL_AUTOFILL_LIMIT,
    ) -> dict[str, Any]:
        now = time.time()
        if now < self._thumbnail_autofill_after:
            return {"created": 0, "skipped": 0, "failed": 0, "checked": 0, "throttled": True}
        if not self._thumbnail_autofill_lock.acquire(blocking=False):
            return {"created": 0, "skipped": 0, "failed": 0, "checked": 0, "running": True}
        self._thumbnail_autofill_after = now + THUMBNAIL_AUTOFILL_COOLDOWN
        try:
            created = 0
            created_from_cache = 0
            skipped = 0
            failed = 0
            checked = 0
            max_checks = max(0, int(limit or 0))
            for clip in clips:
                if max_checks and checked >= max_checks:
                    break
                start = int(clip.get("start") or 0)
                end = int(clip.get("end") or 0)
                if not start or not end:
                    skipped += 1
                    continue
                thumbnail_path = self.thumbnail_path(dev_id, start, end)
                if thumbnail_path.exists() and thumbnail_path.stat().st_size > 0:
                    skipped += 1
                    continue
                checked += 1
                try:
                    if self.ensure_thumbnail(dev_id, start, end):
                        created_from_cache += 1
                    elif self.thumbnail_sync_enabled and self.create_thumbnail_sample(dev_id, start, end):
                        created += 1
                    else:
                        skipped += 1
                except Exception as exc:
                    LOGGER.debug("Could not populate Tuya recording thumbnail for %s %s-%s: %s", dev_id, start, end, exc)
                    failed += 1
            return {
                "created": created,
                "created_from_cache": created_from_cache,
                "skipped": skipped,
                "failed": failed,
                "checked": checked,
                "limit": limit,
            }
        finally:
            self._thumbnail_autofill_lock.release()

    def create_thumbnail_sample(self, dev_id: str, start: int, end: int) -> Path | None:
        """Create a thumbnail by briefly sampling the SD-card playback stream."""
        thumbnail_path = self.thumbnail_path(dev_id, start, end)
        if thumbnail_path.exists() and thumbnail_path.stat().st_size > 0:
            return thumbnail_path
        sample_end = min(int(end), int(start) + THUMBNAIL_SAMPLE_SECONDS)
        if sample_end <= int(start):
            return None
        thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
        sample_path = thumbnail_path.with_suffix(".sample.h264")
        sample_path.unlink(missing_ok=True)
        try:
            config, mqtt_auth = self._ipc_bootstrap(dev_id)
            try:
                self._ipc_download_clip_h264(
                    dev_id,
                    config,
                    mqtt_auth,
                    int(start),
                    sample_end,
                    sample_path,
                    playback_timeout=THUMBNAIL_SAMPLE_TIMEOUT,
                    verify_clip=False,
                )
            except Exception:
                sample_path.unlink(missing_ok=True)
                self._ipc_download_clip_h264(
                    dev_id,
                    config,
                    mqtt_auth,
                    int(start),
                    int(end),
                    sample_path,
                    playback_timeout=THUMBNAIL_SAMPLE_TIMEOUT,
                    verify_clip=True,
                )
            _extract_h264_thumbnail(sample_path, thumbnail_path)
            return thumbnail_path
        finally:
            sample_path.unlink(missing_ok=True)

    def _ipc_recordings_for_day(
        self,
        dev_id: str,
        config: dict[str, Any],
        mqtt_auth: dict[str, Any],
        day: date,
    ) -> list[dict[str, Any]]:
        return self._ipc.recordings_for_day(dev_id, config, mqtt_auth, day)

    def _ipc_download_clip_h264(
        self,
        dev_id: str,
        config: dict[str, Any],
        mqtt_auth: dict[str, Any],
        start: int,
        end: int,
        h264_path: Path,
        playback_timeout: int | None = None,
        verify_clip: bool = True,
    ) -> None:
        self._ipc.download_clip_h264(
            dev_id,
            config,
            mqtt_auth,
            start,
            end,
            h264_path,
            playback_timeout=playback_timeout,
            verify_clip=verify_clip,
        )

    def validate_session(self) -> dict[str, Any]:
        api = self._require_api()
        return {
            "source": "tuya_openapi",
            "user_id": api.user_id,
            "devices": len(api.get_devices()),
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
            self._cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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
            self._cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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
                self._cache_path.write_text(
                    json.dumps(
                        {
                            "expiresAt": self._camera_index_cache_until.isoformat(),
                            "index": stale,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                self._cache_mtime_ns = self._cache_path.stat().st_mtime_ns
            except OSError:
                pass
        return stale

    def _camera_devices(self) -> list[dict[str, Any]]:
        devices = self._require_api().get_devices()
        return [dict(device, devId=device.get("id")) for device in devices if isinstance(device, dict)]

    def _require_api(self) -> TuyaOpenApiClient:
        if self._api is None:
            raise TuyaRecordingsAuthError(
                "tuya_openapi",
                {
                    "code": "missing_credentials",
                    "msg": "Tuya OpenAPI credentials are missing. Configure with LocalTuya cloud credentials or enter Access ID, Access Secret, region, and user ID.",
                },
            )
        return self._api


def _create_webrtc_offer() -> str:
    return _lib_create_webrtc_offer(_pion_helper_path(), process_module=subprocess)


def _start_pion_helper(
    config: dict[str, Any],
    h264_output: Path | None = None,
    helper_timeout: int | None = None,
) -> subprocess.Popen[str]:
    return _lib_start_pion_helper(
        _pion_helper_path(),
        config,
        query_timeout=P2P_QUERY_TIMEOUT,
        playback_timeout=P2P_PLAYBACK_TIMEOUT,
        h264_output=h264_output,
        helper_timeout=helper_timeout,
        process_module=subprocess,
    )


def _drain_helper_errors(errors: queue.Queue[str]) -> str:
    messages: list[str] = []
    while True:
        try:
            messages.append(errors.get_nowait())
        except queue.Empty:
            break
    return "; ".join(message for message in messages if message) or "no helper stderr"


def _publish_helper_local_candidates(
    events: queue.Queue[dict[str, Any]],
    mqtt_client: Any,
    topic: str,
    msid: str,
    dev_id: str,
    moto_id: str,
    session_id: str,
    protocol_version: str,
) -> None:
    _drain_helper_events(events, mqtt_client, topic, msid, dev_id, moto_id, session_id, protocol_version)


def _drain_helper_events(
    events: queue.Queue[dict[str, Any]],
    mqtt_client: Any,
    topic: str,
    msid: str,
    dev_id: str,
    moto_id: str,
    session_id: str,
    protocol_version: str,
) -> list[dict[str, Any]]:
    return _lib_drain_helper_events(events, mqtt_client, topic, msid, dev_id, moto_id, session_id, protocol_version)


def _pion_helper_path() -> Path:
    return _lib_pion_helper_path()


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
