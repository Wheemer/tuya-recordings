from __future__ import annotations

import pytest

from aiohttp import web

from custom_components.tuya_recordings.http import (
    _raise_native_playback_unavailable,
    _timeline_range_for_time,
    build_panel_data,
)
from custom_components.tuya_recordings.lib.native_backend import NativeBackendNotConfigured


class FakeClient:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path / "tuya_recordings"
        self.media_sync_enabled = True
        self.thumbnail_sync_enabled = False
        self.cloud_activity_paused = False

    def clip_path(self, dev_id, start, end):
        return self.tmp_path / "videos" / f"{dev_id}_{start}_{end}.mp4"

    def thumbnail_path(self, dev_id, start, end):
        return self.tmp_path / "thumbs" / f"{dev_id}_{start}_{end}.jpg"

    def clip_ready(self, dev_id, start, end):
        path = self.clip_path(dev_id, start, end)
        if not path.exists() or path.stat().st_size < 1024:
            return False
        data = path.read_bytes()
        return b"ftyp" in data[:128] and b"moov" in data

    def clip_cached(self, dev_id, start, end):
        path = self.clip_path(dev_id, start, end)
        if not path.exists() or path.stat().st_size < 1024:
            return False
        return b"ftyp" in path.read_bytes()[:128]


class FakePlayableBackend:
    name = "apk-native"
    clip_playback_available = True

def test_build_panel_data_marks_cached_files(tmp_path):
    client = FakeClient(tmp_path)
    video_path = client.clip_path("camera 1", 100, 130)
    thumb_path = client.thumbnail_path("camera 1", 100, 130)
    video_path.parent.mkdir(parents=True)
    thumb_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + (b"\x00" * 2048) + b"moov")
    thumb_path.write_bytes(b"jpg")

    data = build_panel_data(
        client,
        {
            "generatedAt": "now",
            "cameras": [
                {
                    "devId": "camera 1",
                    "name": "Front",
                    "online": True,
                    "clips": [{"start": 100, "end": 130, "date": "2026-06-28", "title": "10:00 - 10:30"}],
                }
            ],
        },
        media_root=tmp_path,
    )

    camera = data["cameras"][0]
    assert camera["playback_mode"] == "clips"
    assert camera["recording_ranges"] == []
    clip = camera["clips"][0]
    assert camera["dates"] == ["2026-06-28"]
    assert clip["duration"] == 30
    assert clip["cached"] is True
    assert clip["thumbnail_cached"] is True
    assert clip["playback_url"] == "/media/local/tuya_recordings/videos/camera%201_100_130.mp4"
    assert clip["stream_transport"] == "file"
    assert clip["thumbnail_url"] == "/media/local/tuya_recordings/thumbs/camera%201_100_130.jpg"
    assert data["stats"]["indexed_clips"] == 1
    assert data["stats"]["ready_clips"] == 1
    assert data["stats"]["pending_clips"] == 0
    assert data["stats"]["cached_videos"] == 1
    assert data["stats"]["cached_thumbnails"] == 1
    assert data["stats"]["visible_clips"] == 1
    assert data["stats"]["online_cameras"] == 1
    assert data["stats"]["total_cameras"] == 1
    assert data["stats"]["latest_clip"] == {
        "dev_id": "camera 1",
        "camera_name": "Front",
        "start": 100,
        "end": 130,
        "duration": 30,
    }


def test_build_panel_data_hides_junk_mp4(tmp_path):
    client = FakeClient(tmp_path)
    video_path = client.clip_path("camera 1", 100, 130)
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"not a valid mp4 but not empty")

    data = build_panel_data(
        client,
        {
            "generatedAt": "now",
            "cameras": [
                {
                    "devId": "camera 1",
                    "name": "Front",
                    "online": True,
                    "clips": [{"start": 100, "end": 130, "date": "2026-06-28"}],
                }
            ],
        },
        media_root=tmp_path,
    )

    camera = data["cameras"][0]
    assert camera["dates"] == []
    assert camera["clips"] == []
    assert data["stats"]["indexed_clips"] == 1
    assert data["stats"]["ready_clips"] == 0
    assert data["stats"]["pending_clips"] == 1
    assert data["stats"]["visible_clips"] == 0
    assert data["stats"]["latest_clip"] is None


def test_build_panel_data_hides_uncached_files(tmp_path):
    client = FakeClient(tmp_path)

    data = build_panel_data(
        client,
        {
            "generatedAt": "now",
            "cameras": [
                {
                    "devId": "camera 1",
                    "name": "Front",
                    "online": True,
                    "clips": [{"start": 100, "end": 130, "date": "2026-06-28"}],
                }
            ],
        },
        media_root=tmp_path,
    )

    camera = data["cameras"][0]
    assert camera["dates"] == []
    assert camera["clips"] == []
    assert data["stats"]["indexed_clips"] == 1
    assert data["stats"]["ready_clips"] == 0
    assert data["stats"]["pending_clips"] == 1
    assert data["stats"]["visible_clips"] == 0
    assert data["stats"]["latest_clip"] is None


def test_build_panel_data_hides_cached_video_without_thumbnail(tmp_path):
    client = FakeClient(tmp_path)
    video_path = client.clip_path("camera 1", 100, 130)
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + (b"\x00" * 2048) + b"moov")

    data = build_panel_data(
        client,
        {
            "generatedAt": "now",
            "cameras": [
                {
                    "devId": "camera 1",
                    "name": "Front",
                    "online": True,
                    "clips": [{"start": 100, "end": 130, "date": "2026-06-28"}],
                }
            ],
        },
        media_root=tmp_path,
    )

    camera = data["cameras"][0]
    assert camera["dates"] == []
    assert camera["clips"] == []
    assert data["stats"]["indexed_clips"] == 1
    assert data["stats"]["ready_clips"] == 0
    assert data["stats"]["pending_clips"] == 1
    assert data["stats"]["cached_videos"] == 1
    assert data["stats"]["cached_thumbnails"] == 0
    assert data["stats"]["visible_clips"] == 0
    assert data["stats"]["latest_clip"] is None


def test_build_panel_data_timeline_mode_exposes_ranges_without_thumbnails(tmp_path):
    client = FakeClient(tmp_path)
    client.media_sync_enabled = False
    client._recordings_backend = FakePlayableBackend()

    data = build_panel_data(
        client,
        {
            "generatedAt": "now",
            "cameras": [
                {
                    "devId": "camera 1",
                    "name": "Front",
                    "online": True,
                    "clips": [{"start": 100, "end": 130, "date": "2026-06-28"}],
                }
            ],
        },
        media_root=tmp_path,
    )

    camera = data["cameras"][0]
    assert camera["playback_mode"] == "timeline"
    assert camera["dates"] == ["2026-06-28"]
    assert camera["clips"] == []
    assert len(camera["recording_ranges"]) == 1
    assert camera["recording_ranges"][0]["thumbnail_url"] == ""
    assert camera["recording_ranges"][0]["playback_url"] == "/api/tuya_recordings/timeline/camera%201"
    assert data["stats"]["cache_only"] is False
    assert data["stats"]["thumbnail_cache_only"] is False
    assert data["stats"]["indexed_clips"] == 1
    assert data["stats"]["ready_clips"] == 0
    assert data["stats"]["pending_clips"] == 1
    assert data["stats"]["visible_clips"] == 1
    assert data["stats"]["latest_clip"]["start"] == 100


def test_build_panel_data_uses_native_transport_for_uncached_playback(tmp_path):
    client = FakeClient(tmp_path)
    client.media_sync_enabled = False
    client._recordings_backend = FakePlayableBackend()
    thumb_path = client.thumbnail_path("camera 1", 100, 130)
    thumb_path.parent.mkdir(parents=True)
    thumb_path.write_bytes(b"jpg")

    data = build_panel_data(
        client,
        {
            "generatedAt": "now",
            "cameras": [
                {
                    "devId": "camera 1",
                    "name": "Front",
                    "online": True,
                    "clips": [{"start": 100, "end": 130, "date": "2026-06-28"}],
                }
            ],
        },
        media_root=tmp_path,
    )

    assert data["cameras"][0]["recording_ranges"][0]["stream_transport"] == "native"


def test_timeline_mode_does_not_turn_stale_cached_file_into_clip_playback(tmp_path):
    client = FakeClient(tmp_path)
    client.media_sync_enabled = False
    client._recordings_backend = FakePlayableBackend()
    video_path = client.clip_path("camera 1", 100, 130)
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + (b"\x00" * 2048) + b"moov")

    data = build_panel_data(
        client,
        {
            "cameras": [{
                "devId": "camera 1",
                "name": "Front",
                "online": True,
                "clips": [{"start": 100, "end": 130, "date": "2026-06-28"}],
            }],
        },
        media_root=tmp_path,
    )

    recording_range = data["cameras"][0]["recording_ranges"][0]
    assert recording_range["cached"] is False
    assert recording_range["locally_cached"] is True
    assert recording_range["stream_transport"] == "native"


def test_build_panel_data_uses_backend_playback_flag(tmp_path):
    client = FakeClient(tmp_path)
    client.media_sync_enabled = False
    client._recordings_backend = NativeBackendNotConfigured("APK-native media transport is not implemented")
    client.clip_playback_available = True

    data = build_panel_data(
        client,
        {
            "generatedAt": "now",
            "cameras": [
                {
                    "devId": "camera 1",
                    "name": "Front",
                    "online": True,
                    "clips": [{"start": 100, "end": 130, "date": "2026-06-28"}],
                }
            ],
        },
        media_root=tmp_path,
    )

    assert data["cameras"][0]["clips"] == []
    assert data["cameras"][0]["recording_ranges"][0]["playback_url"] == ""
    assert data["stats"]["recordings_backend"]["clip_playback_available"] is False


def test_build_panel_data_hides_uncached_native_playback_when_backend_unavailable(tmp_path):
    client = FakeClient(tmp_path)
    client.media_sync_enabled = False
    client._recordings_backend = NativeBackendNotConfigured("Smart Life app-session authorization is not configured")

    data = build_panel_data(
        client,
        {
            "generatedAt": "now",
            "cameras": [
                {
                    "devId": "camera 1",
                    "name": "Front",
                    "online": True,
                    "clips": [{"start": 100, "end": 130, "date": "2026-06-28"}],
                }
            ],
        },
        media_root=tmp_path,
    )

    assert data["cameras"][0]["clips"] == []
    assert data["cameras"][0]["recording_ranges"][0]["playback_url"] == ""
    assert data["stats"]["recordings_backend"] == {
        "name": "apk-native-not-configured",
        "available": False,
        "clip_playback_available": False,
        "reason": "Smart Life app-session authorization is not configured",
    }


def test_build_panel_data_thumbnail_sync_keeps_all_timeline_ranges(tmp_path):
    client = FakeClient(tmp_path)
    client.media_sync_enabled = False
    client.thumbnail_sync_enabled = True
    client._recordings_backend = FakePlayableBackend()
    thumb_path = client.thumbnail_path("camera 1", 100, 130)
    thumb_path.parent.mkdir(parents=True)
    thumb_path.write_bytes(b"jpg")

    data = build_panel_data(
        client,
        {
            "generatedAt": "now",
            "cameras": [
                {
                    "devId": "camera 1",
                    "name": "Front",
                    "online": True,
                    "clips": [
                        {"start": 100, "end": 130, "date": "2026-06-28"},
                        {"start": 200, "end": 230, "date": "2026-06-28"},
                    ],
                }
            ],
        },
        media_root=tmp_path,
    )

    assert data["cameras"][0]["clips"] == []
    assert len(data["cameras"][0]["recording_ranges"]) == 2
    assert data["stats"]["thumbnail_cache_only"] is False
    assert data["stats"]["visible_clips"] == 2
    assert data["stats"]["ready_clips"] == 0
    assert data["stats"]["pending_clips"] == 2


def test_build_panel_data_reports_cloud_pause_state(tmp_path):
    client = FakeClient(tmp_path)
    client.cloud_activity_paused = True

    data = build_panel_data(client, {"generatedAt": "now", "cameras": []}, media_root=tmp_path)

    assert data["stats"]["cloud_activity_paused"] is True


def test_native_playback_guard_rejects_paused_client(tmp_path):
    client = FakeClient(tmp_path)
    client.cloud_activity_paused = True
    client._recordings_backend = FakePlayableBackend()

    with pytest.raises(web.HTTPServiceUnavailable, match="paused"):
        _raise_native_playback_unavailable(client)


def test_native_playback_guard_rejects_unavailable_backend(tmp_path):
    client = FakeClient(tmp_path)
    client.media_sync_enabled = False
    client._recordings_backend = NativeBackendNotConfigured("missing transport")

    with pytest.raises(web.HTTPServiceUnavailable, match="not available"):
        _raise_native_playback_unavailable(client)


def test_build_panel_data_cloud_pause_keeps_timeline_catalog_visible(tmp_path):
    client = FakeClient(tmp_path)
    client.media_sync_enabled = False
    client.cloud_activity_paused = True

    data = build_panel_data(
        client,
        {
            "generatedAt": "now",
            "cameras": [
                {
                    "devId": "camera 1",
                    "name": "Front",
                    "online": True,
                    "clips": [{"start": 100, "end": 130, "date": "2026-06-28"}],
                }
            ],
        },
        media_root=tmp_path,
    )

    assert data["cameras"][0]["clips"] == []
    assert len(data["cameras"][0]["recording_ranges"]) == 1
    assert data["cameras"][0]["recording_ranges"][0]["playback_url"] == ""
    assert data["stats"]["visible_clips"] == 1
    assert data["stats"]["cloud_activity_paused"] is True


def test_timeline_range_resolution_uses_containing_then_next_range():
    index = {
        "cameras": [{
            "devId": "camera-1",
            "clips": [
                {"start": 100, "end": 130},
                {"start": 200, "end": 240},
            ],
        }]
    }

    assert _timeline_range_for_time(index, "camera-1", 110) == (100, 130)
    assert _timeline_range_for_time(index, "camera-1", 150) == (200, 240)
    assert _timeline_range_for_time(index, "camera-1", 250) == (200, 240)
    assert _timeline_range_for_time(index, "missing", 110) is None
