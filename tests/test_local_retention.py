"""Local lifecycle policy must never depend on a successful camera transaction."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components import tuya_recordings as integration
from custom_components.tuya_recordings import client as module
from custom_components.tuya_recordings.const import DOMAIN
from custom_components.tuya_recordings.lib.local_media import LocalMediaState, local_clip_key


NOW = 2_000_000_000
OLD = NOW - 7200
RECENT = NOW - 600
LOG = logging.getLogger(__name__)


def write_clip(root, start=OLD, dev="camera", thumbnail=True):
    key = local_clip_key(dev, start, start + 20)
    for folder, suffix in [("videos", ".mp4")] + ([("thumbs", ".jpg")] if thumbnail else []):
        path = root / folder / f"{dev}_{start}_{start + 20}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    return key


def make_client(monkeypatch, root, **options):
    monkeypatch.setattr(module.time, "time", lambda: NOW)
    monkeypatch.setattr(module, "MEDIA_SYNC_INTER_ATTEMPT_DELAY", 0)
    client = module.TuyaRecordingsClient(options, media_storage_path=root)
    monkeypatch.setattr(client, "_recover_interrupted_media_sync", lambda: 0)
    return client


@pytest.mark.parametrize("paused", [False, True])
def test_wall_clock_retention_runs_without_sync_or_catalog(monkeypatch, tmp_path, paused):
    key = write_clip(tmp_path)
    client = make_client(monkeypatch, tmp_path, media_retention_hours=1,
                         cloud_activity_paused=paused)
    camera = Mock(side_effect=AssertionError("Local cleanup contacted a camera"))
    monkeypatch.setattr(client, "refresh_recent_recordings", camera)
    result = client.sync_recordings()
    assert result["deleted_videos"] == result["deleted_thumbnails"] == 1
    assert not client._local_policy().path(key).exists()
    assert client._media_sync_status.last_result["deleted_videos"] == 1
    camera.assert_not_called()


def test_cleanup_precedes_catalog_failure(monkeypatch, tmp_path):
    key = write_clip(tmp_path)
    client = make_client(monkeypatch, tmp_path, media_retention_hours=1, media_sync_enabled=True)
    monkeypatch.setattr(client, "refresh_recent_recordings", Mock(side_effect=RuntimeError("camera offline")))
    with pytest.raises(RuntimeError, match="camera offline"):
        client.sync_recordings()
    assert not client._local_policy().path(key).exists()
    assert not client._media_sync_lock.locked()


def test_download_window_does_not_expire_existing_files(monkeypatch, tmp_path):
    key = write_clip(tmp_path)
    client = make_client(monkeypatch, tmp_path, media_sync_hours=1, media_retention_hours=0)
    assert client.maintain_local_media()["deleted_videos"] == 0
    assert client._local_policy().path(key).exists()


@pytest.mark.parametrize("option", ["media_sync_hours", "media_retention_hours"])
def test_current_time_window_skips_old_catalog_even_when_newest_is_old(monkeypatch, tmp_path, option):
    client = make_client(monkeypatch, tmp_path, media_sync_enabled=True,
                         media_retention_hours=0, **({option: 1} if option != "media_retention_hours" else {}))
    if option == "media_retention_hours":
        client.media_retention_hours = 1
    monkeypatch.setattr(client, "refresh_recent_recordings", lambda: {"cameras": [
        {"devId": "camera", "clips": [{"start": OLD, "end": OLD + 20}]}]})
    download = Mock()
    monkeypatch.setattr(client, "download_clip", download)
    assert client.sync_recordings()["skipped"] == 1
    download.assert_not_called()


@pytest.mark.parametrize("deletion", ["single", "bulk", "external"])
def test_deleted_clips_stay_deleted_across_restart(monkeypatch, tmp_path, deletion):
    key = write_clip(tmp_path, RECENT)
    client = make_client(monkeypatch, tmp_path, media_sync_enabled=True)
    client.maintain_local_media()
    if deletion == "single":
        client.delete_local_recording(key.dev_id, key.start, key.end)
    elif deletion == "bulk":
        client.clear_video_cache()
    else:
        client._local_policy().path(key).unlink()
    restarted = make_client(monkeypatch, tmp_path, media_sync_enabled=True)
    index = {"cameras": [{"devId": "camera", "clips": [{"start": key.start, "end": key.end}]}]}
    monkeypatch.setattr(restarted, "refresh_recent_recordings", lambda: index)
    download = Mock()
    monkeypatch.setattr(restarted, "download_clip", download)
    assert restarted.sync_recordings()["skipped"] == 1
    assert restarted.local_recording_status(key.dev_id, key.start, key.end) == "deleted"
    download.assert_not_called()
    assert restarted.allow_local_recording_downloads(key.dev_id, key.start, key.end) == 1
    download.assert_not_called()
    assert restarted.sync_recordings()["downloaded"] == 1
    download.assert_called_once()


def test_automatic_expiry_is_not_manual_deletion(monkeypatch, tmp_path):
    key = write_clip(tmp_path)
    client = make_client(monkeypatch, tmp_path, media_retention_hours=1)
    client.maintain_local_media()
    assert client.local_recording_status(key.dev_id, key.start, key.end) == "expired"
    client.media_retention_hours = 0
    assert client.local_recording_status(key.dev_id, key.start, key.end) == "pending"


def test_recent_playback_lease_defers_expiry(monkeypatch, tmp_path):
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    key = write_clip(tmp_path)
    client = make_client(monkeypatch, tmp_path, media_retention_hours=1)
    client.protect_local_clip(key.dev_id, key.start, key.end)
    assert client.maintain_local_media()["retention_deferred"] == 2
    assert client._local_policy().path(key).exists()
    clock[0] += 1801
    assert client.maintain_local_media()["deleted_videos"] == 1


def test_deletion_rejects_concurrent_sync(monkeypatch, tmp_path):
    key = write_clip(tmp_path)
    client = make_client(monkeypatch, tmp_path)
    with client._media_sync_lock:
        with pytest.raises(module.CameraWorkBusy):
            client.delete_local_recording(key.dev_id, key.start, key.end)
        assert client.maintain_local_media() == {"busy": True}
    assert client._local_policy().path(key).exists()


def test_cleanup_preserves_unknown_other_account_and_temporary_files(monkeypatch, tmp_path):
    mine = write_clip(tmp_path)
    other = write_clip(tmp_path, dev="other")
    unknown = tmp_path / "videos" / "unrelated.mp4"
    temporary = tmp_path / "videos" / "camera_1_2.tmp.mp4"
    unknown.write_bytes(b"keep")
    temporary.write_bytes(b"keep")
    client = make_client(monkeypatch, tmp_path, media_retention_hours=1)
    client._camera_inventory = {"camera": {}}
    assert client.maintain_local_media()["deleted_videos"] == 1
    assert not client._local_policy().path(mine).exists()
    assert client._local_policy().path(other).exists()
    assert unknown.exists() and temporary.exists()
    with pytest.raises(ValueError, match="Unknown recording camera"):
        client.delete_local_recording(other.dev_id, other.start, other.end)


def test_corrupt_history_fails_closed_before_camera_work(monkeypatch, tmp_path):
    key = write_clip(tmp_path)
    (tmp_path / ".local_media.json").write_text("{bad", encoding="utf-8")
    client = make_client(monkeypatch, tmp_path, media_sync_enabled=True, media_retention_hours=1)
    camera = Mock()
    monkeypatch.setattr(client, "refresh_recent_recordings", camera)
    with pytest.raises(RuntimeError, match="deletion history"):
        client.sync_recordings()
    assert client._local_policy().path(key).exists()
    camera.assert_not_called()


def test_missing_storage_does_not_mark_all_files_deleted(tmp_path):
    key = write_clip(tmp_path, thumbnail=False)
    state = LocalMediaState(tmp_path, tmp_path / "history.json")
    state.remember(key)
    state.path(key).unlink()
    (tmp_path / "videos").rmdir()
    assert state.maintain(None, None, {}, LOG)["storage_unavailable"]
    assert key not in state.deleted
    with pytest.raises(RuntimeError, match="storage is unavailable"):
        state.is_deleted(key)


def test_missing_storage_stops_downloads_without_contacting_cameras(monkeypatch, tmp_path):
    key = write_clip(tmp_path, thumbnail=False)
    client = make_client(monkeypatch, tmp_path, media_sync_enabled=True)
    client.maintain_local_media()
    client._local_policy().path(key).unlink()
    (tmp_path / "videos").rmdir()
    camera = Mock(side_effect=AssertionError("No camera work without local storage"))
    monkeypatch.setattr(client, "refresh_recent_recordings", camera)
    assert client.sync_recordings()["storage_unavailable"]
    assert client._media_sync_status.state == "storage_unavailable"
    assert key not in client._local_policy().deleted
    camera.assert_not_called()


def test_failed_history_write_keeps_file(monkeypatch, tmp_path):
    key = write_clip(tmp_path)
    state = LocalMediaState(tmp_path, tmp_path / "history.json")
    monkeypatch.setattr(state, "_save", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        state.delete(key)
    assert state.path(key).exists()


def test_folder_symlink_cannot_delete_outside_storage(monkeypatch, tmp_path):
    outside = tmp_path / "outside"
    key = write_clip(outside, thumbnail=False)
    root = tmp_path / "root"
    root.mkdir()
    (root / "videos").symlink_to(outside / "videos", target_is_directory=True)
    client = make_client(monkeypatch, root, media_retention_hours=1)
    for method in [client.clear_video_cache, client.maintain_local_media]:
        with pytest.raises(ValueError, match="folder"):
            method()
    assert LocalMediaState(outside, outside / "state.json").path(key).exists()


def test_local_timer_runs_with_sync_off_paused_and_backend_unavailable(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path, media_retention_hours=1)
    client.cloud_activity_paused = True
    key = write_clip(tmp_path)
    entry = SimpleNamespace(entry_id="entry")
    data = {"client": client}
    async def execute(function, *args):
        return function(*args)
    hass = SimpleNamespace(data={DOMAIN: {"entry": data}}, async_add_executor_job=execute)
    callbacks, cancellations = [], []
    monkeypatch.setattr(integration, "_client_recording_backend_available", lambda client: False)
    monkeypatch.setattr(integration, "async_track_time_interval", lambda hass, callback, interval:
                        callbacks.append(callback) or (lambda: cancellations.append("interval")))
    monkeypatch.setattr(integration, "async_call_later", lambda *args: lambda: cancellations.append("startup"))
    monkeypatch.setattr(integration, "async_dispatcher_send", Mock())
    integration._async_schedule_media_sync(hass, entry)
    assert len(callbacks) == 1
    asyncio.run(callbacks[0](None))
    assert not client._local_policy().path(key).exists()
    integration._async_cancel_media_sync_schedule(data)
    assert cancellations == ["interval", "startup"]


def test_legacy_window_is_retention_default_but_explicit_zero_wins(monkeypatch, tmp_path):
    assert make_client(monkeypatch, tmp_path, media_sync_hours=24).media_retention_hours == 24
    assert make_client(monkeypatch, tmp_path, media_sync_hours=24,
                       media_retention_hours=0).media_retention_hours == 0


def test_clear_index_does_not_clear_deletion_history(monkeypatch, tmp_path):
    root = tmp_path / "media"
    key = write_clip(root, RECENT)
    client = make_client(monkeypatch, root)
    client._cache_path = tmp_path / "private" / "catalog.json"
    client._camera_inventory = {"camera": {}}
    client.delete_local_recording(key.dev_id, key.start, key.end)
    state_path = client._local_policy().state_path
    client.clear_cache()
    assert state_path.exists()
    restarted = make_client(monkeypatch, root)
    restarted._cache_path = client._cache_path
    assert restarted.local_recording_status(key.dev_id, key.start, key.end) == "deleted"


def test_maintenance_continues_after_one_unlink_fails(monkeypatch, tmp_path):
    first = write_clip(tmp_path, dev="first")
    second = write_clip(tmp_path, dev="second")
    state = LocalMediaState(tmp_path, tmp_path / "history.json")
    path_type = type(tmp_path)
    unlink = path_type.unlink
    def fail_first(path, *args, **kwargs):
        if path == state.path(first):
            raise PermissionError("busy file")
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(path_type, "unlink", fail_first)
    result = state.maintain(NOW - 3600, None, {}, LOG)
    assert result["deleted_videos"] == 1
    assert state.path(first).exists()
    assert not state.path(second).exists()


def test_bulk_delete_persists_once_before_removing_files(monkeypatch, tmp_path):
    keys = [write_clip(tmp_path, RECENT + index * 30) for index in range(10)]
    client = make_client(monkeypatch, tmp_path)
    state = client._local_policy()
    save = state._save
    calls = []
    def persist():
        assert all(state.path(key).exists() for key in keys)
        calls.append("save")
        save()
    monkeypatch.setattr(state, "_save", persist)
    assert client.clear_video_cache()["deleted_videos"] == 10
    assert calls == ["save"]
    loaded = LocalMediaState(tmp_path, state.state_path)
    assert all(loaded.is_deleted(key) for key in keys)


def test_services_delete_only_selected_account_and_restore_without_download(monkeypatch, tmp_path):
    import voluptuous as vol

    target_root, other_root = tmp_path / "target", tmp_path / "other"
    key = write_clip(target_root, RECENT)
    write_clip(other_root, RECENT)
    target = make_client(monkeypatch, target_root)
    other = make_client(monkeypatch, other_root)
    registered = {}
    def register(domain, name, handler, *, schema):
        registered[name] = (handler, schema)
    async def execute(function, *args):
        return function(*args)
    hass = SimpleNamespace(
        services=SimpleNamespace(has_service=lambda *args: False, async_register=register),
        data={DOMAIN: {entry: {"client": client, "entry": SimpleNamespace(entry_id=entry)}
                       for entry, client in [("target", target), ("other", other)]}},
        async_add_executor_job=execute,
    )
    monkeypatch.setattr(integration, "async_dispatcher_send", Mock())
    integration._async_register_services(hass)
    handler, schema = registered["delete_local_recording"]
    params = {"entry_id": "target", "dev_id": "camera", "start": key.start, "end": key.end}
    with pytest.raises(vol.Invalid):
        schema({**params, "entry_id": ""})
    asyncio.run(handler(SimpleNamespace(data=schema(params))))
    assert not target._local_policy().path(key).exists()
    assert other._local_policy().path(key).exists()
    assert target.local_recording_status("camera", key.start, key.end) == "deleted"
    handler, schema = registered["allow_local_recording_downloads"]
    asyncio.run(handler(SimpleNamespace(data=schema(params))))
    assert target.local_recording_status("camera", key.start, key.end) == "pending"
    assert not target._local_policy().path(key).exists()
