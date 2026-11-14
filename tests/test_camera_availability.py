"""Unavailable cameras retain their catalog without stopping healthy transfers."""

from unittest.mock import Mock

import pytest

from custom_components.tuya_recordings import client as module
from custom_components.tuya_recordings.http import build_panel_data
from custom_components.tuya_recordings.lib.commands import CameraCommandQueue
from custom_components.tuya_recordings.lib.native_relay_playback import NativeSdStatusTimeout
from custom_components.tuya_recordings.lib.native_gateway import NativeGatewayDeviceOfflineError


def wrapped(error):
    result = RuntimeError("backend wrapper")
    result.__cause__ = error
    return result


@pytest.mark.parametrize("error,status", [
    (NativeSdStatusTimeout("private diagnostic"), "not_responding"),
    (NativeGatewayDeviceOfflineError("private diagnostic"), "offline"),
    (RuntimeError("invalid media"), ""),
])
def test_unavailability_uses_typed_evidence_not_error_text(error, status):
    assert module._camera_unavailable_status(wrapped(error)) == status
    assert module._camera_unavailable_status(RuntimeError("Camera did not report SD status after wake")) == ""
    error.__cause__ = error
    assert module._camera_unavailable_status(error) == status


@pytest.mark.parametrize("error,status", [
    (NativeSdStatusTimeout("private diagnostic"), "not_responding"),
    (NativeGatewayDeviceOfflineError("private diagnostic"), "offline"),
])
def test_failed_browse_preserves_catalog_and_success_clears_status(tmp_path, error, status):
    client = module.TuyaRecordingsClient({}, cache_path=tmp_path / "catalog.json",
                                          media_storage_path=tmp_path / "media")
    clip = {"start": 100, "end": 110, "date": "2026-11-14"}
    client._camera_index_cache = {"cameras": [{"devId": "camera", "online": True, "clips": [clip]}]}
    with client._refresh_lock:
        with pytest.raises(RuntimeError), client._browse_camera_query("camera"):
            raise wrapped(error)
    data = build_panel_data(client, client.cached_camera_index())
    assert data["cameras"][0]["catalog_status"] == status
    assert client.cached_camera_index()["cameras"][0]["clips"] == [clip]
    assert "private diagnostic" not in str(data)
    assert client.cached_camera_index()["cameras"][0]["online"] is True
    with client._refresh_lock, client._browse_camera_query("camera"):
        pass
    assert client.camera_catalog_status("camera") == "ready"
    assert not client.browse_refresh_failed("camera")
    assert client.cached_camera_index()["cameras"][0]["clips"] == [clip]


@pytest.mark.parametrize("failure", ["timeout", "offline", "known_offline", "invalid_media"])
def test_sync_continues_other_cameras_only_for_unavailability(monkeypatch, tmp_path, failure):
    client = module.TuyaRecordingsClient({"media_sync_enabled": True}, media_storage_path=tmp_path)
    index = {"cameras": [
        {"devId": "missing", "online": failure != "known_offline",
         "clips": [{"start": 100, "end": 110}, {"start": 80, "end": 90}]},
        {"devId": "healthy", "online": True, "clips": [{"start": 50, "end": 60}]},
    ]}
    client._camera_index_cache = index
    monkeypatch.setattr(client, "refresh_recent_recordings", lambda: index)
    monkeypatch.setattr(client, "_recover_interrupted_media_sync", lambda: 0)
    monkeypatch.setattr(module, "MEDIA_SYNC_INTER_ATTEMPT_DELAY", 0)
    monkeypatch.setattr(module, "CAMERA_COMMANDS", CameraCommandQueue())
    cleanup = Mock(return_value={})
    monkeypatch.setattr(client, "cleanup_cached_media", cleanup)
    calls = []

    def download(dev, *args, **kwargs):
        calls.append(dev)
        if dev == "missing":
            error = (NativeSdStatusTimeout("private diagnostic") if failure == "timeout"
                     else NativeGatewayDeviceOfflineError("private diagnostic") if failure == "offline"
                     else RuntimeError("invalid media"))
            raise wrapped(error)

    monkeypatch.setattr(client, "download_clip", download)
    result = client.sync_recordings()
    if failure == "invalid_media":
        assert calls == ["missing"] and result["failed"] == 1
        assert result["unavailable_cameras"] == []
    else:
        assert calls == (["healthy"] if failure == "known_offline" else ["missing", "healthy"])
        assert result["downloaded"] == 1 and result["failed"] == 0 and result["deferred"] == 2
        assert result["unavailable_cameras"] == ["missing"]
        assert client._media_failures == {}
        assert len(index["cameras"][0]["clips"]) == 2
        assert client._media_sync_status.to_dict()["state"] == "deferred"
        assert client._media_sync_status.to_dict()["last_error"] is None
    cleanup.assert_not_called()


@pytest.mark.parametrize("error,status", [
    (NativeSdStatusTimeout("private diagnostic"), "not_responding"),
    (NativeGatewayDeviceOfflineError("private diagnostic"), "offline"),
])
def test_automatic_catalog_retains_unavailable_camera_and_continues(monkeypatch, tmp_path, error, status):
    from custom_components.tuya_recordings.lib.commands import manual_catalog_refresh
    client = module.TuyaRecordingsClient({}, cache_path=tmp_path / "catalog.json")
    client.set_camera_inventory({"a_missing": "Missing", "z_healthy": "Healthy"})
    old = {"start": 100, "end": 110, "date": "1970-01-01"}
    client._camera_index_cache = {"cameras": [
        {"devId": "a_missing", "online": True, "clips": [old]},
        {"devId": "z_healthy", "online": True, "clips": []},
    ]}
    monkeypatch.setattr(module, "CAMERA_COMMANDS", CameraCommandQueue())
    calls = []

    def query(dev, day):
        calls.append(dev)
        if dev == "a_missing":
            raise wrapped(error)
        return []

    monkeypatch.setattr(client, "_recordings_for_day", query)
    index = client.refresh_background_catalog(2)
    assert calls == ["a_missing", "z_healthy"]
    assert index["cameras"][0]["clips"] == [old]
    assert index["cameras"][0]["bridgeStatus"]["status"] == status
    assert index["cameras"][0]["bridgeStatus"]["error"] == ""
    assert index["cameras"][1]["online"] is True
    monkeypatch.setattr(client, "_recordings_for_day", lambda *args: [])
    with manual_catalog_refresh():
        recovered = client.refresh_background_catalog(2)
    assert recovered["cameras"][0]["online"] is True
    assert recovered["cameras"][0]["bridgeStatus"].get("status") not in {"offline", "not_responding"}
    assert recovered["cameras"][0]["clips"] == [old]
