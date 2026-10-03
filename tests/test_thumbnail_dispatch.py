import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components import tuya_recordings as integration
from custom_components.tuya_recordings.client import TuyaRecordingsClient
from custom_components.tuya_recordings.http import TuyaRecordingsPlaybackView
from custom_components.tuya_recordings.lib.commands import CameraWorkBusy, CameraWorkCancelled


class FakeAvailableBackend:
    name = "apk-native"
    clip_playback_available = True

    def direct_thumbnail_available(self, dev_id):
        del dev_id
        return True


class FakeThumbnailBackend(FakeAvailableBackend):
    def __init__(self):
        self.requests = []
        self.stream_requests = []

    def receive_thumbnail(
        self, dev_id, config, auth, start, end, output_path
    ):
        self.requests.append((dev_id, config, auth, start, end))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"\xff\xd8direct-camera-jpeg\xff\xd9")

    def receive_thumbnail_from_stream(
        self, dev_id, config, auth, start, end, output_path, **kwargs
    ):
        self.stream_requests.append((dev_id, config, auth, start, end, kwargs))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"\xff\xd8stream-frame-jpeg\xff\xd9")


class BlockingThumbnailBackend(FakeThumbnailBackend):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def receive_thumbnail(self, *args):
        self.started.set()
        assert self.release.wait(2)
        super().receive_thumbnail(*args)


class PerCameraCatalogBackend(FakeAvailableBackend):
    def recordings_for_day(self, dev_id, config, auth, day):
        if dev_id == "camera-without-recordings":
            raise RuntimeError("No SD recording catalog")
        return [
            {
                "start": 100,
                "end": 110,
                "date": day.isoformat(),
                "raw": {},
            }
        ]


def test_catalog_failure_is_backed_off_per_camera_without_blocking_others():
    client = TuyaRecordingsClient({}, recordings_backend=PerCameraCatalogBackend())
    client.set_camera_inventory(
        {
            "camera-without-recordings": "No SD Camera",
            "camera-with-recordings": "SD Camera",
        }
    )

    index = client.refresh_background_catalog(limit=2)
    cameras = {camera["devId"]: camera for camera in index["cameras"]}

    assert "backgroundError" not in index
    assert cameras["camera-without-recordings"]["catalogErrorCount"] == 1
    assert cameras["camera-without-recordings"]["catalogRetryAfter"]
    assert cameras["camera-with-recordings"]["clips"][0]["start"] == 100


def test_thumbnail_failure_cools_down_only_that_camera(monkeypatch, tmp_path):
    client = TuyaRecordingsClient(
        {}, media_storage_path=tmp_path, recordings_backend=FakeAvailableBackend()
    )
    client.thumbnail_sync_enabled = True
    client.media_sync_enabled = True
    calls = []

    def create(dev_id, start, end, **kwargs):
        del kwargs
        calls.append((dev_id, start, end))
        if dev_id == "camera-failing":
            raise RuntimeError("JPEG request failed")
        path = client.thumbnail_path(dev_id, start, end)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xd8direct-camera-jpeg\xff\xd9")
        return path

    monkeypatch.setattr(client, "create_thumbnail", create)
    candidates = [
        ("camera-failing", {"start": 200, "end": 210, "raw": {"event_types": [1]}}),
        ("camera-working", {"start": 100, "end": 110, "raw": {"event_types": [1]}}),
    ]

    for dev_id, clip in candidates:
        path = client.clip_path(dev_id, clip["start"], clip["end"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048 + b"moov")

    first = client._populate_thumbnail_candidates(
        candidates, 2, ignore_catalog_backoff=True
    )
    second = client._populate_thumbnail_candidates(
        [("camera-failing", {"start": 200, "end": 210, "raw": {"event_types": [1]}})],
        1,
        ignore_catalog_backoff=True,
    )

    assert first["failed"] == 1
    assert first["created"] == 1
    assert calls == [
        ("camera-failing", 200, 210),
        ("camera-working", 100, 110),
    ]
    assert second["checked"] == 0
    assert second["skipped"] == 1


def test_missing_thumbnail_never_opens_a_camera_session(tmp_path):
    backend = FakeThumbnailBackend()
    client = TuyaRecordingsClient(
        {}, media_storage_path=tmp_path, recordings_backend=backend
    )
    client._camera_index_cache = {"cameras": [{
        "devId": "camera",
        "clips": [{"start": 100, "end": 110, "raw": {"event_types": [1]}}],
    }]}

    first = client.ensure_thumbnail("camera", 100, 110)
    second = client.ensure_thumbnail("camera", 100, 110)

    assert first is second is None
    assert backend.requests == []
    assert backend.stream_requests == []


def test_uncached_thumbnail_does_not_start_a_camera_command(tmp_path):
    class FailingDirectBackend(FakeThumbnailBackend):
        def receive_thumbnail(self, *args):
            self.requests.append(args[:5])
            raise RuntimeError("direct JPEG failed")

    backend = FailingDirectBackend()
    client = TuyaRecordingsClient(
        {}, media_storage_path=tmp_path, recordings_backend=backend
    )
    client._camera_index_cache = {"cameras": [{
        "devId": "camera",
        "clips": [{"start": 100, "end": 110, "raw": {"event_types": [1]}}],
    }]}

    assert client.ensure_thumbnail("camera", 100, 110) is None
    assert backend.requests == []
    assert backend.stream_requests == []


def test_concurrent_thumbnail_requests_do_not_start_camera_commands(tmp_path):
    backend = BlockingThumbnailBackend()
    client = TuyaRecordingsClient(
        {}, media_storage_path=tmp_path, recordings_backend=backend
    )
    client._camera_index_cache = {"cameras": [{
        "devId": "camera",
        "clips": [{"start": 100, "end": 110, "raw": {"event_types": [1]}}],
    }]}
    assert client.ensure_thumbnail("camera", 100, 110) is None
    assert client.ensure_thumbnail("camera", 100, 110) is None
    assert backend.requests == []
    assert backend.stream_requests == []


def test_ordinary_catalog_clip_does_not_use_playback_frame_extraction(tmp_path):
    backend = FakeThumbnailBackend()
    client = TuyaRecordingsClient(
        {}, media_storage_path=tmp_path, recordings_backend=backend
    )
    client._camera_index_cache = {"cameras": [{
        "devId": "camera",
        "clips": [{"start": 100, "end": 110, "raw": {"event_types": []}}],
    }]}

    output = client.ensure_thumbnail("camera", 100, 110)

    assert output is None
    assert backend.requests == []
    assert backend.stream_requests == []


def test_uncached_clip_never_uses_playback_frame_for_thumbnail(tmp_path):
    class NoDirectJpegBackend(FakeThumbnailBackend):
        def direct_thumbnail_available(self, dev_id):
            del dev_id
            return False

    backend = NoDirectJpegBackend()
    client = TuyaRecordingsClient(
        {}, media_storage_path=tmp_path, recordings_backend=backend
    )
    client._camera_index_cache = {"cameras": [{
        "devId": "camera",
        "clips": [{"start": 100, "end": 110, "raw": {}}],
    }]}

    result = client.ensure_thumbnail("camera", 100, 110)

    assert result is None
    assert backend.requests == []
    assert backend.stream_requests == []


def test_cached_thumbnail_is_returned_without_event_metadata(tmp_path):
    backend = FakeThumbnailBackend()
    client = TuyaRecordingsClient(
        {}, media_storage_path=tmp_path, recordings_backend=backend
    )
    path = client.thumbnail_path("camera", 100, 110)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"jpeg")

    assert client.ensure_thumbnail("camera", 100, 110) == path
    assert backend.requests == []


def test_cached_clip_thumbnail_is_local_and_never_contacts_camera(monkeypatch, tmp_path):
    backend = FakeThumbnailBackend()
    client = TuyaRecordingsClient(
        {}, media_storage_path=tmp_path, recordings_backend=backend
    )
    client._camera_index_cache = {"cameras": [{
        "devId": "camera",
        "clips": [{"start": 100, "end": 110, "raw": {"event_types": [1]}}],
    }]}
    clip_path = client.clip_path("camera", 100, 110)
    clip_path.parent.mkdir(parents=True, exist_ok=True)
    clip_path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048 + b"moov")
    local_calls = []

    def extract_local(source, output):
        local_calls.append((source, output))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"\xff\xd8local-jpeg\xff\xd9")

    monkeypatch.setattr(
        "custom_components.tuya_recordings.client.extract_thumbnail_from_mp4",
        extract_local,
    )

    result = client.ensure_thumbnail("camera", 100, 110)

    assert result == tmp_path / "thumbs" / "camera_100_110.jpg"
    assert local_calls == [(clip_path, result)]
    assert backend.requests == []
    assert backend.stream_requests == []


def test_cached_clip_bypasses_camera_and_catalog_backoff(monkeypatch, tmp_path):
    backend = FakeThumbnailBackend()
    client = TuyaRecordingsClient(
        {}, media_storage_path=tmp_path, recordings_backend=backend
    )
    client.thumbnail_sync_enabled = True
    client.media_sync_enabled = True
    clip = {"start": 100, "end": 110, "raw": {}}
    client._camera_index_cache = {
        "cameras": [{"devId": "camera", "online": True, "clips": [clip]}]
    }
    client._thumbnail_camera_failures["camera"] = float("inf")
    clip_path = client.clip_path("camera", 100, 110)
    clip_path.parent.mkdir(parents=True, exist_ok=True)
    clip_path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048 + b"moov")

    def extract_local(source, output):
        assert source == clip_path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"\xff\xd8local-jpeg\xff\xd9")

    monkeypatch.setattr(
        "custom_components.tuya_recordings.client.extract_thumbnail_from_mp4",
        extract_local,
    )
    result = client._populate_thumbnail_candidates([("camera", clip)], 1)

    assert result["created"] == 1
    assert result["failed"] == 0
    assert client._thumbnail_camera_failures == {"camera": float("inf")}
    assert backend.requests == []
    assert backend.stream_requests == []


def test_local_thumbnail_failure_does_not_cool_down_camera(monkeypatch, tmp_path):
    client = TuyaRecordingsClient(
        {}, media_storage_path=tmp_path, recordings_backend=FakeThumbnailBackend()
    )
    client.thumbnail_sync_enabled = True
    client.media_sync_enabled = True
    clip = {"start": 100, "end": 110, "raw": {}}
    client._camera_index_cache = {
        "cameras": [{"devId": "camera", "online": True, "clips": [clip]}]
    }
    clip_path = client.clip_path("camera", 100, 110)
    clip_path.parent.mkdir(parents=True, exist_ok=True)
    clip_path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048 + b"moov")
    monkeypatch.setattr(
        "custom_components.tuya_recordings.client.extract_thumbnail_from_mp4",
        Mock(side_effect=RuntimeError("local decoder failed")),
    )

    result = client._populate_thumbnail_candidates([("camera", clip)], 1)

    assert result["failed"] == 1
    assert client._thumbnail_camera_failures == {}
    assert ("camera", 100, 110) in client._thumbnail_failures




def test_background_thumbnail_sync_skips_uncached_clips(tmp_path):
    class NoDirectJpegBackend(FakeThumbnailBackend):
        def direct_thumbnail_available(self, dev_id):
            del dev_id
            return False

    backend = NoDirectJpegBackend()
    client = TuyaRecordingsClient(
        {}, media_storage_path=tmp_path, recordings_backend=backend
    )
    client.thumbnail_sync_enabled = True
    client.media_sync_enabled = True
    clip = {"start": 100, "end": 110, "raw": {"event_types": []}}
    client._camera_index_cache = {
        "cameras": [{"devId": "camera", "online": True, "clips": [clip]}]
    }

    result = client._populate_thumbnail_candidates([("camera", clip)], 1)

    assert result == {
        "created": 0,
        "skipped": 1,
        "failed": 0,
        "checked": 0,
        "limit": 1,
    }
    assert backend.requests == []
    assert backend.stream_requests == []


def test_playback_view_has_no_post_playback_thumbnail_dispatch():
    assert not hasattr(TuyaRecordingsPlaybackView, "_schedule_thumbnail")


@pytest.mark.parametrize("outcome", ["busy", "cancel", "closed", "success", "auth"])
def test_thumbnail_dispatch_finishes_cleanly(monkeypatch, outcome):
    client = TuyaRecordingsClient({})
    client.thumbnail_sync_enabled = True
    client.media_sync_enabled = True
    client.media_sync_enabled = True
    dispatch = Mock()
    reauth = Mock()
    monkeypatch.setattr(integration, "async_dispatcher_send", dispatch)
    data = {
        "client": client,
        "entry": SimpleNamespace(async_start_reauth=reauth),
        integration.DATA_THUMBNAIL_SYNC_PENDING: True,
        integration.DATA_THUMBNAIL_SYNC_REQUIRE_MEDIA: False,
    }

    async def execute(function, *args):
        if outcome == "busy":
            raise CameraWorkBusy("Synthetic busy queue")
        if outcome == "cancel":
            raise CameraWorkCancelled("Synthetic cancelled operation")
        if outcome == "auth":
            raise integration.TuyaRecordingsAuthError("test", {"code": "invalid_token"})
        if outcome == "closed":
            client.close()
        return {"created": 0}

    hass = SimpleNamespace(data={integration.DOMAIN: {"entry": data}}, async_add_executor_job=execute)
    operation = integration._async_run_camera_work(hass, "entry", "test")
    if outcome == "auth":
        with pytest.raises(integration.ConfigEntryAuthFailed):
            asyncio.run(operation)
    else:
        asyncio.run(operation)
    assert not data[integration.DATA_THUMBNAIL_SYNC_RUNNING]
    assert not data.get(integration.DATA_THUMBNAIL_SYNC_PENDING)
    assert dispatch.call_count == int(outcome == "success")
    assert reauth.call_count == int(outcome == "auth")


def test_catalog_is_refreshed_before_thumbnail_work(monkeypatch):
    client = TuyaRecordingsClient({})
    client.thumbnail_sync_enabled = True
    client.media_sync_enabled = True
    calls = []
    monkeypatch.setattr(integration, "async_dispatcher_send", Mock())
    data = {"client": client, integration.DATA_THUMBNAIL_SYNC_PENDING: True,
            integration.DATA_THUMBNAIL_SYNC_REQUIRE_MEDIA: False}

    async def execute(function, *args):
        calls.append((function.__name__, args[-1]))
        return {"created": 0}

    hass = SimpleNamespace(data={integration.DOMAIN: {"entry": data}}, async_add_executor_job=execute)
    asyncio.run(integration._async_run_camera_work(hass, "entry", "test"))
    assert calls == [("sync_thumbnails", True)]


def test_cached_catalog_thumbnail_work_skips_catalog_refresh(monkeypatch):
    client = TuyaRecordingsClient({})
    client.thumbnail_sync_enabled = True
    client.media_sync_enabled = True
    calls = []
    monkeypatch.setattr(integration, "async_dispatcher_send", Mock())
    data = {
        "client": client,
        integration.DATA_THUMBNAIL_SYNC_PENDING: True,
        integration.DATA_THUMBNAIL_SYNC_REQUIRE_MEDIA: False,
        integration.DATA_THUMBNAIL_SYNC_REFRESH_CATALOG: False,
    }

    async def execute(function, *args):
        calls.append((function.__name__, args[-1]))
        return {"checked": 1, "created": 1}

    hass = SimpleNamespace(
        data={integration.DOMAIN: {"entry": data}},
        async_add_executor_job=execute,
        loop=SimpleNamespace(time=lambda: 1000),
    )
    asyncio.run(integration._async_run_camera_work(hass, "entry", "service"))
    assert calls == [("sync_thumbnails", False)]


def test_cached_catalog_thumbnail_work_bypasses_catalog_backoff(monkeypatch):
    client = TuyaRecordingsClient({})
    client.thumbnail_sync_enabled = True
    client.media_sync_enabled = True
    populate = Mock(return_value={"checked": 1, "created": 1})
    refresh = Mock(side_effect=AssertionError("cached thumbnail work refreshed catalog"))
    monkeypatch.setattr(client, "populate_thumbnails", populate)
    monkeypatch.setattr(client, "refresh_background_catalog", refresh)

    result = client.sync_thumbnails(
        1,
        client.thumbnail_work_cancellation(False),
        False,
    )

    assert result == {"checked": 1, "created": 1}
    populate.assert_called_once_with(1, ignore_catalog_backoff=True)
    refresh.assert_not_called()


def test_thumbnail_work_never_refreshes_catalog(monkeypatch):
    client = TuyaRecordingsClient({})
    client.thumbnail_sync_enabled = True
    client.media_sync_enabled = True
    populate = Mock(return_value={"checked": 1, "created": 1})
    refresh = Mock(return_value={"cameras": []})
    monkeypatch.setattr(client, "populate_thumbnails", populate)
    monkeypatch.setattr(client, "refresh_background_catalog", refresh)

    result = client.sync_thumbnails(
        1,
        client.thumbnail_work_cancellation(False),
        True,
    )

    assert result == {"checked": 1, "created": 1}
    refresh.assert_not_called()
    populate.assert_called_once_with(1, ignore_catalog_backoff=True)


def test_thumbnail_success_sets_background_cooldown(monkeypatch):
    client = TuyaRecordingsClient({})
    client.thumbnail_sync_enabled = True
    client.media_sync_enabled = True
    monkeypatch.setattr(integration, "async_dispatcher_send", Mock())
    data = {"client": client, integration.DATA_THUMBNAIL_SYNC_PENDING: True,
            integration.DATA_THUMBNAIL_SYNC_REQUIRE_MEDIA: False}

    async def execute(function, *args):
        return {"checked": 1, "created": 1}

    hass = SimpleNamespace(
        data={integration.DOMAIN: {"entry": data}},
        async_add_executor_job=execute,
        loop=SimpleNamespace(time=lambda: 1000),
    )
    asyncio.run(integration._async_run_camera_work(hass, "entry", "test"))
    assert data[integration.DATA_THUMBNAIL_SYNC_RETRY_AFTER] == 1000 + integration.THUMBNAIL_BACKGROUND_COOLDOWN


def test_pause_leaves_thumbnail_cooldown_for_next_unpause():
    resume_timer = Mock()
    data = {
        integration.DATA_MEDIA_SYNC_PENDING: True,
        integration.DATA_THUMBNAIL_SYNC_PENDING: True,
        integration.DATA_THUMBNAIL_SYNC_LIMIT: 10,
        integration.DATA_THUMBNAIL_SYNC_REQUIRE_MEDIA: False,
        integration.DATA_THUMBNAIL_SYNC_RETRY_AFTER: 0,
        integration.DATA_CAMERA_RESUME_TIMER: resume_timer,
    }
    hass = SimpleNamespace(
        data={integration.DOMAIN: {"entry": data}},
        loop=SimpleNamespace(time=lambda: 5000),
    )
    asyncio.run(integration.async_pause_camera_work(hass, "entry"))
    assert data[integration.DATA_MEDIA_SYNC_PENDING] is False
    assert data[integration.DATA_THUMBNAIL_SYNC_PENDING] is False
    assert integration.DATA_THUMBNAIL_SYNC_LIMIT not in data
    assert integration.DATA_THUMBNAIL_SYNC_REQUIRE_MEDIA not in data
    assert integration.DATA_CAMERA_RESUME_TIMER not in data
    resume_timer.assert_called_once_with()
    assert data[integration.DATA_THUMBNAIL_SYNC_RETRY_AFTER] == 5000 + integration.THUMBNAIL_BACKGROUND_COOLDOWN


def test_unpause_does_not_queue_background_work_in_timeline_mode(monkeypatch):
    client = TuyaRecordingsClient({}, recordings_backend=FakeAvailableBackend())
    data = {
        "client": client,
        integration.DATA_THUMBNAIL_SYNC_RETRY_AFTER: 9999,
        integration.DATA_CAMERA_WORK_TASK: SimpleNamespace(done=lambda: False),
    }
    async def execute(function, *args):
        return function(*args)

    hass = SimpleNamespace(
        data={integration.DOMAIN: {"entry": data}},
        async_add_executor_job=execute,
    )

    asyncio.run(integration.async_resume_camera_work(hass, "entry"))

    assert data[integration.DATA_THUMBNAIL_SYNC_RETRY_AFTER] == 9999
    assert integration.DATA_CAMERA_RESUME_TIMER not in data
    assert integration.DATA_THUMBNAIL_SYNC_PENDING not in data


def test_explicit_resume_clears_persisted_camera_backoff(tmp_path):
    cache_path = tmp_path / "recordings.json"
    client = TuyaRecordingsClient({}, cache_path=cache_path)
    client._camera_index_cache = {
        "cameras": [{
            "devId": "camera",
            "catalogErrorAt": "2026-09-19T10:00:00+00:00",
            "catalogErrorCount": 3,
            "catalogRetryAfter": "2026-09-19T10:15:00+00:00",
            "bridgeStatus": {"error": "no answer", "retryAfter": "later", "listedCount": 4},
        }]
    }
    client._thumbnail_camera_failures["camera"] = 100.0

    client.reset_camera_work_backoff()

    camera = client._camera_index_cache["cameras"][0]
    assert "catalogErrorAt" not in camera
    assert "catalogErrorCount" not in camera
    assert "catalogRetryAfter" not in camera
    assert camera["bridgeStatus"] == {"listedCount": 4}
    assert client._thumbnail_camera_failures == {}


def test_recent_catalog_failure_suppresses_next_thumbnail_request(monkeypatch):
    client = TuyaRecordingsClient({})
    client.thumbnail_sync_enabled = True
    create = Mock(side_effect=AssertionError("Recent catalog failure queued more camera work"))
    data = {
        "client": client,
        integration.DATA_THUMBNAIL_SYNC_RETRY_AFTER: 2000,
    }
    hass = SimpleNamespace(
        data={integration.DOMAIN: {"entry": data}},
        async_create_task=create,
        loop=SimpleNamespace(time=lambda: 1000),
    )
    asyncio.run(integration._async_request_camera_work(
        hass,
        "entry",
        "interval",
        thumbnails=True,
        require_media_sync=False,
    ))
    create.assert_not_called()
    assert integration.DATA_THUMBNAIL_SYNC_PENDING not in data


def test_background_requests_are_bounded_and_merged():
    client = TuyaRecordingsClient({}, recordings_backend=FakeAvailableBackend())
    client.thumbnail_sync_enabled = True
    client.media_sync_enabled = True
    data = {"client": client, integration.DATA_CAMERA_WORK_TASK: SimpleNamespace(done=lambda: False)}
    hass = SimpleNamespace(data={integration.DOMAIN: {"entry": data}})

    async def requests():
        for limit in (10, 0, 20):
            await integration._async_request_camera_work(
                hass, "entry", "test", thumbnails=True, thumbnail_limit=limit, require_media_sync=False,
            )

    asyncio.run(requests())
    assert data[integration.DATA_THUMBNAIL_SYNC_LIMIT] == integration.THUMBNAIL_BACKGROUND_LIMIT


def test_dispatch_clamps_zero_limit_before_worker(monkeypatch):
    client = TuyaRecordingsClient({})
    client.thumbnail_sync_enabled = True
    client.media_sync_enabled = True
    received = []
    monkeypatch.setattr(integration, "async_dispatcher_send", Mock())
    data = {"client": client, integration.DATA_THUMBNAIL_SYNC_PENDING: True,
            integration.DATA_THUMBNAIL_SYNC_LIMIT: 0,
            integration.DATA_THUMBNAIL_SYNC_REQUIRE_MEDIA: False}

    async def execute(function, limit, cancellation, refresh_catalog):
        received.append(limit)
        assert refresh_catalog is True
        return {"created": 0}

    hass = SimpleNamespace(data={integration.DOMAIN: {"entry": data}}, async_add_executor_job=execute)
    asyncio.run(integration._async_run_camera_work(hass, "entry", "test"))
    assert received == [integration.THUMBNAIL_BACKGROUND_LIMIT]
