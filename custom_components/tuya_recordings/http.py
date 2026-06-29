from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aiohttp import web

from homeassistant.components import http
from homeassistant.core import HomeAssistant

from .client import TuyaRecordingsClient
from .const import DOMAIN, NAME


def build_panel_data(client: TuyaRecordingsClient, index: dict[str, Any], media_root: Path = Path("/media")) -> dict[str, Any]:
    """Build cache-backed panel data from the recording index."""
    cameras: list[dict[str, Any]] = []
    stats: dict[str, Any] = _empty_stats(client)
    cache_only = bool(getattr(client, "media_sync_enabled", False))
    for camera in index.get("cameras", []):
        dev_id = str(camera.get("devId") or "")
        if not dev_id:
            continue
        stats["total_cameras"] += 1
        if camera.get("online"):
            stats["online_cameras"] += 1
        clips = []
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
            clip_cached = client.clip_cached(dev_id, start, end) if hasattr(client, "clip_cached") else _path_ready(clip_path)
            thumbnail_cached = _path_ready(thumbnail_path)
            if clip_cached:
                stats["cached_videos"] += 1
                camera_stats["cached_videos"] += 1
                size = _file_size(clip_path)
                camera_stats["video_bytes"] += size
            if thumbnail_cached:
                stats["cached_thumbnails"] += 1
                camera_stats["cached_thumbnails"] += 1
                size = _file_size(thumbnail_path)
                camera_stats["thumbnail_bytes"] += size
            ready = clip_cached and thumbnail_cached
            if ready:
                stats["ready_clips"] += 1
                camera_stats["ready_clips"] += 1
            if cache_only and not ready:
                continue
            dates.add(clip_date)
            stats["visible_clips"] += 1
            _update_latest_clip(stats, dev_id, str(camera.get("name") or dev_id), start, end)
            clips.append(
                {
                    "dev_id": dev_id,
                    "date": clip_date,
                    "start": start,
                    "end": end,
                    "duration": max(0, end - start),
                    "title": str(clip.get("title") or _clip_title(start, end)),
                    "cached": clip_cached,
                    "thumbnail_cached": thumbnail_cached,
                    "playback_url": _local_media_url(clip_path, media_root) if clip_cached else _playback_api_url(dev_id, start, end),
                    "thumbnail_url": _local_media_url(thumbnail_path, media_root) if thumbnail_cached else "",
                }
            )
        camera_stats["pending_clips"] = max(0, camera_stats["indexed_clips"] - camera_stats["ready_clips"])
        stats["pending_clips"] += camera_stats["pending_clips"]
        stats["camera_stats"].append(camera_stats)
        clips.sort(key=lambda item: int(item["start"]), reverse=True)
        cameras.append(
            {
                "dev_id": dev_id,
                "name": str(camera.get("name") or dev_id),
                "online": bool(camera.get("online")),
                "dates": sorted(dates, reverse=True),
                "clips": clips,
            }
        )
    _add_storage_stats(stats, client)
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
    }


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


def _add_storage_stats(stats: dict[str, Any], client: TuyaRecordingsClient) -> None:
    media_storage_path = getattr(client, "media_storage_path", None)
    if not media_storage_path:
        return
    root = Path(media_storage_path)
    video_files, video_bytes = _folder_stats(root / "videos", "*.mp4")
    thumbnail_files, thumbnail_bytes = _folder_stats(root / "thumbs", "*.jpg")
    stats["video_files"] = video_files
    stats["thumbnail_files"] = thumbnail_files
    stats["video_bytes"] = video_bytes
    stats["thumbnail_bytes"] = thumbnail_bytes
    stats["total_bytes"] = video_bytes + thumbnail_bytes


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


def _local_media_url(path: Path, media_root: Path) -> str:
    try:
        relative = path.resolve().relative_to(media_root.resolve())
    except ValueError:
        return ""
    return f"/media/local/{quote(relative.as_posix())}"


def _playback_api_url(dev_id: str, start: int, end: int) -> str:
    return f"/api/{DOMAIN}/play/{quote(dev_id)}/{start}/{end}"


def _clip_title(start: int, end: int) -> str:
    return f"{datetime.fromtimestamp(start):%H:%M:%S} - {datetime.fromtimestamp(end):%H:%M:%S}"


class TuyaRecordingsPanelDataView(http.HomeAssistantView):
    """Serve cache-backed data for the Tuya Recordings panel."""

    url = f"/api/{DOMAIN}/panel"
    name = f"api:{DOMAIN}:panel"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        client = self._client()
        data = await self.hass.async_add_executor_job(self._build_data, client)
        return web.json_response(data)

    @staticmethod
    def _build_data(client: TuyaRecordingsClient) -> dict[str, Any]:
        return build_panel_data(client, client.cached_camera_index())

    def _client(self) -> TuyaRecordingsClient:
        for entry_data in self.hass.data.get(DOMAIN, {}).values():
            if isinstance(entry_data, dict) and isinstance(client := entry_data.get("client"), TuyaRecordingsClient):
                return client
        raise web.HTTPNotFound(reason="Tuya Recordings is not configured")


class TuyaRecordingsPlaybackView(http.HomeAssistantView):
    """Serve Tuya recording playback from the private media cache."""

    url = f"/api/{DOMAIN}/play/{{dev_id}}/{{start}}/{{end}}"
    name = f"api:{DOMAIN}:play"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, dev_id: str, start: str, end: str) -> web.FileResponse:
        try:
            start_int = int(start)
            end_int = int(end)
        except ValueError as exc:
            raise web.HTTPBadRequest(reason="Invalid recording time") from exc

        client = self._client()
        output_path = client.clip_path(dev_id, start_int, end_int)
        clip_cached = client.clip_cached(dev_id, start_int, end_int) if hasattr(client, "clip_cached") else _path_ready(output_path)
        if not clip_cached:
            if getattr(client, "media_sync_enabled", False):
                raise web.HTTPNotFound(reason="Tuya recording is not cached")
            await self.hass.async_add_executor_job(client.download_clip, dev_id, start_int, end_int, output_path)
            clip_cached = client.clip_cached(dev_id, start_int, end_int) if hasattr(client, "clip_cached") else _path_ready(output_path)
            if not clip_cached:
                raise web.HTTPNotFound(reason="Tuya recording could not be cached")
        self._schedule_thumbnail(client, dev_id, start_int, end_int)
        return web.FileResponse(output_path)

    def _client(self) -> TuyaRecordingsClient:
        for entry_data in self.hass.data.get(DOMAIN, {}).values():
            if isinstance(entry_data, dict) and isinstance(client := entry_data.get("client"), TuyaRecordingsClient):
                return client
        raise web.HTTPNotFound(reason="Tuya Recordings is not configured")

    def _schedule_thumbnail(self, client: TuyaRecordingsClient, dev_id: str, start: int, end: int) -> None:
        async def _run() -> None:
            await self.hass.async_add_executor_job(client.ensure_thumbnail, dev_id, start, end)

        self.hass.async_create_task(_run())

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
        if not thumbnail_path.exists() or thumbnail_path.stat().st_size <= 0:
            await self.hass.async_add_executor_job(client.ensure_thumbnail, dev_id, start_int, end_int)
        if not thumbnail_path.exists() or thumbnail_path.stat().st_size <= 0:
            raise web.HTTPNotFound(reason="Tuya recording thumbnail is not cached yet")
        return web.FileResponse(thumbnail_path, headers={"Cache-Control": "private, max-age=3600"})

    def _client(self) -> TuyaRecordingsClient:
        for entry_data in self.hass.data.get(DOMAIN, {}).values():
            if isinstance(entry_data, dict) and isinstance(client := entry_data.get("client"), TuyaRecordingsClient):
                return client
        raise web.HTTPNotFound(reason="Tuya Recordings is not configured")
