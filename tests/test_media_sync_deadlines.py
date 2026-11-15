"""Caching budgets must not truncate healthy long SD recordings."""

from unittest.mock import Mock
from types import SimpleNamespace

import pytest

from custom_components.tuya_recordings import client as module
from custom_components.tuya_recordings.lib.commands import _WORK_CANCEL, CameraCommandQueue


def test_backfill_discovers_and_downloads_older_days_newest_first(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(module, "CAMERA_COMMANDS", CameraCommandQueue())
    monkeypatch.setattr(module, "MEDIA_SYNC_INTER_ATTEMPT_DELAY", 0)
    client = module.TuyaRecordingsClient({"media_sync_enabled": True, "lookback_days": 5},
                                        media_storage_path=tmp_path)
    today = module.dt_util.now().date()
    now = datetime.now(timezone.utc).isoformat()
    client._camera_index_cache = {"cameras": [{"devId": "camera", "clips": [],
        "catalogDays": {today.isoformat(): now, (today - timedelta(days=1)).isoformat(): now}}]}
    monkeypatch.setattr(client, "_camera_devices", lambda: [{"devId": "camera", "online": True}])
    monkeypatch.setattr(client, "_camera_candidates", lambda devices: devices)
    queried = []

    def query(dev_id, day):
        assert not _WORK_CANCEL.get().is_set()
        queried.append(day)
        start = int(datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp())
        return [{"start": start, "end": start + 10}]

    monkeypatch.setattr(client, "_recordings_for_day", query)
    monkeypatch.setattr(client, "refresh_recent_recordings", lambda: client._camera_index_cache)
    monkeypatch.setattr(client, "_recover_interrupted_media_sync", lambda: 0)
    monkeypatch.setattr(client, "cleanup_cached_media", lambda *args, **kwargs: {})
    downloaded = []
    monkeypatch.setattr(client, "download_clip", lambda dev, start, end, path, **kwargs: downloaded.append(start))
    client.refresh_background_catalog(limit=2)
    assert queried == [today - timedelta(days=2), today - timedelta(days=3)]
    result = client.sync_recordings()
    assert result["downloaded"] == 2
    assert downloaded == sorted(downloaded, reverse=True)
    assert all(start < int(datetime.combine(today - timedelta(days=1), datetime.min.time(),
                                           tzinfo=timezone.utc).timestamp()) for start in downloaded)
    client.refresh_background_catalog(limit=2)
    assert queried[-1] == today - timedelta(days=4)
    assert len(queried) == 3


def test_remux_failure_skips_only_that_camera_and_keeps_other_downloads(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "CAMERA_COMMANDS", CameraCommandQueue())
    monkeypatch.setattr(module, "MEDIA_SYNC_INTER_ATTEMPT_DELAY", 0)
    client = module.TuyaRecordingsClient({"media_sync_enabled": True}, media_storage_path=tmp_path)
    index = {"cameras": [
        {"devId": "ptz", "clips": [{"start": 300, "end": 310}, {"start": 250, "end": 260}]},
        {"devId": "doorbell", "clips": [{"start": 200, "end": 210}]},
        {"devId": "working", "clips": [{"start": 100, "end": 110}]},
    ]}
    monkeypatch.setattr(client, "refresh_recent_recordings", lambda: index)
    monkeypatch.setattr(client, "_recover_interrupted_media_sync", lambda: 0)
    store = Mock()
    cleanup = Mock(return_value={})
    monkeypatch.setattr(client, "_store_media_failures", store)
    monkeypatch.setattr(client, "cleanup_cached_media", cleanup)
    calls = []

    def download(dev, start, end, path, **kwargs):
        calls.append((dev, start))
        assert not _WORK_CANCEL.get().is_set()
        if dev == "ptz":
            raise RuntimeError("Native H264 remux failed")

    monkeypatch.setattr(client, "download_clip", download)
    result = client.sync_recordings()
    assert calls == [("ptz", 300), ("doorbell", 200), ("working", 100)]
    assert result["downloaded"] == 2 and result["failed"] == 1 and result["skipped"] == 1
    assert result["failed_cameras"] == ["ptz"]
    assert result["unavailable_cameras"] == []
    assert set(client._media_failures) == {("ptz", 300, 310)}
    assert client._media_sync_status.to_dict()["last_error"] == "Native H264 remux failed"
    store.assert_called_once()
    cleanup.assert_not_called()
    assert not client._media_sync_lock.locked()


@pytest.mark.parametrize("failure", ["cancel", "auth"])
def test_sync_does_not_continue_after_cancellation_or_auth_failure(monkeypatch, tmp_path, failure):
    monkeypatch.setattr(module, "CAMERA_COMMANDS", CameraCommandQueue())
    client = module.TuyaRecordingsClient({"media_sync_enabled": True}, media_storage_path=tmp_path)
    index = {"cameras": [{"devId": dev, "clips": [{"start": start, "end": start + 10}]}
                         for dev, start in [("first", 200), ("second", 100)]]}
    monkeypatch.setattr(client, "refresh_recent_recordings", lambda: index)
    monkeypatch.setattr(client, "_recover_interrupted_media_sync", lambda: 0)
    calls = []

    def download(dev, *args, **kwargs):
        calls.append(dev)
        if failure == "cancel":
            raise module.CameraWorkCancelled("playback priority")
        raise module.TuyaRecordingsAuthError("expired session")

    monkeypatch.setattr(client, "download_clip", download)
    if failure == "auth":
        with pytest.raises(module.TuyaRecordingsAuthError):
            client.sync_recordings()
    else:
        assert client.sync_recordings()["cancelled"] is True
    assert calls == ["first"]
    assert not client._media_sync_lock.locked()


@pytest.mark.parametrize("race", [False, True])
def test_sync_defers_cooldown_without_failure_and_resumes_next_pass(monkeypatch, tmp_path, race):
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    commands = CameraCommandQueue()
    monkeypatch.setattr(module, "CAMERA_COMMANDS", commands)
    client = module.TuyaRecordingsClient({"media_sync_enabled": True}, media_storage_path=tmp_path)
    index = {"cameras": [
        {"devId": "sleeping", "clips": [{"start": 100, "end": 110}, {"start": 80, "end": 90}]},
        {"devId": "awake", "clips": [{"start": 50, "end": 60}]},
    ]}
    monkeypatch.setattr(client, "refresh_recent_recordings", lambda: index)
    monkeypatch.setattr(client, "_recover_interrupted_media_sync", lambda: 0)
    monkeypatch.setattr(client, "_store_media_failures", Mock())
    cleanup = Mock(return_value={})
    monkeypatch.setattr(client, "cleanup_cached_media", cleanup)
    def cooldown():
        assert commands.reserve_wake("sleeping")
        commands.mark_wake_report_missing("sleeping")
    if not race:
        cooldown()
    calls = []
    def download(dev, *args, **kwargs):
        if race and not calls and clock[0] == 0 and dev == "sleeping":
            cooldown()
            raise module.CameraWorkBusy("cooldown began after selection")
        calls.append(dev)
    monkeypatch.setattr(client, "download_clip", download)
    result = client.sync_recordings()
    assert calls == ["awake"]
    assert result["deferred"] == 2 and result["failed"] == 0
    assert result["downloaded"] == 1
    assert client._media_failures == {}
    cleanup.assert_not_called()
    assert commands.wake_report_on_cooldown("sleeping")
    clock[0] = 61
    calls.clear()
    result = client.sync_recordings()
    assert calls == ["sleeping", "sleeping", "awake"]
    assert result["deferred"] == result["failed"] == 0
    assert result["downloaded"] == 3
    assert client._media_failures == {}
    # Retention is now independent of camera success, not catalog-based pruning.
    cleanup.assert_not_called()


def test_cooldown_download_does_not_allocate_output_or_contact_camera(monkeypatch, tmp_path):
    commands = CameraCommandQueue()
    monkeypatch.setattr(module, "CAMERA_COMMANDS", commands)
    assert commands.reserve_wake("camera")
    commands.mark_wake_report_missing("camera")
    client = module.TuyaRecordingsClient({}, media_storage_path=tmp_path)
    assert not client.media_sync_enabled
    output = tmp_path / "uncreated" / "clip.mp4"
    with pytest.raises(module.CameraWorkBusy, match="deferred"):
        client.download_clip("camera", 100, 110, output)
    assert not output.parent.exists()


def test_download_cooldown_race_cleans_partial_without_transfer_failure(monkeypatch, tmp_path, caplog):
    commands = CameraCommandQueue()
    monkeypatch.setattr(module, "CAMERA_COMMANDS", commands)
    client = module.TuyaRecordingsClient({}, media_storage_path=tmp_path)
    monkeypatch.setattr(client, "_clip_playback_metadata", lambda *args: {})
    def receive(dev, _one, _two, _start, _end, path, **kwargs):
        path.write_bytes(b"partial")
        assert commands.reserve_wake(dev)
        commands.mark_wake_report_missing(dev)
        raise RuntimeError("native session entered cooldown")
    monkeypatch.setattr(client, "_backend", SimpleNamespace(clip_playback_available=True, receive_clip=receive))
    output = tmp_path / "clip.mp4"
    with pytest.raises(module.CameraWorkBusy, match="deferred"):
        client.download_clip("camera", 100, 110, output)
    assert not output.exists() and not output.with_suffix(".tmp.mp4").exists()
    assert "Failed to transfer" not in caplog.text


def test_battery_sync_defaults_off_and_setup_warning_is_present():
    import json
    from pathlib import Path
    assert module.DEFAULT_MEDIA_SYNC_ENABLED is False
    root = Path(module.__file__).parent
    for filename in ("strings.json", "translations/en.json"):
        translations = json.loads((root / filename).read_text())
        for section in ("config", "options"):
            description = translations[section]["step"]["user" if section == "config" else "init"]["data_description"]["media_sync_enabled"]
            assert "off by default" in description and "Battery cameras" in description


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
