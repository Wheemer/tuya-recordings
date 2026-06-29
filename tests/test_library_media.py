from custom_components.tuya_recordings.lib import CachedClipKey, MediaSyncStatus, cleanup_cached_media, parse_cached_media_name


class FakeLogger:
    def debug(self, msg, *args):
        pass


def test_parse_cached_media_name_round_trips_safe_clip_key():
    assert parse_cached_media_name("cam_1_10_20.mp4", ".mp4") == CachedClipKey("cam_1", 10, 20)
    assert parse_cached_media_name("not-a-clip.mp4", ".mp4") is None


def test_media_sync_status_serializes_updates():
    status = MediaSyncStatus()

    status.update("running", downloaded=2, current={"dev_id": "camera"})

    payload = status.to_dict()
    assert payload["state"] == "running"
    assert payload["downloaded"] == 2
    assert payload["current"] == {"dev_id": "camera"}
    assert payload["updated_at"]


def test_cleanup_cached_media_deletes_stale_files(tmp_path):
    video = tmp_path / "videos" / "camera_10_20.mp4"
    thumb = tmp_path / "thumbs" / "camera_10_20.jpg"
    keep = tmp_path / "videos" / "camera_30_40.mp4"
    video.parent.mkdir(parents=True)
    thumb.parent.mkdir(parents=True)
    video.write_bytes(b"old")
    thumb.write_bytes(b"old")
    keep.write_bytes(b"keep")

    result = cleanup_cached_media(tmp_path, {CachedClipKey("camera", 30, 40)}, None, FakeLogger())

    assert result == {"deleted_videos": 1, "deleted_thumbnails": 1}
    assert not video.exists()
    assert not thumb.exists()
    assert keep.exists()
