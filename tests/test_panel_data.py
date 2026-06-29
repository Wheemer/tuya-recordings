from __future__ import annotations

from custom_components.tuya_recordings.http import build_panel_data


class FakeClient:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path / "tuya_recordings"
        self.media_sync_enabled = True

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
    clip = camera["clips"][0]
    assert camera["dates"] == ["2026-06-28"]
    assert clip["duration"] == 30
    assert clip["cached"] is True
    assert clip["thumbnail_cached"] is True
    assert clip["playback_url"] == "/media/local/tuya_recordings/videos/camera%201_100_130.mp4"
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


def test_build_panel_data_lazy_mode_shows_uncached_files(tmp_path):
    client = FakeClient(tmp_path)
    client.media_sync_enabled = False

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
    clip = camera["clips"][0]
    assert camera["dates"] == ["2026-06-28"]
    assert clip["cached"] is False
    assert clip["thumbnail_cached"] is False
    assert clip["playback_url"] == "/api/tuya_recordings/play/camera%201/100/130"
    assert clip["thumbnail_url"] == ""
    assert data["stats"]["cache_only"] is False
    assert data["stats"]["indexed_clips"] == 1
    assert data["stats"]["ready_clips"] == 0
    assert data["stats"]["pending_clips"] == 1
    assert data["stats"]["visible_clips"] == 1
    assert data["stats"]["latest_clip"]["camera_name"] == "Front"
