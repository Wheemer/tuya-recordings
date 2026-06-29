from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source import BrowseMediaSource, MediaSource, MediaSourceItem, PlayMedia
from homeassistant.components.media_source.error import Unresolvable
from homeassistant.core import HomeAssistant

from .const import DOMAIN, NAME


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    return TuyaRecordingsMediaSource(hass)


def build_identifier(params: dict[str, str] | None = None) -> str:
    if params is None:
        return DOMAIN
    return f"{DOMAIN}/?{urlencode(params)}"


def parse_identifier(identifier: str) -> dict[str, str]:
    return dict(parse_qsl(urlparse(identifier).query, keep_blank_values=True))


class TuyaRecordingsMediaSource(MediaSource):
    name = NAME

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        index = await self._async_load_index()
        identifier = item.identifier or ""
        cameras = index.get("cameras", [])
        client = self._client() if cameras else None

        if not identifier:
            children = []
            if client is not None:
                for camera in cameras:
                    if not camera.get("devId"):
                        continue
                    has_visible = await self.hass.async_add_executor_job(self._camera_has_visible_clips, client, camera)
                    if has_visible:
                        children.append(
                            self._directory(
                                build_identifier({"dev_id": camera["devId"]}),
                                camera.get("name") or camera["devId"],
                            )
                        )
            return self._root(children, index)

        query = parse_identifier(identifier)
        dev_id = query.get("dev_id", "")
        clip_date = query.get("date")
        start = query.get("start")
        end = query.get("end")

        if dev_id and not clip_date and not start and not end:
            camera = self._find_camera(cameras, dev_id)
            if camera is None:
                raise Unresolvable(f"Unknown camera: {dev_id}")
            if client is None:
                raise Unresolvable(f"{NAME} is not configured.")
            visible_clips = await self.hass.async_add_executor_job(self._visible_clips, client, camera)
            dates = sorted({clip.get("date") for clip in visible_clips if clip.get("date")}, reverse=True)
            status = camera.get("bridgeStatus") or {}
            children = [
                self._directory(build_identifier({"dev_id": dev_id, "date": date_value}), date_value)
                for date_value in dates
            ]
            if not children:
                message = status.get("error") or "No Tuya recordings found yet."
                children = [self._directory(build_identifier({"dev_id": dev_id, "empty": "1"}), message, can_expand=False)]
            return self._directory_node(
                identifier=identifier,
                title=camera.get("name") or dev_id,
                children=children,
            )

        if dev_id and clip_date and not start and not end:
            camera = self._find_camera(cameras, dev_id)
            if camera is None:
                raise Unresolvable(f"Unknown camera: {dev_id}")
            if client is None:
                raise Unresolvable(f"{NAME} is not configured.")
            visible_clips = await self.hass.async_add_executor_job(self._visible_clips, client, camera)
            clips = [clip for clip in visible_clips if clip.get("date") == clip_date]
            use_12h = await self._async_use_12h_time()
            clips.sort(
                key=lambda clip: int(clip.get("start") or 0),
                reverse=client.media_view_recordings_order != "Ascending",
            )
            children = [
                self._clip_node(
                    client=client,
                    clip=clip,
                    dev_id=dev_id,
                    clip_date=clip_date,
                    start=int(clip.get("start") or 0),
                    end=int(clip.get("end") or 0),
                    title=self._clip_title(
                        int(clip.get("start") or 0),
                        int(clip.get("end") or 0),
                        use_12h=use_12h,
                    ),
                )
                for clip in clips
            ]
            self._schedule_thumbnail_autofill(client, dev_id, clips)
            return self._directory_node(identifier=identifier, title=clip_date, children=children)

        if dev_id and start and end:
            use_12h = await self._async_use_12h_time()
            return BrowseMediaSource(
                domain=DOMAIN,
                identifier=identifier,
                media_class=MediaClass.VIDEO,
                media_content_type="video/mp4",
                title=self._clip_title(int(start), int(end), use_12h=use_12h),
                can_play=True,
                can_expand=False,
                children=[],
                thumbnail=None,
            )

        if query.get("empty"):
            return self._directory_node(identifier=identifier, title="No clips", children=[])

        raise Unresolvable(f"Unknown Tuya Recordings media identifier: {identifier}")

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        query = parse_identifier(item.identifier or "")
        dev_id = query.get("dev_id", "")
        try:
            start = int(query.get("start") or "")
            end = int(query.get("end") or "")
        except ValueError as exc:
            raise Unresolvable("Invalid Tuya recording clip identifier") from exc
        if not dev_id or not start or not end:
            raise Unresolvable("Invalid Tuya recording clip identifier")

        client = self._client()
        try:
            playback_url = await self.hass.async_add_executor_job(
                self._resolve_clip_playback_url,
                client,
                dev_id,
                start,
                end,
            )
        except Exception as exc:
            raise Unresolvable(f"Tuya recording is not playable yet: {exc}") from exc
        return PlayMedia(playback_url, "video/mp4")

    async def _async_load_index(self) -> dict[str, Any]:
        entries = self.hass.data.get(DOMAIN, {})
        for entry_data in entries.values():
            if isinstance(entry_data, dict) and (client := entry_data.get("client")):
                return await self.hass.async_add_executor_job(client.cached_camera_index)
        return {"warning": f"{NAME} is not configured.", "cameras": []}

    def _client(self) -> Any:
        entries = self.hass.data.get(DOMAIN, {})
        for entry_data in entries.values():
            if isinstance(entry_data, dict) and (client := entry_data.get("client")):
                return client
        raise Unresolvable(f"{NAME} is not configured.")

    def _schedule_thumbnail_autofill(self, client: Any, dev_id: str, clips: list[dict[str, Any]]) -> None:
        async def _run() -> None:
            await self.hass.async_add_executor_job(client.populate_thumbnails_for_clips, dev_id, clips)

        self.hass.async_create_task(_run())

    async def _async_use_12h_time(self) -> bool:
        return await self.hass.async_add_executor_job(self._use_12h_time)

    def _use_12h_time(self) -> bool:
        storage = self._storage_path()
        explicit: bool | None = None
        language: str | None = None
        if storage is not None:
            for path in sorted(storage.glob("frontend.user_data_*")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                language_data = data.get("data", {}).get("language", {})
                if not isinstance(language_data, dict):
                    continue
                time_format = str(language_data.get("time_format") or "").lower()
                if time_format in {"12", "12h", "12_hour", "12-hour"}:
                    explicit = True
                    break
                if time_format in {"24", "24h", "24_hour", "24-hour"}:
                    explicit = False
                    break
                if language is None:
                    language = str(language_data.get("language") or "").lower()
        if explicit is not None:
            return explicit
        return self._language_uses_12h_time(language)

    def _storage_path(self) -> Path | None:
        try:
            return Path(self.hass.config.path(".storage"))
        except (AttributeError, TypeError):
            return None

    @staticmethod
    def _language_uses_12h_time(language: str | None) -> bool:
        if not language:
            return False
        normalized = language.replace("_", "-").lower()
        if normalized in {"en-gb", "en-ie", "en-au", "en-nz", "en-za"}:
            return False
        if normalized == "en" or normalized.startswith(("en-us", "en-ca")):
            return True
        return False

    def _clip_title(self, start: int, end: int, *, use_12h: bool) -> str:
        try:
            time_zone = ZoneInfo(str(self.hass.config.time_zone))
        except (AttributeError, ZoneInfoNotFoundError, ValueError):
            time_zone = None
        start_dt = datetime.fromtimestamp(start, tz=time_zone)
        end_dt = datetime.fromtimestamp(end, tz=time_zone)
        return f"{self._format_time(start_dt, use_12h)} - {self._format_time(end_dt, use_12h)}"

    @staticmethod
    def _format_time(value: datetime, use_12h: bool) -> str:
        if not use_12h:
            return value.strftime("%H:%M:%S")
        return value.strftime("%I:%M:%S %p").lstrip("0")

    @staticmethod
    def _find_camera(cameras: list[dict[str, Any]], dev_id: str) -> dict[str, Any] | None:
        return next((camera for camera in cameras if camera.get("devId") == dev_id), None)

    @staticmethod
    def _clip_url(path: Path) -> str:
        return TuyaRecordingsMediaSource._clip_url_name(path.name)

    @staticmethod
    def _clip_url_name(name: str) -> str:
        return f"/media/local/{DOMAIN}/videos/{quote(name)}"

    def _cached_clip_playback_url(self, client: Any, dev_id: str, start: int, end: int) -> str | None:
        clip_path = client.clip_path(dev_id, start, end)
        if hasattr(client, "clip_cached") and not client.clip_cached(dev_id, start, end):
            return None
        if not hasattr(client, "clip_cached") and not self._path_ready(clip_path):
            return None
        return self._local_media_url(clip_path)

    def _require_cached_clip_playback_url(self, client: Any, dev_id: str, start: int, end: int) -> str:
        clip_path = client.clip_path(dev_id, start, end)
        clip_cached = client.clip_cached(dev_id, start, end) if hasattr(client, "clip_cached") else self._path_ready(clip_path)
        if not clip_cached:
            raise RuntimeError("clip is not cached")
        playback_url = self._local_media_url(clip_path)
        if not playback_url:
            raise RuntimeError("clip storage path must be under /media for Home Assistant Media Browser playback")
        return playback_url

    def _resolve_clip_playback_url(self, client: Any, dev_id: str, start: int, end: int) -> str:
        clip_path = client.clip_path(dev_id, start, end)
        clip_cached = client.clip_cached(dev_id, start, end) if hasattr(client, "clip_cached") else self._path_ready(clip_path)
        if not clip_cached:
            if self._requires_ready_cache(client):
                raise RuntimeError("clip is not cached")
            client.download_clip(dev_id, start, end, clip_path)
        return self._require_cached_clip_playback_url(client, dev_id, start, end)

    def _visible_clips(self, client: Any, camera: dict[str, Any]) -> list[dict[str, Any]]:
        visible: list[dict[str, Any]] = []
        dev_id = str(camera.get("devId") or "")
        if not dev_id:
            return visible
        cache_only = self._requires_ready_cache(client)
        for clip in camera.get("clips", []):
            start = int(clip.get("start") or 0)
            end = int(clip.get("end") or 0)
            if not start or not end:
                continue
            if cache_only:
                clip_cached = client.clip_cached(dev_id, start, end) if hasattr(client, "clip_cached") else self._path_ready(client.clip_path(dev_id, start, end))
                thumbnail_cached = self._path_ready(client.thumbnail_path(dev_id, start, end))
                if not clip_cached or not thumbnail_cached:
                    continue
            visible.append(clip)
        return visible

    def _camera_has_visible_clips(self, client: Any, camera: dict[str, Any]) -> bool:
        return bool(self._visible_clips(client, camera))

    @staticmethod
    def _requires_ready_cache(client: Any) -> bool:
        return bool(getattr(client, "media_sync_enabled", False))

    @staticmethod
    def _path_ready(path: Path) -> bool:
        try:
            return path.exists() and path.stat().st_size > 0
        except OSError:
            return False

    @staticmethod
    def _thumbnail_url_name(name: str) -> str:
        return f"/media/local/{DOMAIN}/thumbs/{quote(name)}"

    @staticmethod
    def _thumbnail_url_for_clip(dev_id: str, start: int, end: int) -> str:
        return f"/api/{DOMAIN}/thumb/{quote(dev_id)}/{start}/{end}"

    def _thumbnail_url(self, client: Any, clip: dict[str, Any], dev_id: str, start: int, end: int) -> str | None:
        live_thumbnail = str(clip.get("thumbnail") or "").strip()
        if not live_thumbnail and isinstance(raw := clip.get("raw"), dict):
            live_thumbnail = self._raw_thumbnail_url(raw)
        if live_thumbnail:
            return self._absolute_url(client, live_thumbnail)

        thumbnail_path = client.thumbnail_path(dev_id, start, end)
        if thumbnail_path.exists() and thumbnail_path.stat().st_size > 0:
            return self._thumbnail_url_for_clip(dev_id, start, end)
        return None

    @staticmethod
    def _local_media_url(path: Path) -> str | None:
        try:
            relative = path.resolve().relative_to(Path("/media").resolve())
        except ValueError:
            return None
        return f"/media/local/{quote(relative.as_posix())}"

    @staticmethod
    def _raw_thumbnail_url(raw: dict[str, Any]) -> str:
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
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key, value in raw.items():
            text = str(key).lower()
            if any(fragment in text for fragment in ("thumb", "snapshot", "cover", "poster", "preview", "image", "pic")):
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    @staticmethod
    def _absolute_url(client: Any, url: str) -> str:
        if url.startswith(("http://", "https://", "/media/")):
            return url
        return url

    def _clip_node(self, client: Any, clip: dict[str, Any], dev_id: str, clip_date: str, start: int, end: int, title: str) -> BrowseMediaSource:
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=build_identifier(
                {
                    "dev_id": dev_id,
                    "date": clip_date,
                    "start": str(start),
                    "end": str(end),
                }
            ),
            media_class=MediaClass.VIDEO,
            media_content_type="video/mp4",
            title=title,
            can_play=True,
            can_expand=False,
            children=[],
            thumbnail=self._thumbnail_url(client, clip, dev_id, start, end),
        )

    def _root(self, children: list[BrowseMediaSource], index: dict[str, Any]) -> BrowseMediaSource:
        if not children and index.get("warning"):
            children = [self._directory(build_identifier({"warning": "1"}), index["warning"], can_expand=False)]
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=None,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title=self.name,
            can_play=False,
            can_expand=True,
            children=children,
        )

    def _directory(self, identifier: str, title: str, can_expand: bool = True) -> BrowseMediaSource:
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title=title,
            can_play=False,
            can_expand=can_expand,
            children=[],
        )

    def _directory_node(
        self,
        identifier: str,
        title: str,
        children: list[BrowseMediaSource],
    ) -> BrowseMediaSource:
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title=title,
            can_play=False,
            can_expand=True,
            children=children,
        )


def _safe_segment(value: str) -> str:
    return "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in value)
