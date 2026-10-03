from __future__ import annotations

import asyncio
import logging
import queue
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aiohttp import WSCloseCode, web

from homeassistant.components import http
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .client import TuyaRecordingsAuthError, TuyaRecordingsClient
from .lib.commands import CameraWorkBusy, CameraWorkCancelled
from .const import (
    DATA_INTERACTIVE_PLAYBACK_ACTIVE,
    DOMAIN,
    NAME,
    THUMBNAIL_BACKGROUND_COOLDOWN,
)

_LOGGER = logging.getLogger(__name__)

_DATA_MEDIA_SYNC_PENDING = "media_sync_pending"
_DATA_THUMBNAIL_SYNC_PENDING = "thumbnail_sync_pending"
_DATA_THUMBNAIL_SYNC_LIMIT = "thumbnail_sync_limit"
_DATA_THUMBNAIL_SYNC_REQUIRE_MEDIA = "thumbnail_sync_require_media"
_DATA_THUMBNAIL_SYNC_REFRESH_CATALOG = "thumbnail_sync_refresh_catalog"
_DATA_THUMBNAIL_SYNC_RETRY_AFTER = "thumbnail_sync_retry_after"
_DATA_PLAYBACK_GENERATION = "_playback_generation"
_DATA_PLAYBACK_LOCK = "_playback_lock"
_DATA_PLAYBACK_STOP = "_playback_stop_event"
_DATA_PLAYBACK_WS = "_playback_websocket"
_PLAYBACK_HANDOFF_TIMEOUT = 30.0


def build_panel_data(client: TuyaRecordingsClient, index: dict[str, Any], media_root: Path = Path("/media")) -> dict[str, Any]:
    """Build cache-backed panel data from the recording index."""
    cameras: list[dict[str, Any]] = []
    stats: dict[str, Any] = _empty_stats(client)
    storage_root = getattr(client, "media_storage_path", None)
    video_files = _ready_file_sizes(Path(storage_root) / "videos", "*.mp4") if storage_root else None
    thumbnail_files = _ready_file_sizes(Path(storage_root) / "thumbs", "*.jpg") if storage_root else None
    cloud_activity_paused = bool(getattr(client, "cloud_activity_paused", False))
    media_cache_only = bool(getattr(client, "media_sync_enabled", False))
    playback_mode = "clips" if media_cache_only else "timeline"
    for camera in index.get("cameras", []):
        dev_id = str(camera.get("devId") or "")
        if not dev_id:
            continue
        stats["total_cameras"] += 1
        if camera.get("online"):
            stats["online_cameras"] += 1
        live_playback_available = (
            not cloud_activity_paused
            and _uncached_playback_available(client)
            and camera.get("online") is True
        )
        clips = []
        recording_ranges = []
        dates: set[str] = set()
        camera_stats = {
            "dev_id": dev_id,
            "name": str(camera.get("name") or dev_id),
            "indexed_clips": 0,
            "cached_videos": 0,
            "cached_thumbnails": 0,
            "ready_clips": 0,
            "pending_clips": 0,
            "video_bytes": 0,
            "thumbnail_bytes": 0,
        }
        for clip in camera.get("clips", []):
            start = int(clip.get("start") or 0)
            end = int(clip.get("end") or 0)
            clip_date = str(clip.get("date") or "")
            if not start or not end or not clip_date:
                continue
            stats["indexed_clips"] += 1
            camera_stats["indexed_clips"] += 1
            clip_path = client.clip_path(dev_id, start, end)
            thumbnail_path = client.thumbnail_path(dev_id, start, end)
            clip_candidate = video_files is None or clip_path in video_files
            clip_cached = clip_candidate and _clip_ready(client, dev_id, start, end, clip_path)
            thumbnail_cached = media_cache_only and (
                thumbnail_path in thumbnail_files
                if thumbnail_files is not None
                else _path_ready(thumbnail_path)
            )
            if clip_cached:
                stats["cached_videos"] += 1
                camera_stats["cached_videos"] += 1
                size = video_files.get(clip_path, 0) if video_files is not None else _file_size(clip_path)
                camera_stats["video_bytes"] += size
            if thumbnail_cached:
                stats["cached_thumbnails"] += 1
                camera_stats["cached_thumbnails"] += 1
                size = thumbnail_files.get(thumbnail_path, 0) if thumbnail_files is not None else _file_size(thumbnail_path)
                camera_stats["thumbnail_bytes"] += size
            playback_ready = clip_cached or live_playback_available
            ready = thumbnail_cached and (clip_cached if media_cache_only else playback_ready)
            if ready:
                stats["ready_clips"] += 1
                camera_stats["ready_clips"] += 1
            item = {
                "dev_id": dev_id,
                "date": clip_date,
                "start": start,
                "end": end,
                "duration": max(0, end - start),
                "title": str(clip.get("title") or _clip_title(start, end)),
                "cached": clip_cached,
                "thumbnail_cached": thumbnail_cached,
                "thumbnail_url": _local_media_url(thumbnail_path, media_root) if thumbnail_cached else "",
            }
            if media_cache_only:
                if not ready:
                    continue
                dates.add(clip_date)
                item["playback_url"] = _local_media_url(clip_path, media_root)
                item["stream_transport"] = "file"
                clips.append(item)
            else:
                # Timeline ranges describe what exists on the camera. They stay
                # visible while paused/offline and do not pretend to be files.
                item["locally_cached"] = clip_cached
                item["cached"] = False
                item["playback_url"] = (
                    _timeline_socket_url(dev_id) if live_playback_available else ""
                )
                item["stream_transport"] = "native"
                recording_ranges.append(item)
                dates.add(clip_date)
            _update_latest_clip(stats, dev_id, str(camera.get("name") or dev_id), start, end)
            stats["visible_clips"] += 1
        camera_stats["pending_clips"] = max(0, camera_stats["indexed_clips"] - camera_stats["ready_clips"])
        stats["pending_clips"] += camera_stats["pending_clips"]
        stats["camera_stats"].append(camera_stats)
        clips.sort(key=lambda item: int(item["start"]), reverse=True)
        recording_ranges.sort(key=lambda item: int(item["start"]))
        cameras.append(
            {
                "dev_id": dev_id,
                "name": str(camera.get("name") or dev_id),
                "online": bool(camera.get("online")),
                "dates": sorted(dates, reverse=True),
                "playback_mode": playback_mode,
                "clips": clips,
                "recording_ranges": recording_ranges,
            }
        )
    _add_storage_stats(stats, client, video_files, thumbnail_files)
    _add_sync_stats(stats, client)
    return {
        "title": NAME,
        "generated_at": index.get("generatedAt"),
        "warning": index.get("warning"),
        "stats": stats,
        "cameras": cameras,
    }


def _empty_stats(client: TuyaRecordingsClient) -> dict[str, Any]:
    media_storage_path = getattr(client, "media_storage_path", None)
    backend = getattr(client, "_recordings_backend", None)
    backend_name = str(getattr(backend, "name", "unknown"))
    backend_reason = str(getattr(backend, "reason", ""))
    return {
        "indexed_clips": 0,
        "cached_videos": 0,
        "cached_thumbnails": 0,
        "ready_clips": 0,
        "pending_clips": 0,
        "visible_clips": 0,
        "total_cameras": 0,
        "online_cameras": 0,
        "latest_clip": None,
        "video_files": 0,
        "thumbnail_files": 0,
        "video_bytes": 0,
        "thumbnail_bytes": 0,
        "total_bytes": 0,
        "media_storage_path": str(media_storage_path) if media_storage_path else "",
        "sync": {},
        "camera_stats": [],
        "cache_only": bool(getattr(client, "media_sync_enabled", False)),
        "thumbnail_cache_only": bool(getattr(client, "media_sync_enabled", False)),
        "cloud_activity_paused": bool(getattr(client, "cloud_activity_paused", False)),
        "recordings_backend": {
            "name": backend_name,
            "available": _backend_available(client),
            "clip_playback_available": _uncached_playback_available(client),
            "reason": backend_reason,
        },
    }


def _backend_available(client: TuyaRecordingsClient) -> bool:
    backend = getattr(client, "_recordings_backend", None)
    if backend is None:
        return True
    return getattr(backend, "name", "") != "apk-native-not-configured"


def _uncached_playback_available(client: TuyaRecordingsClient) -> bool:
    backend = getattr(client, "_recordings_backend", None)
    if backend is None:
        return bool(getattr(client, "clip_playback_available", False))
    return bool(getattr(backend, "clip_playback_available", False))


def _raise_native_playback_unavailable(client: TuyaRecordingsClient) -> None:
    if getattr(client, "cloud_activity_paused", False):
        raise web.HTTPServiceUnavailable(reason="Tuya Recordings camera activity is paused")
    if not _uncached_playback_available(client):
        raise web.HTTPServiceUnavailable(reason="Tuya Recordings native playback is not available")


def _update_latest_clip(stats: dict[str, Any], dev_id: str, camera_name: str, start: int, end: int) -> None:
    latest = stats.get("latest_clip")
    if isinstance(latest, dict) and int(latest.get("start") or 0) >= start:
        return
    stats["latest_clip"] = {
        "dev_id": dev_id,
        "camera_name": camera_name,
        "start": start,
        "end": end,
        "duration": max(0, end - start),
    }


def _add_storage_stats(
    stats: dict[str, Any],
    client: TuyaRecordingsClient,
    video_files: dict[Path, int] | None = None,
    thumbnail_files: dict[Path, int] | None = None,
) -> None:
    media_storage_path = getattr(client, "media_storage_path", None)
    if not media_storage_path:
        return
    root = Path(media_storage_path)
    if video_files is None:
        video_count, video_bytes = _folder_stats(root / "videos", "*.mp4")
    else:
        video_count, video_bytes = len(video_files), sum(video_files.values())
    if thumbnail_files is None:
        thumbnail_count, thumbnail_bytes = _folder_stats(root / "thumbs", "*.jpg")
    else:
        thumbnail_count, thumbnail_bytes = len(thumbnail_files), sum(thumbnail_files.values())
    stats["video_files"] = video_count
    stats["thumbnail_files"] = thumbnail_count
    stats["video_bytes"] = video_bytes
    stats["thumbnail_bytes"] = thumbnail_bytes
    stats["total_bytes"] = video_bytes + thumbnail_bytes


def _ready_file_sizes(folder: Path, pattern: str) -> dict[Path, int]:
    """List ready media files once instead of statting every catalog row."""
    if not folder.exists():
        return {}
    ready: dict[Path, int] = {}
    for path in folder.glob(pattern):
        if not path.is_file():
            continue
        size = _file_size(path)
        if size > 0:
            ready[path] = size
    return ready


def _add_sync_stats(stats: dict[str, Any], client: TuyaRecordingsClient) -> None:
    diagnostics = {}
    if hasattr(client, "diagnostics"):
        try:
            diagnostics = client.diagnostics()
        except Exception:  # pragma: no cover - defensive status only
            diagnostics = {}
    sync = diagnostics.get("media_sync_status") if isinstance(diagnostics, dict) else {}
    if isinstance(sync, dict):
        stats["sync"] = sync


def _folder_stats(folder: Path, pattern: str) -> tuple[int, int]:
    if not folder.exists():
        return 0, 0
    count = 0
    size = 0
    for path in folder.glob(pattern):
        if not path.is_file():
            continue
        count += 1
        size += _file_size(path)
    return count, size


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _path_ready(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def _clip_ready(client: TuyaRecordingsClient, dev_id: str, start: int, end: int, path: Path) -> bool:
    ready = getattr(client, "clip_ready", None)
    if callable(ready):
        return bool(ready(dev_id, start, end))
    cached = getattr(client, "clip_cached", None)
    if callable(cached):
        return bool(cached(dev_id, start, end))
    return _path_ready(path)


def _local_media_url(path: Path, media_root: Path) -> str:
    try:
        relative = path.resolve().relative_to(media_root.resolve())
    except ValueError:
        return ""
    return f"/media/local/{quote(relative.as_posix())}"


def _timeline_socket_url(dev_id: str) -> str:
    return f"/api/{DOMAIN}/timeline/{quote(dev_id)}"


def _timeline_range_for_time(index: dict[str, Any], dev_id: str, position: int) -> tuple[int, int] | None:
    """Resolve a wall-clock position to a real catalog range for one camera."""
    ranges: list[tuple[int, int]] = []
    for camera in index.get("cameras", []):
        if camera.get("devId") != dev_id:
            continue
        for clip in camera.get("clips", []):
            start = int(clip.get("start") or 0)
            end = int(clip.get("end") or 0)
            if start > 0 and end > start:
                ranges.append((start, end))
        break
    ranges.sort()
    for start, end in ranges:
        if start <= position < end:
            return start, end
    for start, end in ranges:
        if start >= position:
            return start, end
    return ranges[-1] if ranges else None


def _pause_background_camera_work(
    hass: HomeAssistant,
    client: TuyaRecordingsClient,
    generation: int,
) -> None:
    """Suspend only camera-facing background work for native playback."""
    entry_map = hass.data.get(DOMAIN, {}) if hasattr(hass, "data") else {}
    retry_after = (
        float(hass.loop.time()) + THUMBNAIL_BACKGROUND_COOLDOWN
        if getattr(hass, "loop", None) is not None and hasattr(hass.loop, "time")
        else THUMBNAIL_BACKGROUND_COOLDOWN
    )
    for entry_data in entry_map.values():
        if not isinstance(entry_data, dict) or entry_data.get("client") is not client:
            continue
        entry_data[DATA_INTERACTIVE_PLAYBACK_ACTIVE] = generation
        entry_data[_DATA_MEDIA_SYNC_PENDING] = False
        entry_data[_DATA_THUMBNAIL_SYNC_PENDING] = False
        entry_data.pop(_DATA_THUMBNAIL_SYNC_LIMIT, None)
        entry_data.pop(_DATA_THUMBNAIL_SYNC_REQUIRE_MEDIA, None)
        entry_data.pop(_DATA_THUMBNAIL_SYNC_REFRESH_CATALOG, None)
        entry_data[_DATA_THUMBNAIL_SYNC_RETRY_AFTER] = retry_after
    client.pause_background_work()


def _resume_background_camera_work(
    hass: HomeAssistant,
    client: TuyaRecordingsClient,
    generation: int,
) -> None:
    """Resume background work only for the playback generation that owns it."""
    domain_data = hass.data.get(DOMAIN, {}) if hasattr(hass, "data") else {}
    if domain_data.get(_DATA_PLAYBACK_GENERATION) != generation:
        return
    for entry_data in domain_data.values():
        if not isinstance(entry_data, dict) or entry_data.get("client") is not client:
            continue
        if entry_data.get(DATA_INTERACTIVE_PLAYBACK_ACTIVE) == generation:
            entry_data.pop(DATA_INTERACTIVE_PLAYBACK_ACTIVE, None)
    client.resume_background_work()


def _clip_title(start: int, end: int) -> str:
    return f"{datetime.fromtimestamp(start):%H:%M:%S} - {datetime.fromtimestamp(end):%H:%M:%S}"


class TuyaRecordingsPanelDataView(http.HomeAssistantView):
    """Serve cache-backed data for the Tuya Recordings panel."""

    url = f"/api/{DOMAIN}/panel"
    name = f"api:{DOMAIN}:panel"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._catalog_refresh_tasks: dict[tuple[int, str, str], asyncio.Task[None]] = {}

    async def get(self, request: web.Request) -> web.Response:
        client = self._client()
        if client.background_work_paused:
            data = await self.hass.async_add_executor_job(self._build_data, client)
            data["on_demand"] = not client.media_sync_enabled and not client.thumbnail_sync_enabled
            data["selected_date"] = request.query.get("date", dt_util.now().date().isoformat())
        elif not client.media_sync_enabled and not client.thumbnail_sync_enabled:
            try:
                day = date.fromisoformat(request.query.get("date", dt_util.now().date().isoformat()))
            except ValueError as exc:
                raise web.HTTPBadRequest(reason="Invalid recording date") from exc
            dev_id = request.query.get("camera", "")
            index = await self.hass.async_add_executor_job(client.cached_camera_index)
            if dev_id and not any(camera.get("devId") == dev_id for camera in index.get("cameras", [])):
                raise web.HTTPNotFound(reason="Unknown camera")
            refresh_pending = self._schedule_catalog_refresh(client, dev_id, day, index)
            data = await self.hass.async_add_executor_job(build_panel_data, client, index)
            data["on_demand"] = True
            data["selected_date"] = day.isoformat()
            data["catalog_refresh_pending"] = refresh_pending
        else:
            data = await self.hass.async_add_executor_job(self._build_data, client)
        return web.json_response(data)

    def _schedule_catalog_refresh(
        self,
        client: TuyaRecordingsClient,
        dev_id: str,
        day: date,
        index: dict[str, Any],
    ) -> bool:
        """Run one request-driven catalog refresh without delaying panel paint."""
        target = dev_id
        if not target and index.get("cameras"):
            return False
        key = (id(client), target, day.isoformat())
        existing = self._catalog_refresh_tasks.get(key)
        if existing is not None and not existing.done():
            return True
        if not client.browse_refresh_due(target, day):
            return False

        async def _refresh() -> None:
            try:
                await self.hass.async_add_executor_job(client.browse_recordings, target, day)
            except (CameraWorkBusy, CameraWorkCancelled):
                _LOGGER.debug(
                    "Tuya Recordings catalog refresh deferred for %s on %s",
                    target or "camera discovery",
                    day,
                )
            except Exception:
                _LOGGER.exception(
                    "Tuya Recordings catalog refresh failed for %s on %s",
                    target or "camera discovery",
                    day,
                )
            finally:
                self._catalog_refresh_tasks.pop(key, None)

        self._catalog_refresh_tasks[key] = self.hass.async_create_task(_refresh())
        return True

    @staticmethod
    def _build_data(client: TuyaRecordingsClient) -> dict[str, Any]:
        return build_panel_data(client, client.cached_camera_index())

    def _client(self) -> TuyaRecordingsClient:
        for entry_data in self.hass.data.get(DOMAIN, {}).values():
            if isinstance(entry_data, dict) and isinstance(client := entry_data.get("client"), TuyaRecordingsClient):
                return client
        raise web.HTTPNotFound(reason="Tuya Recordings is not configured")


class TuyaRecordingsDebugView(http.HomeAssistantView):
    """Log frontend playback milestones without touching camera resources."""

    url = f"/api/{DOMAIN}/debug"
    name = f"api:{DOMAIN}:debug"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except ValueError:
            data = {}
        _LOGGER.warning(
            "Tuya Recordings frontend event phase=%s dev=%s start=%s end=%s detail=%s",
            data.get("phase"),
            data.get("dev_id"),
            data.get("start"),
            data.get("end"),
            data.get("detail"),
        )
        return web.json_response({"ok": True})


class TuyaRecordingsPlaybackView(http.HomeAssistantView):
    """Serve selected SD clips through the APK-native playback stream path."""

    url = f"/api/{DOMAIN}/play/{{dev_id}}/{{start}}/{{end}}"
    name = f"api:{DOMAIN}:play"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, dev_id: str, start: str, end: str) -> web.StreamResponse:
        try:
            start_int = int(start)
            end_int = int(end)
        except ValueError as exc:
            raise web.HTTPBadRequest(reason="Invalid recording time") from exc

        client = self._client()
        if start_int <= 0 or end_int <= start_int:
            raise web.HTTPBadRequest(reason="Invalid recording time range")
        playback_mode = request.query.get("mode", "clip")
        if playback_mode not in {"clip", "timeline"}:
            raise web.HTTPBadRequest(reason="Unsupported playback mode")
        play_time = None
        if "position" in request.query:
            try:
                play_time = int(request.query["position"])
            except (TypeError, ValueError) as exc:
                raise web.HTTPBadRequest(reason="Invalid playback position") from exc
            if playback_mode == "clip" and not start_int <= play_time < end_int:
                raise web.HTTPBadRequest(reason="Playback position is outside the recording")
        index = await self.hass.async_add_executor_job(client.cached_camera_index)
        if playback_mode == "timeline" and play_time is not None:
            resolved = _timeline_range_for_time(index, dev_id, play_time)
            if resolved is None:
                raise web.HTTPNotFound(reason="No recording is available at this time")
            start_int, end_int = resolved
            play_time = max(start_int, min(end_int - 1, play_time))
        clip_path = client.clip_path(dev_id, start_int, end_int)
        clip_cached = await self.hass.async_add_executor_job(
            _clip_ready,
            client,
            dev_id,
            start_int,
            end_int,
            clip_path,
        )
        # A locally cached clip never needs a camera session, even when an old
        # frontend has retained a native transport query parameter.
        transport = "file" if clip_cached and playback_mode == "clip" else (request.query.get("transport") or "native")
        _LOGGER.warning(
            "Tuya Recordings playback request dev=%s start=%s end=%s transport=%s cached=%s position=%s media_sync=%s paused=%s",
            dev_id,
            start_int,
            end_int,
            transport,
            clip_cached,
            play_time,
            client.media_sync_enabled,
            client.cloud_activity_paused,
        )
        if transport not in {"file", "native"}:
            raise web.HTTPBadRequest(reason="Unsupported Tuya Recordings playback transport")
        if not any(
            camera.get("devId") == dev_id
            and any(int(clip.get("start") or 0) == start_int and int(clip.get("end") or 0) == end_int for clip in camera.get("clips", []))
            for camera in index.get("cameras", [])
        ):
            raise web.HTTPNotFound(reason="Unknown recording")
        if transport == "file":
            if not clip_cached or not clip_path.exists() or clip_path.stat().st_size <= 0:
                raise web.HTTPNotFound(reason="Tuya recording is not cached")
            return web.FileResponse(
                clip_path,
                headers={
                    "Cache-Control": "private, max-age=3600",
                    "Content-Type": "video/mp4",
                },
            )
        _raise_native_playback_unavailable(client)
        domain_data = self.hass.data.setdefault(DOMAIN, {})
        lock = domain_data.setdefault(_DATA_PLAYBACK_LOCK, asyncio.Lock())
        generation = int(domain_data.get(_DATA_PLAYBACK_GENERATION, 0)) + 1
        domain_data[_DATA_PLAYBACK_GENERATION] = generation
        _pause_background_camera_work(self.hass, client, generation)
        active_stop = domain_data.get(_DATA_PLAYBACK_STOP)
        if isinstance(active_stop, threading.Event):
            active_stop.set()
            _LOGGER.debug(
                "Tuya Recordings handing active playback to dev=%s start=%s end=%s",
                dev_id,
                start_int,
                end_int,
            )
        try:
            await asyncio.wait_for(lock.acquire(), timeout=_PLAYBACK_HANDOFF_TIMEOUT)
        except TimeoutError as err:
            _resume_background_camera_work(self.hass, client, generation)
            raise web.HTTPServiceUnavailable(
                reason="Previous Tuya recording playback did not close cleanly"
            ) from err
        if generation != domain_data.get(_DATA_PLAYBACK_GENERATION):
            lock.release()
            raise web.HTTPConflict(reason="Playback request was replaced by a newer clip")
        stop_event = threading.Event()
        domain_data[_DATA_PLAYBACK_STOP] = stop_event
        try:
            return await self._stream_native_clip(
                request,
                client,
                dev_id,
                start_int,
                end_int,
                play_time,
                stop_event=stop_event,
            )
        except TuyaRecordingsAuthError as err:
            self._start_reauth(client)
            raise web.HTTPUnauthorized(
                reason="Smart Life camera playback authorization expired"
            ) from err
        except Exception:
            _LOGGER.exception(
                "Tuya Recordings APK-native playback stream failed dev=%s start=%s end=%s",
                dev_id,
                start_int,
                end_int,
            )
            raise
        finally:
            stop_event.set()
            if domain_data.get(_DATA_PLAYBACK_STOP) is stop_event:
                domain_data.pop(_DATA_PLAYBACK_STOP, None)
            lock.release()
            _resume_background_camera_work(self.hass, client, generation)

    def _client(self) -> TuyaRecordingsClient:
        for entry_data in self.hass.data.get(DOMAIN, {}).values():
            if isinstance(entry_data, dict) and isinstance(client := entry_data.get("client"), TuyaRecordingsClient):
                return client
        raise web.HTTPNotFound(reason="Tuya Recordings is not configured")

    def _start_reauth(self, client: TuyaRecordingsClient) -> None:
        for entry_data in self.hass.data.get(DOMAIN, {}).values():
            if not isinstance(entry_data, dict) or entry_data.get("client") is not client:
                continue
            entry = entry_data.get("entry")
            if entry is not None:
                entry.async_start_reauth(self.hass)
            return

    async def _stream_native_clip(
        self,
        request: web.Request,
        client: TuyaRecordingsClient,
        dev_id: str,
        start: int,
        end: int,
        play_time: int | None,
        *,
        stop_event: threading.Event,
    ) -> web.StreamResponse:
        chunks: queue.Queue[bytes | Exception | object] = queue.Queue(maxsize=8)
        sentinel = object()

        def put_chunk(chunk: bytes) -> None:
            while not stop_event.is_set():
                try:
                    chunks.put(chunk, timeout=0.25)
                    return
                except queue.Full:
                    continue
            raise CameraWorkCancelled("Browser ended Tuya recording playback")

        def put_result(item: Exception | object) -> None:
            while not stop_event.is_set():
                try:
                    chunks.put(item, timeout=0.25)
                    return
                except queue.Full:
                    continue

        def produce() -> None:
            try:
                client.stream_clip(
                    dev_id,
                    start,
                    end,
                    put_chunk,
                    play_time=play_time,
                    cancel_event=stop_event,
                )
            except Exception as err:  # noqa: BLE001 - executor boundary transports failures
                if not stop_event.is_set():
                    put_result(err)
            finally:
                put_result(sentinel)

        producer = asyncio.ensure_future(self.hass.async_add_executor_job(produce))
        loop = asyncio.get_running_loop()
        first = await loop.run_in_executor(None, chunks.get)
        if first is sentinel:
            stop_event.set()
            await producer
            raise web.HTTPServiceUnavailable(reason="Tuya recording playback produced no video")
        if isinstance(first, Exception):
            stop_event.set()
            await producer
            raise first

        response = web.StreamResponse(
            status=200,
            headers={
                "Cache-Control": "no-store",
                "Content-Type": "application/vnd.tuya-recordings.stream",
                "Accept-Ranges": "none",
            },
        )
        await response.prepare(request)
        try:
            await response.write(first)
            while True:
                item = await loop.run_in_executor(None, chunks.get)
                if item is sentinel:
                    break
                if isinstance(item, Exception):
                    raise item
                await response.write(item)
        except (asyncio.CancelledError, ConnectionError):
            stop_event.set()
            raise
        except Exception:
            stop_event.set()
            _LOGGER.exception("Tuya Recordings native playback response failed dev=%s start=%s end=%s", dev_id, start, end)
            raise
        finally:
            stop_event.set()
            await producer
        await response.write_eof()
        return response


class TuyaRecordingsTimelineView(http.HomeAssistantView):
    """Expose one APK-style, bidirectional SD-card timeline session."""

    url = f"/api/{DOMAIN}/timeline/{{dev_id}}"
    name = f"api:{DOMAIN}:timeline"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, dev_id: str) -> web.WebSocketResponse:
        client = self._client()
        _raise_native_playback_unavailable(client)
        try:
            position = int(request.query["position"])
        except (KeyError, TypeError, ValueError) as err:
            raise web.HTTPBadRequest(reason="Invalid playback position") from err
        index = await self.hass.async_add_executor_job(client.cached_camera_index)
        resolved = _timeline_range_for_time(index, dev_id, position)
        if resolved is None:
            raise web.HTTPNotFound(reason="No recording is available at this time")
        start, end = resolved
        position = max(start, min(end - 1, position))
        initial = client.timeline_request(dev_id, start, end, position)
        _LOGGER.warning(
            "Tuya Recordings timeline playback requested dev=%s start=%s end=%s position=%s",
            dev_id,
            start,
            end,
            position,
        )

        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        domain_data = self.hass.data.setdefault(DOMAIN, {})
        lock = domain_data.setdefault(_DATA_PLAYBACK_LOCK, asyncio.Lock())
        generation = int(domain_data.get(_DATA_PLAYBACK_GENERATION, 0)) + 1
        domain_data[_DATA_PLAYBACK_GENERATION] = generation
        _pause_background_camera_work(self.hass, client, generation)
        active_stop = domain_data.get(_DATA_PLAYBACK_STOP)
        if isinstance(active_stop, threading.Event):
            active_stop.set()
        active_ws = domain_data.get(_DATA_PLAYBACK_WS)
        if isinstance(active_ws, web.WebSocketResponse) and not active_ws.closed:
            # Waking only the camera producer is insufficient: the superseded
            # request can remain blocked in its WebSocket receive loop and keep
            # the playback lock forever. Closing its socket lets that handler
            # run cleanup and release the lock before this session starts.
            await active_ws.close(
                code=WSCloseCode.GOING_AWAY,
                message=b"Playback moved to a newer request",
            )
        try:
            await asyncio.wait_for(lock.acquire(), timeout=_PLAYBACK_HANDOFF_TIMEOUT)
        except TimeoutError:
            _resume_background_camera_work(self.hass, client, generation)
            await ws.send_json({"type": "error", "message": "Previous playback did not close"})
            await ws.close()
            return ws

        stop_event = threading.Event()
        commands: queue.Queue[tuple[str, Any]] = queue.Queue()
        chunks: queue.Queue[bytes | Exception | object] = queue.Queue(maxsize=8)
        sentinel = object()
        domain_data[_DATA_PLAYBACK_STOP] = stop_event
        domain_data[_DATA_PLAYBACK_WS] = ws

        def put_chunk(chunk: bytes) -> None:
            while not stop_event.is_set():
                try:
                    chunks.put(chunk, timeout=0.25)
                    return
                except queue.Full:
                    continue
            raise CameraWorkCancelled("Browser ended Tuya timeline playback")

        def put_result(item: Exception | object) -> None:
            while True:
                try:
                    chunks.put(item, timeout=0.25)
                    return
                except queue.Full:
                    if stop_event.is_set():
                        try:
                            chunks.get_nowait()
                        except queue.Empty:
                            pass

        def produce() -> None:
            try:
                client.stream_timeline(
                    dev_id,
                    initial,
                    commands,
                    put_chunk,
                    cancel_event=stop_event,
                )
            except Exception as err:  # noqa: BLE001 - executor handoff
                if not stop_event.is_set():
                    put_result(err)
            finally:
                put_result(sentinel)

        async def send_chunks() -> None:
            loop = asyncio.get_running_loop()
            try:
                while True:
                    item = await loop.run_in_executor(None, chunks.get)
                    if item is sentinel:
                        return
                    if isinstance(item, Exception):
                        _LOGGER.error(
                            "Tuya Recordings timeline playback failed dev=%s",
                            dev_id,
                            exc_info=(type(item), item, item.__traceback__),
                        )
                        await ws.send_json({"type": "error", "message": str(item)})
                        return
                    await ws.send_bytes(item)
            finally:
                # Producer completion must also wake the WebSocket receive
                # loop, otherwise the handler retains the playback lock.
                if not ws.closed:
                    await ws.close()

        producer = asyncio.ensure_future(self.hass.async_add_executor_job(produce))
        sender = self.hass.async_create_task(send_chunks())
        try:
            await ws.send_json({"type": "ready", "position": position})
            async for message in ws:
                if message.type != web.WSMsgType.TEXT:
                    if message.type in {
                        web.WSMsgType.CLOSE,
                        web.WSMsgType.CLOSING,
                        web.WSMsgType.CLOSED,
                        web.WSMsgType.ERROR,
                    }:
                        break
                    continue
                try:
                    payload = message.json()
                except ValueError:
                    await ws.send_json({"type": "error", "message": "Invalid command"})
                    continue
                action = str(payload.get("action") or "")
                if action in {"pause", "resume"}:
                    commands.put((action, None))
                    continue
                if action != "seek":
                    await ws.send_json({"type": "error", "message": "Invalid command"})
                    continue
                try:
                    wanted = int(payload["position"])
                except (KeyError, TypeError, ValueError):
                    await ws.send_json({"type": "error", "message": "Invalid position"})
                    continue
                resolved = _timeline_range_for_time(index, dev_id, wanted)
                if resolved is None:
                    await ws.send_json({"type": "error", "message": "No recording at that time"})
                    continue
                start, end = resolved
                wanted = max(start, min(end - 1, wanted))
                commands.put(
                    ("seek", client.timeline_request(dev_id, start, end, wanted))
                )
                await ws.send_json({"type": "seeking", "position": wanted})
        finally:
            stop_event.set()
            commands.put(("stop", None))
            await asyncio.gather(producer, sender, return_exceptions=True)
            if domain_data.get(_DATA_PLAYBACK_STOP) is stop_event:
                domain_data.pop(_DATA_PLAYBACK_STOP, None)
            if domain_data.get(_DATA_PLAYBACK_WS) is ws:
                domain_data.pop(_DATA_PLAYBACK_WS, None)
            lock.release()
            _resume_background_camera_work(self.hass, client, generation)
            if not ws.closed:
                await ws.close()
        return ws

    def _client(self) -> TuyaRecordingsClient:
        for entry_data in self.hass.data.get(DOMAIN, {}).values():
            if isinstance(entry_data, dict) and isinstance(
                client := entry_data.get("client"), TuyaRecordingsClient
            ):
                return client
        raise web.HTTPNotFound(reason="Tuya Recordings is not configured")

class TuyaRecordingsThumbnailView(http.HomeAssistantView):
    """Serve generated Tuya recording thumbnails from the private cache."""

    url = f"/api/{DOMAIN}/thumb/{{dev_id}}/{{start}}/{{end}}"
    name = f"api:{DOMAIN}:thumb"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, dev_id: str, start: str, end: str) -> web.FileResponse:
        try:
            start_int = int(start)
            end_int = int(end)
        except ValueError as exc:
            raise web.HTTPBadRequest(reason="Invalid recording time") from exc

        client = self._client()
        thumbnail_path = client.thumbnail_path(dev_id, start_int, end_int)
        if not getattr(client, "media_sync_enabled", False):
            raise web.HTTPNotFound(reason="Tuya recording thumbnails are available only in local cache mode")
        if not thumbnail_path.exists() or thumbnail_path.stat().st_size <= 0:
            raise web.HTTPNotFound(reason="Tuya recording thumbnail is not cached yet")
        return web.FileResponse(thumbnail_path, headers={"Cache-Control": "private, max-age=3600"})

    def _client(self) -> TuyaRecordingsClient:
        for entry_data in self.hass.data.get(DOMAIN, {}).values():
            if isinstance(entry_data, dict) and isinstance(client := entry_data.get("client"), TuyaRecordingsClient):
                return client
        raise web.HTTPNotFound(reason="Tuya Recordings is not configured")
