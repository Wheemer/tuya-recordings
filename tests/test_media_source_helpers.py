from custom_components.tuya_recordings.const import DOMAIN
from custom_components.tuya_recordings.config_flow import _validate_media_storage_path
from custom_components.tuya_recordings.media_source import TuyaRecordingsMediaSource, build_identifier, parse_identifier
import pytest


class StubClient:
    media_sync_enabled = False

    def thumbnail_path(self, dev_id, start, end):
        return DummyPath()


class DummyPath:
    name = "thumb.jpg"

    def exists(self):
        return False

    def stat(self):
        raise AssertionError("stat should not be called when thumbnail is missing")


class DummyCachedPath:
    name = "cam_10_20.mp4"

    def exists(self):
        return True

    def stat(self):
        class Stat:
            st_size = 100

        return Stat()


class CachedClipClient(StubClient):
    def clip_path(self, dev_id, start, end):
        return DummyCachedPath()


class MissingThenCachedPath(DummyCachedPath):
    def __init__(self):
        self.cached = False

    def exists(self):
        return self.cached


class DownloadingClipClient(StubClient):
    def __init__(self):
        self.path = MissingThenCachedPath()
        self.downloaded = []

    def clip_path(self, dev_id, start, end):
        return self.path

    def download_clip(self, dev_id, start, end, output_path):
        self.downloaded.append((dev_id, start, end, output_path))
        self.path.cached = True
        return output_path


class FailedDownloadClient(DownloadingClipClient):
    def download_clip(self, dev_id, start, end, output_path):
        self.downloaded.append((dev_id, start, end, output_path))
        return output_path


def test_build_identifier_root():
    assert build_identifier() == DOMAIN


def test_build_identifier_round_trips_query_values():
    identifier = build_identifier({"dev_id": "abc123", "date": "2026-06-26"})

    assert identifier.startswith(f"{DOMAIN}/?")
    assert parse_identifier(identifier) == {"dev_id": "abc123", "date": "2026-06-26"}


def test_root_warning_uses_query_identifier():
    source = TuyaRecordingsMediaSource(hass=None)
    root = source._root([], {"warning": "No cache"})

    assert root.children[0].identifier == build_identifier({"warning": "1"})


def test_clip_node_is_playable():
    source = TuyaRecordingsMediaSource(hass=None)
    node = source._directory_node(
        build_identifier({"dev_id": "abc", "date": "2026-06-27"}),
        "2026-06-27",
        [
            source._clip_node(
                client=StubClient(),
                clip={},
                dev_id="abc",
                clip_date="2026-06-27",
                start=1782598641,
                end=1782598677,
                title="clip",
            )
        ],
    )

    assert node.children[0].can_play is True
    assert node.children[0].can_expand is False


def test_safe_clip_url_uses_local_media_path():
    source = TuyaRecordingsMediaSource(hass=None)

    assert source._clip_url_name("cam 1_10_20.mp4") == f"/media/local/{DOMAIN}/videos/cam%201_10_20.mp4"


def test_cached_clip_playback_url_prefers_local_media(monkeypatch):
    source = TuyaRecordingsMediaSource(hass=None)
    monkeypatch.setattr(source, "_local_media_url", lambda path: f"/media/local/{DOMAIN}/videos/{path.name}")

    assert source._cached_clip_playback_url(CachedClipClient(), "cam", 10, 20) == f"/media/local/{DOMAIN}/videos/cam_10_20.mp4"


def test_require_cached_clip_playback_url_returns_cached_local_media(monkeypatch):
    source = TuyaRecordingsMediaSource(hass=None)
    client = DownloadingClipClient()
    client.path.cached = True
    monkeypatch.setattr(source, "_local_media_url", lambda path: f"/media/local/{DOMAIN}/videos/{path.name}" if path.exists() else None)

    assert source._require_cached_clip_playback_url(client, "cam", 10, 20) == f"/media/local/{DOMAIN}/videos/cam_10_20.mp4"
    assert client.downloaded == []


def test_require_cached_clip_playback_url_rejects_uncached_clip(monkeypatch):
    source = TuyaRecordingsMediaSource(hass=None)
    client = FailedDownloadClient()
    monkeypatch.setattr(source, "_local_media_url", lambda path: f"/media/local/{DOMAIN}/videos/{path.name}" if path.exists() else None)

    with pytest.raises(RuntimeError, match="not cached"):
        source._require_cached_clip_playback_url(client, "cam", 10, 20)
    assert client.downloaded == []


def test_require_cached_clip_playback_url_rejects_storage_outside_media(monkeypatch):
    source = TuyaRecordingsMediaSource(hass=None)
    client = CachedClipClient()
    monkeypatch.setattr(source, "_local_media_url", lambda path: None)

    with pytest.raises(RuntimeError, match="under /media"):
        source._require_cached_clip_playback_url(client, "cam", 10, 20)


def test_resolve_clip_playback_url_downloads_uncached_when_lazy(monkeypatch):
    source = TuyaRecordingsMediaSource(hass=None)
    client = DownloadingClipClient()
    monkeypatch.setattr(source, "_local_media_url", lambda path: f"/media/local/{DOMAIN}/videos/{path.name}" if path.exists() else None)

    assert source._resolve_clip_playback_url(client, "cam", 10, 20) == f"/media/local/{DOMAIN}/videos/cam_10_20.mp4"
    assert client.downloaded == [("cam", 10, 20, client.path)]


def test_resolve_clip_playback_url_rejects_uncached_when_precache_enabled(monkeypatch):
    source = TuyaRecordingsMediaSource(hass=None)
    client = DownloadingClipClient()
    client.media_sync_enabled = True
    monkeypatch.setattr(source, "_local_media_url", lambda path: f"/media/local/{DOMAIN}/videos/{path.name}" if path.exists() else None)

    with pytest.raises(RuntimeError, match="not cached"):
        source._resolve_clip_playback_url(client, "cam", 10, 20)
    assert client.downloaded == []


def test_visible_clips_lazy_mode_includes_uncached():
    source = TuyaRecordingsMediaSource(hass=None)
    client = DownloadingClipClient()
    camera = {"devId": "cam", "clips": [{"start": 10, "end": 20, "date": "2026-06-29"}]}

    assert source._visible_clips(client, camera) == camera["clips"]


def test_visible_clips_precache_mode_requires_cached_video_and_thumbnail():
    source = TuyaRecordingsMediaSource(hass=None)
    client = DownloadingClipClient()
    client.media_sync_enabled = True
    camera = {"devId": "cam", "clips": [{"start": 10, "end": 20, "date": "2026-06-29"}]}

    assert source._visible_clips(client, camera) == []


def test_thumbnail_url_uses_private_integration_endpoint():
    assert TuyaRecordingsMediaSource._thumbnail_url_for_clip("cam 1", 10, 20) == f"/api/{DOMAIN}/thumb/cam%201/10/20"


def test_language_time_format_uses_12_hour_for_plain_english():
    assert TuyaRecordingsMediaSource._language_uses_12h_time("en") is True
    assert TuyaRecordingsMediaSource._language_uses_12h_time("en-CA") is True
    assert TuyaRecordingsMediaSource._language_uses_12h_time("en-GB") is False


def test_format_time_supports_12_hour_titles():
    from datetime import datetime

    value = datetime(2026, 6, 27, 19, 30, 7)

    assert TuyaRecordingsMediaSource._format_time(value, use_12h=True) == "7:30:07 PM"
    assert TuyaRecordingsMediaSource._format_time(value, use_12h=False) == "19:30:07"


def test_media_storage_path_validation_accepts_private_absolute_path():
    assert _validate_media_storage_path("/media/tuya_recordings") == "/media/tuya_recordings"


@pytest.mark.parametrize("path", ["tuya_recordings", "/config/www", "/config/www/tuya_recordings"])
def test_media_storage_path_validation_rejects_public_or_relative_paths(path):
    with pytest.raises(Exception):
        _validate_media_storage_path(path)
