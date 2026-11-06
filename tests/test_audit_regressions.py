import json
import queue
from types import SimpleNamespace

import pytest

from custom_components.tuya_recordings.client import TuyaRecordingsClient
from custom_components.tuya_recordings.http import _TimelineCommands
from custom_components.tuya_recordings.http import _select_client, _resume_background_camera_work
from custom_components.tuya_recordings.const import DOMAIN, DATA_INTERACTIVE_PLAYBACK_ACTIVE
from custom_components.tuya_recordings.lib.native_v4 import NativeV4AIDetection
from custom_components.tuya_recordings.media_source import TuyaRecordingsMediaSource


def test_ai_icon_is_optional():
    assert NativeV4AIDetection(100, 110, "ipc_human").as_catalog_item() == {
        "startTime": 100, "endTime": 110, "aiCode": "ipc_human"}


@pytest.mark.parametrize("payload", [None, [], "invalid"])
def test_cache_accepts_only_object_envelope(tmp_path, payload):
    path = tmp_path / "recordings.json"
    path.write_text(json.dumps(payload))
    client = TuyaRecordingsClient({}, cache_path=path)
    client.load_cache()
    assert client._camera_index_cache is None


def test_control_queue_coalesces_seeks_and_always_admits_stop():
    commands = _TimelineCommands()
    for position in range(10000):
        commands.put(("seek", position))
    assert commands.qsize() == 1
    assert commands.get_nowait() == ("seek", 9999)
    for _ in range(32):
        commands.put(("pause", None))
    with pytest.raises(queue.Full):
        commands.put(("resume", None))
    commands.put(("stop", None))
    assert commands.qsize() == 1
    assert commands.get_nowait() == ("stop", None)


def test_media_clock_uses_server_locale_not_other_users_storage():
    hass = SimpleNamespace(config=SimpleNamespace(language="en-CA"))
    media = TuyaRecordingsMediaSource(hass)
    assert media._use_12h_time()


def test_saved_inventory_is_not_evidence_of_camera_online():
    client = TuyaRecordingsClient({})
    client.set_camera_inventory({"camera": "Camera"})
    assert client._camera_devices()[0]["online"] is None


def test_multiple_accounts_require_unambiguous_camera_owner():
    from aiohttp import web

    first = TuyaRecordingsClient({})
    second = TuyaRecordingsClient({})
    first.set_camera_inventory({"first-camera": "First"})
    second.set_camera_inventory({"second-camera": "Second"})
    hass = SimpleNamespace(data={DOMAIN: {
        "first": {"client": first}, "second": {"client": second},
    }})
    assert _select_client(hass, dev_id="second-camera") is second
    assert _select_client(hass, entry_id="first") is first
    with pytest.raises(web.HTTPBadRequest):
        _select_client(hass, dev_id="unknown-camera")
    second.set_camera_inventory({"first-camera": "Duplicate"})
    with pytest.raises(web.HTTPBadRequest):
        _select_client(hass, dev_id="first-camera")


def test_playback_cleanup_resumes_only_owning_account_generation():
    from unittest.mock import Mock

    first = SimpleNamespace(resume_background_work=Mock())
    second = SimpleNamespace(resume_background_work=Mock())
    first_data = {"client": first, DATA_INTERACTIVE_PLAYBACK_ACTIVE: 1}
    second_data = {"client": second, DATA_INTERACTIVE_PLAYBACK_ACTIVE: 2}
    hass = SimpleNamespace(data={DOMAIN: {"first": first_data, "second": second_data}})
    _resume_background_camera_work(hass, first, 0)
    first.resume_background_work.assert_not_called()
    _resume_background_camera_work(hass, first, 1)
    first.resume_background_work.assert_called_once()
    assert second_data[DATA_INTERACTIVE_PLAYBACK_ACTIVE] == 2
    second.resume_background_work.assert_not_called()


def test_stale_response_never_renews_catalog_freshness(tmp_path):
    from datetime import datetime, timedelta, timezone

    client = TuyaRecordingsClient({}, cache_path=tmp_path / "catalog.json")
    client._camera_index_cache = {"cameras": [], "generatedAt": "original"}
    expiry = datetime.now(timezone.utc) - timedelta(minutes=10)
    client._camera_index_cache_until = expiry
    stale = client._stale_cache(datetime.now(timezone.utc), "camera busy")
    assert stale["stale"]
    assert stale["generatedAt"] == "original"
    assert client._camera_index_cache_until == expiry
    saved = json.loads((tmp_path / "catalog.json").read_text())
    assert abs((datetime.fromisoformat(saved["expiresAt"]) - expiry).total_seconds()) < 0.01


def test_inventory_refresh_rotates_backend_once_and_closes_gateway(monkeypatch):
    from unittest.mock import Mock
    import custom_components.tuya_recordings.client as module

    gateway = Mock()
    gateway.device_connection_inventory_data.return_value = (
        {"new-camera": "new-key"}, {"new-camera": "2.3"},
        [{"devId": "new-camera", "category": "sp", "name": "Camera"}],
    )
    factory = Mock(return_value=gateway)
    monkeypatch.setattr(module, "NativeAppGatewayClient", factory)
    old, new = Mock(), Mock()
    build = Mock(return_value=new)
    monkeypatch.setattr(module, "build_recordings_backend", build)
    client = TuyaRecordingsClient({"cloud_activity_paused": False}, recordings_backend=old)
    session = {"device_fingerprint": "fingerprint"}
    updates = client.refresh_inventory({
        "native_app_session": session, "device_protocol_versions": {"new-camera": "2.2", "removed": "2.1"},
    })
    gateway.device_connection_inventory_data.assert_called_once_with(session, refresh_protocols=True)
    assert updates["device_protocol_versions"] == {"new-camera": "2.3"}
    assert build.call_args.args[0]["device_protocol_versions"] == {"new-camera": "2.3"}
    gateway.session.close.assert_called_once()
    old.close.assert_called_once()
    assert client._recordings_backend is new
    assert updates["device_inventory"] == {"new-camera": "Camera"}
    assert client._camera_inventory == updates["device_inventory"]
    client.cloud_activity_paused = True
    with pytest.raises(RuntimeError, match="paused"):
        client.refresh_inventory({"native_app_session": session})
    assert factory.call_count == 1
