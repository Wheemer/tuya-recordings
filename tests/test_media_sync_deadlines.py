"""Caching budgets must not truncate healthy long SD recordings."""

from unittest.mock import Mock

import pytest

from custom_components.tuya_recordings import client as module
from custom_components.tuya_recordings.lib.commands import _WORK_CANCEL


@pytest.mark.parametrize("catalog_seconds,transfer_seconds", [(100, 100), (0, 130)])
def test_catalog_time_and_pass_budget_do_not_cancel_active_transfer(monkeypatch, tmp_path, catalog_seconds, transfer_seconds):
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    client = module.TuyaRecordingsClient({"media_sync_enabled": True}, media_storage_path=tmp_path)
    clips = [{"start": 100, "end": 45800}, {"start": 50, "end": 60}]
    def refresh():
        clock[0] += catalog_seconds
        assert not _WORK_CANCEL.get().is_set()
        return {"cameras": [{"devId": "camera", "clips": clips}]}
    def download(dev, start, end, path, **kwargs):
        clock[0] += transfer_seconds
        assert not _WORK_CANCEL.get().is_set()
    monkeypatch.setattr(client, "refresh_recent_recordings", refresh)
    monkeypatch.setattr(client, "download_clip", Mock(side_effect=download))
    monkeypatch.setattr(client, "_recover_interrupted_media_sync", lambda: 0)
    monkeypatch.setattr(client, "_set_media_sync_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(client, "_store_media_failures", lambda: None)
    monkeypatch.setattr(client, "cleanup_cached_media", lambda *args: {})
    result = client.sync_recordings()
    assert result["downloaded"] >= 1
    assert not result.get("cancelled") and not result.get("timed_out")
    if transfer_seconds > module.MEDIA_SYNC_CAMERA_PASS_TIMEOUT:
        assert result["yielded"]
        assert client.download_clip.call_count == 1


def test_background_pause_still_interrupts_long_download(monkeypatch, tmp_path):
    client = module.TuyaRecordingsClient({"media_sync_enabled": True}, media_storage_path=tmp_path)
    monkeypatch.setattr(client, "refresh_recent_recordings", lambda: {"cameras": [{"devId": "camera", "clips": [{"start": 100, "end": 45800}]}]})
    def download(*args, **kwargs):
        client._background_pause_event.set()
        assert _WORK_CANCEL.get().is_set()
        raise module.CameraWorkCancelled("foreground playback")
    monkeypatch.setattr(client, "download_clip", download)
    monkeypatch.setattr(client, "_recover_interrupted_media_sync", lambda: 0)
    monkeypatch.setattr(client, "_set_media_sync_status", lambda *args, **kwargs: None)
    result = client.sync_recordings()
    assert result["cancelled"]
    assert not result.get("timed_out")
