import asyncio
import json
import queue
import struct
import threading
import time
from dataclasses import replace
from datetime import date

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from custom_components.tuya_recordings.lib.native_media_crypto import (
    NativeMediaSecretStore,
)
from custom_components.tuya_recordings.lib.native_playback_stream import (
    PLAYBACK_CODEC_INFO_BYTES,
    NativePlaybackStreamParser,
)
from custom_components.tuya_recordings.lib.native_relay_runtime import (
    NativeRelayPlaybackWorker,
    NativeRelayRuntimeError,
    NativeRelayRuntimePool,
    NativeRelayRuntimeSession,
    NativeRelayStreamWorker,
)
from custom_components.tuya_recordings.lib.native_session import (
    NativeCameraSessionConfig,
    NativeMediaFormat,
    NativePlaybackRequest,
)
from custom_components.tuya_recordings.lib.native_thumbnail import NativeThumbnailReady

TOKEN = {
    "cloud_cid": "cloud",
    "cid": "camera-cid",
    "username": "user",
    "expired": 1,
    "crypt_algo": 2,
    "crypt_key": "crypt",
    "sign_algo": 2,
    "sign_key": "sign",
    "ice_token": {"servers": [{"url": "stun:example.test:3478"}]},
}
APP_SESSION = {
    "sid": "sid",
    "ecode": "ecode",
    "uid": "uid",
    "partner_identity": "partner",
    "mobile_mqtts_url": "mqtt.example.test",
    "device_fingerprint": "fingerprint",
}


def camera_config():
    return NativeCameraSessionConfig(
        dev_id="camera-device",
        local_key="0123456789abcdef",
        camera_password="password",
        token=json.dumps(TOKEN),
        skill=json.dumps({"localStorage": 33554432}),
        p2p_type=4,
        trace_id="trace",
        p2p_id="camera-device",
        local_id="user-id",
        config_json='{"pv":"2.3"}',
    )


def wire_frame(payload, *, frame_type, codec=None):
    info = b""
    flag = 0
    if codec is not None:
        flag = 1
        info = struct.pack(">6H", codec, 8, 1, 1, 0, 0) + bytes(
            PLAYBACK_CODEC_INFO_BYTES - 12
        )
    wire = struct.pack(">HH7I", frame_type, flag, len(payload), 1, 2, 3, 4, 5, 6)
    return NativePlaybackStreamParser().feed(wire + info + payload)[0]


def encrypted_wire_frame(payload, *, frame_type, codec, uuid, secret, iv):
    padding = 16 - len(payload) % 16
    padded = payload + bytes([padding]) * padding
    encryptor = Cipher(
        algorithms.AES(secret.encode()[:16]), modes.CBC(iv)
    ).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    info = bytearray(PLAYBACK_CODEC_INFO_BYTES)
    struct.pack_into(">6H", info, 0, codec, 8, 1, 1, 1, 0)
    info[12 : 12 + len(uuid)] = uuid.encode()
    info[44:60] = iv
    wire = struct.pack(">HH7I", frame_type, 1, len(encrypted), 1, 2, 3, 4, 5, 6)
    return NativePlaybackStreamParser().feed(wire + bytes(info) + encrypted)[0]


class Playback:
    local_cid = "local-cid"

    def __init__(self):
        self.frames = asyncio.Queue()
        self.started = None
        self.start_calls = []
        self.stop_count = 0
        self._playback_started = False
        self.closed = False
        self.queries = []
        self.event_queries = []
        self.thumbnails = []
        self.connect_modes = []

    async def connect(self, *, media=True):
        self.connected = True
        self.connect_modes.append(media)

    async def start(self, **kwargs):
        self.started = kwargs
        self.start_calls.append(kwargs)
        self._playback_started = True
        self.frames.put_nowait(
            wire_frame(
                b"\x00\x00\x00\x01\x67s"
                b"\x00\x00\x00\x01\x68p"
                b"\x00\x00\x00\x01\x65v",
                frame_type=1,
            )
        )
        self.frames.put_nowait(wire_frame(b"aac", frame_type=3, codec=4))

    async def stop(self):
        self._playback_started = False
        self.stop_count += 1

    async def query_recordings(self, day):
        from custom_components.tuya_recordings.lib.native_v4 import (
            NativeV4CatalogPage,
            NativeV4Recording,
        )

        self.query = day
        self.queries.append(day)
        return NativeV4CatalogPage(
            response_id=2,
            status=0,
            error_message="",
            total_pages=1,
            total_files=1,
            page=1,
            items=(NativeV4Recording(100, 110, event_type=2),),
        )

    async def query_event_recordings(self, day):
        self.event_queries.append(day)
        return await self.query_recordings(day)

    async def download_thumbnail(self, *, start, end):
        self.thumbnails.append((start, end))
        return b"\xff\xd8image\xff\xd9"

    async def receive_frame(self):
        return await self.frames.get()

    async def close(self):
        self.closed = True


class ReusablePlayback(Playback):
    instances = []

    def __init__(self):
        super().__init__()
        self._playback_started = False
        self.connect_count = 0
        self.start_count = 0
        type(self).instances.append(self)

    async def connect(self, *, media=True):
        self.connect_count += 1
        self.connect_modes.append(media)

    async def start(self, **kwargs):
        self._playback_started = True
        self.start_count += 1
        await super().start(**kwargs)

    async def stop(self):
        await super().stop()


class FailedConnectPlayback(ReusablePlayback):
    async def connect(self, *, media=True):
        self.connect_count += 1
        self.connect_modes.append(media)
        raise RuntimeError("no answer")


class DirectThumbnailPlayback(ReusablePlayback):
    async def download_thumbnail(self, *, start, end):
        self.thumbnails.append((start, end))
        return b"\xff\xd8image\xff\xd9"


class DelayedThumbnailErrorPlayback(ReusablePlayback):
    async def download_thumbnail(self, *, start, end):
        self.thumbnails.append((start, end))
        await asyncio.sleep(0.02)
        raise RuntimeError("thumbnail transport diagnostics")


class ReusesAudioCodecPlayback(Playback):
    async def start(self, **kwargs):
        self.started = kwargs
        self.frames.put_nowait(wire_frame(b"\x00\x00\x00\x01\x65v", frame_type=1))
        self.frames.put_nowait(wire_frame(b"aac-one", frame_type=3, codec=4))
        self.frames.put_nowait(wire_frame(b"aac-two", frame_type=3))


class HangingCatalogPlayback(Playback):
    transport_diagnostics = "frames=0xf4:2,0xf5:3, segments=0, decode_failures=0"

    async def query_recordings(self, day):
        await asyncio.Event().wait()


class HangingMediaPlayback(Playback):
    transport_diagnostics = (
        "transport=direct-ice, relay=disabled, session=(routed=0:0x81=4), "
        "control=(records=2,json_types=playback.record.view.control.resp=1)"
    )

    async def start(self, **kwargs):
        self.started = kwargs


class BlockingConnectPlayback(Playback):
    def __init__(self):
        super().__init__()
        self.connect_started = threading.Event()

    async def connect(self, *, media=True):
        self.connect_started.set()
        self.connect_modes.append(media)
        await asyncio.Event().wait()


class EncryptedPlayback(Playback):
    uuid = "clip-uuid"
    secret = "0123456789abcdef-extra"

    async def start(self, **kwargs):
        self.started = kwargs
        self.frames.put_nowait(
            encrypted_wire_frame(
                b"\x00\x00\x00\x01\x65video",
                frame_type=1,
                codec=1,
                uuid=self.uuid,
                secret=self.secret,
                iv=bytes(range(16)),
            )
        )
        self.frames.put_nowait(
            encrypted_wire_frame(
                b"aac",
                frame_type=3,
                codec=4,
                uuid=self.uuid,
                secret=self.secret,
                iv=bytes(range(16, 32)),
            )
        )


class EncryptedCatalogPlayback(Playback):
    async def query_recordings(self, day):
        from custom_components.tuya_recordings.lib.native_v4 import (
            NativeV4CatalogPage,
            NativeV4Recording,
        )

        self.queries.append(day)
        return NativeV4CatalogPage(
            response_id=2,
            status=0,
            error_message="",
            total_pages=1,
            total_files=1,
            page=1,
            items=(
                NativeV4Recording(
                    100,
                    110,
                    encrypt=1,
                    uuid="clip-uuid",
                    encrypt_md5="digest",
                ),
            ),
        )


def test_streams_each_clip_as_one_continuous_camera_request():
    playback = Playback()
    events = []
    session = NativeRelayRuntimeSession(
        camera_config(),
        app_session=APP_SESSION,
        end_idle_timeout=0.01,
        playback_factory=lambda *args, **kwargs: playback,
        clock=lambda: 1000.0,
    )

    session.start_playback_stream(
        NativePlaybackRequest(
            start=100,
            end=110,
            media_format=NativeMediaFormat.RAW_PACKETS,
        ),
        events.append,
    )

    assert [event["payload"]["type"] for event in events] == [
        "started",
        "media-codec",
        "media-codec",
        "finished",
    ]
    assert playback.start_calls == [
        {
            "start": 100,
            "end": 110,
            "play_time": 100,
            "encrypted": False,
            "fragments_json": "",
            "play_mode_supported": False,
        },
    ]
    assert playback.stop_count == 1
    assert playback.queries == []
    assert playback.closed is True


def test_worker_switches_clips_on_one_connected_transport():
    ReusablePlayback.instances.clear()
    worker = NativeRelayStreamWorker(
        camera_config(),
        APP_SESSION,
        playback_factory=lambda *args, **kwargs: ReusablePlayback(),
        end_idle_timeout=0.01,
    )
    events = []

    for start in (100, 120):
        worker.stream(
            NativePlaybackRequest(
                start=start,
                end=start + 10,
                media_format=NativeMediaFormat.RAW_PACKETS,
            ),
            events.append,
            None,
        )

    assert len(ReusablePlayback.instances) == 1
    playback = ReusablePlayback.instances[0]
    assert playback.connect_count == 1
    assert playback.connect_modes == [True]
    assert playback.start_count == 2
    assert playback.stop_count == 2
    assert [
        event["payload"]["type"]
        for event in events
        if event["payload"]["type"] in {"started", "finished"}
    ] == ["started", "finished", "started", "finished"]
    worker.close()
    worker._thread.join(2)
    assert not worker._thread.is_alive()
    assert playback.closed is True


def test_worker_treats_completed_thumbnail_as_graceful_playback_stop():
    class ThumbnailPlayback(ReusablePlayback):
        async def start(self, **kwargs):
            self.frames = asyncio.Queue()
            await super().start(**kwargs)

    ThumbnailPlayback.instances.clear()
    worker = NativeRelayStreamWorker(
        camera_config(),
        APP_SESSION,
        playback_factory=lambda *args, **kwargs: ThumbnailPlayback(),
        end_idle_timeout=0.01,
    )

    def stop_after_video(event):
        info = event["payload"].get("info") or {}
        if event["payload"]["type"] == "media-codec" and info.get("avChannel") == 0:
            raise NativeThumbnailReady

    request = NativePlaybackRequest(
        start=100,
        end=110,
        media_format=NativeMediaFormat.RAW_PACKETS,
    )
    worker.stream(request, stop_after_video, None)
    worker.stream(replace(request, start=120, end=130), lambda event: None, None)

    assert len(ThumbnailPlayback.instances) == 1
    playback = ThumbnailPlayback.instances[0]
    assert playback.connect_count == 1
    assert playback.start_count == 2
    assert playback.stop_count == 2
    worker.close()
    assert worker.wait_closed(2)


def test_worker_downloads_thumbnails_sequentially_on_one_transport():
    ReusablePlayback.instances.clear()

    worker = NativeRelayPlaybackWorker(
        camera_config(),
        APP_SESSION,
        playback_factory=lambda *args, **kwargs: DirectThumbnailPlayback(),
        end_idle_timeout=0.01,
    )

    first = worker.thumbnail(100, 110, None)
    second = worker.thumbnail(120, 130, None)

    assert first == b"\xff\xd8image\xff\xd9"
    assert second == b"\xff\xd8image\xff\xd9"
    assert len(DirectThumbnailPlayback.instances) == 1
    playback = DirectThumbnailPlayback.instances[0]
    assert playback.connect_count == 1
    assert playback.connect_modes == [True]
    assert playback.start_count == 0
    assert playback.stop_count == 0
    assert playback.thumbnails == [(100, 110), (120, 130)]
    worker.close()
    worker._thread.join(2)
    assert not worker._thread.is_alive()
    assert playback.closed is True
    with pytest.raises(NativeRelayRuntimeError, match="worker is closed"):
        worker.thumbnail(140, 150, None)


def test_worker_preserves_thumbnail_transport_error_at_inner_deadline():
    worker = NativeRelayPlaybackWorker(
        camera_config(),
        APP_SESSION,
        playback_factory=lambda *args, **kwargs: DelayedThumbnailErrorPlayback(),
        command_timeout=0.01,
    )

    with pytest.raises(RuntimeError, match="thumbnail transport diagnostics"):
        worker.thumbnail(100, 110, None)

    worker.close()
    assert worker.wait_closed(2)


def test_worker_reuses_catalog_session_for_companion_thumbnail():
    ReusablePlayback.instances.clear()
    worker = NativeRelayPlaybackWorker(
        camera_config(),
        APP_SESSION,
        playback_factory=lambda *args, **kwargs: DirectThumbnailPlayback(),
        end_idle_timeout=0.01,
    )
    day = date(2026, 9, 17)

    catalog = worker.catalog([day], None)
    image = worker.thumbnail(100, 110, None)

    assert catalog[0][0] == day
    assert catalog[0][1][0]["start_time"] == 100
    assert image == b"\xff\xd8image\xff\xd9"
    assert len(DirectThumbnailPlayback.instances) == 1
    playback = DirectThumbnailPlayback.instances[0]
    assert playback.connect_count == 1
    assert playback.connect_modes == [True]
    assert playback.queries == [day]
    assert playback.thumbnails == [(100, 110)]
    worker.close()
    worker._thread.join(2)
    assert not worker._thread.is_alive()


def test_runtime_downloads_thumbnail_without_media_playback():
    playback = Playback()
    session = NativeRelayRuntimeSession(
        camera_config(),
        app_session=APP_SESSION,
        playback_factory=lambda *args, **kwargs: playback,
    )

    image = session.download_thumbnail(100, 110)

    assert image == b"\xff\xd8image\xff\xd9"
    assert playback.thumbnails == [(100, 110)]
    assert playback.started is None
    assert playback.connect_modes == [True]
    assert playback.closed is True


def test_low_level_runtime_leaves_thumbnail_capability_policy_to_backend():
    playback = Playback()
    session = NativeRelayRuntimeSession(
        replace(camera_config(), skill=json.dumps({"localStorage": 0})),
        app_session=APP_SESSION,
        playback_factory=lambda *args, **kwargs: playback,
    )

    image = session.download_thumbnail(100, 110)

    assert image == b"\xff\xd8image\xff\xd9"
    assert playback.thumbnails == [(100, 110)]


def test_worker_rejects_cancelled_thumbnail_before_opening_transport():
    cancel = threading.Event()
    cancel.set()
    worker = NativeRelayPlaybackWorker(
        camera_config(),
        APP_SESSION,
        playback_factory=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("cancelled work opened a camera transport")
        ),
    )

    with pytest.raises(NativeRelayRuntimeError, match="cancelled"):
        worker.thumbnail(100, 110, cancel)

    worker.close()
    worker._thread.join(2)
    assert not worker._thread.is_alive()


def test_worker_close_cancels_active_connect_and_drains_queued_work():
    playback = BlockingConnectPlayback()
    worker = NativeRelayPlaybackWorker(
        camera_config(),
        APP_SESSION,
        playback_factory=lambda *args, **kwargs: playback,
    )
    failures = []

    def download(start):
        try:
            worker.thumbnail(start, start + 10, None)
        except Exception as err:  # noqa: BLE001 - thread transports the failure
            failures.append(err)

    active = threading.Thread(target=download, args=(100,))
    queued = threading.Thread(target=download, args=(120,))
    active.start()
    assert playback.connect_started.wait(1)
    queued.start()
    time.sleep(0.05)

    worker.close()
    active.join(2)
    queued.join(2)
    worker._thread.join(2)

    assert not active.is_alive()
    assert not queued.is_alive()
    assert not worker._thread.is_alive()
    assert len(failures) == 2
    assert all(isinstance(error, NativeRelayRuntimeError) for error in failures)
    assert playback.thumbnails == []
    assert playback.closed is True


def test_pool_close_cancels_all_workers_before_waiting_for_threads():
    playbacks = {}

    def worker_for(dev_id):
        playback = BlockingConnectPlayback()
        playbacks[dev_id] = playback
        return NativeRelayPlaybackWorker(
            replace(camera_config(), dev_id=dev_id),
            APP_SESSION,
            playback_factory=lambda *args, **kwargs: playback,
        )

    pool = NativeRelayRuntimePool(APP_SESSION)
    first = worker_for("camera-one")
    second = worker_for("camera-two")
    pool._workers = {"camera-one": first, "camera-two": second}
    failures = []

    def download(worker):
        try:
            worker.thumbnail(100, 110, None)
        except Exception as err:  # noqa: BLE001 - thread transports the failure
            failures.append(err)

    callers = [threading.Thread(target=download, args=(worker,)) for worker in (first, second)]
    for caller in callers:
        caller.start()
    assert all(playback.connect_started.wait(1) for playback in playbacks.values())

    pool.close()
    for caller in callers:
        caller.join(2)

    assert all(not caller.is_alive() for caller in callers)
    assert not first._thread.is_alive()
    assert not second._thread.is_alive()
    assert len(failures) == 2
    assert all(isinstance(error, NativeRelayRuntimeError) for error in failures)
    assert all(playback.closed for playback in playbacks.values())


def test_pool_rotates_worker_when_apk_trace_id_changes():
    pool = NativeRelayRuntimePool(APP_SESSION)
    first_session = pool.session(camera_config(), None)
    first_worker = first_session.playback_worker
    assert first_worker is not None

    refreshed = replace(camera_config(), trace_id="refreshed-trace")
    second_session = pool.session(refreshed, None)
    second_worker = second_session.playback_worker

    assert second_worker is not first_worker
    assert first_worker is not None
    assert not first_worker._thread.is_alive()
    pool.close()


def test_pool_uses_one_sequential_transport_worker_per_camera():
    pool = NativeRelayRuntimePool(APP_SESSION)
    session = pool.session(camera_config(), None)

    assert isinstance(session.playback_worker, NativeRelayPlaybackWorker)
    assert isinstance(session.command_worker, NativeRelayPlaybackWorker)
    assert session.playback_worker is session.command_worker
    pool.close()


def test_session_does_not_disconnect_shared_worker_before_playback():
    calls = []

    class SharedWorker:
        def disconnect(self):
            calls.append(("disconnect",))

        def stream(self, request, event_handler, cancel_event):
            calls.append(("stream", request.start, cancel_event))

    worker = SharedWorker()
    session = NativeRelayRuntimeSession(
        camera_config(),
        app_session=APP_SESSION,
        playback_worker=worker,
        command_worker=worker,
    )

    session.start_playback_stream(
        NativePlaybackRequest(
            start=100,
            end=110,
            media_format=NativeMediaFormat.RAW_PACKETS,
        ),
        lambda event: None,
    )

    assert calls == [("stream", 100, None)]


def test_session_disconnects_metadata_transport_before_playback():
    calls = []

    class StreamWorker:
        def stream(self, request, event_handler, cancel_event):
            calls.append(("stream", request.start, cancel_event))

    class CommandWorker:
        def disconnect(self):
            calls.append(("disconnect-command",))

    session = NativeRelayRuntimeSession(
        camera_config(),
        app_session=APP_SESSION,
        playback_worker=StreamWorker(),
        command_worker=CommandWorker(),
    )
    session.start_playback_stream(
        NativePlaybackRequest(
            start=100,
            end=110,
            media_format=NativeMediaFormat.RAW_PACKETS,
        ),
        lambda event: None,
    )

    assert calls == [("disconnect-command",), ("stream", 100, None)]


def test_session_disconnects_playback_transport_before_thumbnail():
    calls = []

    class StreamWorker:
        def disconnect(self):
            calls.append(("disconnect-stream",))

    class CommandWorker:
        def thumbnail(self, start, end, cancel_event):
            calls.append(("thumbnail", start, end, cancel_event))
            return b"\xff\xd8image\xff\xd9"

    session = NativeRelayRuntimeSession(
        camera_config(),
        app_session=APP_SESSION,
        playback_worker=StreamWorker(),
        command_worker=CommandWorker(),
    )

    assert session.download_thumbnail(100, 110) == b"\xff\xd8image\xff\xd9"
    assert calls == [("disconnect-stream",), ("thumbnail", 100, 110, None)]


def test_pool_rotates_worker_when_camera_credentials_change():
    pool = NativeRelayRuntimePool(APP_SESSION)
    first_worker = pool.session(camera_config(), None).playback_worker
    assert first_worker is not None

    refreshed = replace(camera_config(), token=json.dumps({**TOKEN, "expired": 2}))
    second_worker = pool.session(refreshed, None).playback_worker

    assert second_worker is not None
    assert second_worker is not first_worker
    assert not first_worker._thread.is_alive()
    pool.close()


def test_pool_reuses_worker_while_camera_connection_config_is_current():
    pool = NativeRelayRuntimePool(APP_SESSION)
    config = camera_config()

    first_worker = pool.session(config, None).playback_worker
    second_worker = pool.session(config, None).playback_worker

    assert first_worker is second_worker
    pool.close()


def test_worker_discards_transport_after_active_clip_is_cancelled():
    ReusablePlayback.instances.clear()
    worker = NativeRelayStreamWorker(
        camera_config(),
        APP_SESSION,
        playback_factory=lambda *args, **kwargs: ReusablePlayback(),
        end_idle_timeout=0.2,
    )
    cancel = threading.Event()
    received_media = threading.Event()
    failure = []
    media_count = 0

    def first_event(event):
        nonlocal media_count
        if event["payload"]["type"] == "media-codec":
            media_count += 1
            if media_count == 2:
                received_media.set()

    def first_stream():
        try:
            worker.stream(
                NativePlaybackRequest(
                    start=100,
                    end=110,
                    media_format=NativeMediaFormat.RAW_PACKETS,
                ),
                first_event,
                cancel,
            )
        except BaseException as err:  # noqa: BLE001 - assert worker handoff
            failure.append(err)

    thread = threading.Thread(target=first_stream)
    thread.start()
    assert received_media.wait(2)
    cancel.set()
    thread.join(2)
    assert not thread.is_alive()
    assert failure and "cancelled" in str(failure[0])

    worker.stream(
        NativePlaybackRequest(
            start=120,
            end=130,
            media_format=NativeMediaFormat.RAW_PACKETS,
        ),
        lambda event: None,
        None,
    )

    assert len(ReusablePlayback.instances) == 2
    first_playback, second_playback = ReusablePlayback.instances
    assert first_playback.connect_count == 1
    assert first_playback.start_count == 1
    assert first_playback.stop_count == 1
    assert first_playback.closed is True
    assert second_playback.connect_count == 1
    assert second_playback.start_count == 1
    assert second_playback.stop_count == 1
    worker.close()
    worker._thread.join(2)
    assert not worker._thread.is_alive()


def test_worker_discards_failed_connection_before_next_request():
    ReusablePlayback.instances.clear()
    created = []

    def factory(*args, **kwargs):
        playback = (
            FailedConnectPlayback() if not created else ReusablePlayback()
        )
        created.append(playback)
        return playback

    worker = NativeRelayStreamWorker(
        camera_config(),
        APP_SESSION,
        playback_factory=factory,
        end_idle_timeout=0.01,
    )
    request = NativePlaybackRequest(
        start=100,
        end=110,
        media_format=NativeMediaFormat.RAW_PACKETS,
    )

    with pytest.raises(RuntimeError, match="no answer"):
        worker.stream(request, lambda event: None, None)
    worker.stream(request, lambda event: None, None)

    assert len(created) == 2
    assert created[0].closed is True
    assert created[1].connect_count == 1
    assert created[1].start_count == 1
    worker.close()
    worker._thread.join(2)
    assert not worker._thread.is_alive()


def test_runs_one_bounded_catalog_transaction():
    playback = Playback()
    session = NativeRelayRuntimeSession(
        camera_config(),
        app_session=APP_SESSION,
        playback_factory=lambda *args, **kwargs: playback,
        clock=lambda: 1000.0,
    )

    recordings = session.recordings_for_day(date(2026, 9, 17))

    assert recordings == [
        {
            "start_time": 100,
            "end_time": 110,
            "event_type": 2,
            "video_type": 0,
            "encrypt": 0,
            "uuid": "",
            "encrypt_md5": "",
        }
    ]
    assert playback.query == date(2026, 9, 17)
    assert playback.event_queries == []
    assert playback.closed is True


def test_runtime_leaves_stream_id_allocation_to_thing_camera_simple_session():
    captured = {}
    playback = Playback()

    def playback_factory(*args, **kwargs):
        captured.update(kwargs)
        return playback

    session = NativeRelayRuntimeSession(
        camera_config(),
        app_session=APP_SESSION,
        playback_factory=playback_factory,
    )

    session.recordings_for_day(date(2026, 9, 17))

    assert captured == {}


def test_catalog_timeout_reports_relay_diagnostics():
    playback = HangingCatalogPlayback()
    session = NativeRelayRuntimeSession(
        camera_config(),
        app_session=APP_SESSION,
        signaling_timeout=0.01,
        playback_factory=lambda *args, **kwargs: playback,
    )

    with pytest.raises(
        NativeRelayRuntimeError,
        match=r"frames=0xf4:2,0xf5:3, segments=0, decode_failures=0",
    ):
        session.recordings_for_day(date(2026, 9, 17))
    assert playback.closed is True


def test_playback_timeout_reports_media_state_and_transport_diagnostics():
    playback = HangingMediaPlayback()
    session = NativeRelayRuntimeSession(
        camera_config(),
        app_session=APP_SESSION,
        first_frame_timeout=0.01,
        playback_factory=lambda *args, **kwargs: playback,
    )

    with pytest.raises(
        NativeRelayRuntimeError,
        match=(
            r"video=False, audio=False; transport=direct-ice, "
            r"relay=disabled, session=\(routed=0:0x81=4\)"
        ),
    ):
        session.start_playback_stream(
            NativePlaybackRequest(
                start=100,
                end=110,
                media_format=NativeMediaFormat.RAW_PACKETS,
            ),
            lambda event: None,
        )
    assert playback.closed is True


def test_browser_handoff_cancels_camera_handshake_promptly():
    playback = BlockingConnectPlayback()
    cancel = threading.Event()
    failures = []
    session = NativeRelayRuntimeSession(
        camera_config(),
        app_session=APP_SESSION,
        cancel_event=cancel,
        playback_factory=lambda *args, **kwargs: playback,
    )

    def run():
        try:
            session.start_playback_stream(
                NativePlaybackRequest(
                    start=100,
                    end=110,
                    media_format=NativeMediaFormat.RAW_PACKETS,
                ),
                lambda event: None,
            )
        except Exception as err:  # noqa: BLE001 - thread transports the failure
            failures.append(err)

    worker = threading.Thread(target=run)
    worker.start()
    assert playback.connect_started.wait(1)
    cancel.set()
    worker.join(2)

    assert not worker.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], NativeRelayRuntimeError)
    assert "cancelled" in str(failures[0])
    assert playback.closed is True


def test_catalog_batch_reuses_one_native_connection_newest_first():
    playbacks = []

    def playback_factory(*args, **kwargs):
        playback = Playback()
        playbacks.append(playback)
        return playback

    session = NativeRelayRuntimeSession(
        camera_config(),
        app_session=APP_SESSION,
        playback_factory=playback_factory,
        clock=lambda: 1000.0,
    )
    days = [date(2026, 9, 17), date(2026, 9, 16)]

    recordings = session.recordings_for_days(days)

    assert len(playbacks) == 1
    assert [day for day, _ in recordings] == days
    assert playbacks[0].queries == days
    assert playbacks[0].closed is True


def test_reuses_audio_codec_info_for_following_native_packets():
    playback = ReusesAudioCodecPlayback()
    events = []
    session = NativeRelayRuntimeSession(
        camera_config(),
        app_session=APP_SESSION,
        end_idle_timeout=0.01,
        playback_factory=lambda *args, **kwargs: playback,
        clock=lambda: 1000.0,
    )

    session.start_playback_stream(
        NativePlaybackRequest(
            start=100,
            end=106,
            media_format=NativeMediaFormat.RAW_PACKETS,
        ),
        events.append,
    )

    media = [event for event in events if event["payload"]["type"] == "media-codec"]
    assert len(media) == 3
    assert media[-1]["payload"]["info"]["codecId"] == 4


def test_encrypted_playback_fetches_one_secret_and_emits_clear_audio_video():
    calls = []

    def call_api(api, version, body, extra_params):
        calls.append((api, version, body, extra_params))
        return [{"uuid": "clip-uuid", "secretKey": EncryptedPlayback.secret}]

    playback = EncryptedPlayback()
    events = []
    session = NativeRelayRuntimeSession(
        camera_config(),
        app_session=APP_SESSION,
        end_idle_timeout=0.01,
        playback_factory=lambda *args, **kwargs: playback,
        media_secrets=NativeMediaSecretStore(call_api),
    )

    session.start_playback_stream(
        NativePlaybackRequest(
            start=100,
            end=106,
            encrypted=True,
            encryption_uuid="clip-uuid",
            media_format=NativeMediaFormat.RAW_PACKETS,
        ),
        events.append,
    )

    media = [event for event in events if event["payload"]["type"] == "media-codec"]
    assert len(media) == 2
    assert calls[0][2] == {"uuids": '["clip-uuid"]'}
    assert playback.closed is True


def test_catalog_prefetches_encrypted_recording_secrets_as_one_batch():
    calls = []

    def call_api(api, version, body, extra_params):
        calls.append((api, version, body, extra_params))
        return [{"uuid": "clip-uuid", "secretKey": EncryptedPlayback.secret}]

    store = NativeMediaSecretStore(call_api)
    session = NativeRelayRuntimeSession(
        camera_config(),
        app_session=APP_SESSION,
        playback_factory=lambda *args, **kwargs: EncryptedCatalogPlayback(),
        media_secrets=store,
    )

    recordings = session.recordings_for_day(date(2026, 9, 17))

    assert recordings[0]["uuid"] == "clip-uuid"
    assert calls[0][2] == {"uuids": '["clip-uuid"]'}
    assert store.cached("clip-uuid") == EncryptedPlayback.secret


def test_rejects_encrypted_clip_without_secret_source_before_opening_transport():
    opened = []
    session = NativeRelayRuntimeSession(
        camera_config(),
        app_session=APP_SESSION,
        playback_factory=lambda *args, **kwargs: opened.append(True),
    )

    with pytest.raises(NativeRelayRuntimeError, match="no recording secret source"):
        session.start_playback_stream(
            NativePlaybackRequest(
                start=100,
                end=110,
                encrypted=True,
                media_format=NativeMediaFormat.RAW_PACKETS,
            ),
            lambda event: None,
        )
    assert opened == []


def test_interactive_timeline_seeks_pauses_and_resumes_one_camera_object():
    commands = queue.Queue()
    events = []

    class InteractivePlayback(Playback):
        def __init__(self):
            super().__init__()
            self.seek_calls = []
            self.pause_count = 0
            self.resume_count = 0
            self.receive_count = 0

        async def seek(self, **kwargs):
            self.seek_calls.append(kwargs)
            self.frames.put_nowait(
                wire_frame(b"\x00\x00\x00\x01\x65seek", frame_type=1)
            )
            self.frames.put_nowait(wire_frame(b"aac-seek", frame_type=3, codec=4))

        async def pause(self):
            self.pause_count += 1

        async def resume(self):
            self.resume_count += 1

        async def receive_frame(self):
            frame = await super().receive_frame()
            self.receive_count += 1
            if self.receive_count == 2:
                commands.put(
                    (
                        "seek",
                        NativePlaybackRequest(
                            start=200,
                            end=210,
                            play_time=205,
                            fragments_json='{"fragments":[{"start":200,"end":210}]}',
                            media_format=NativeMediaFormat.RAW_PACKETS,
                        ),
                    )
                )
            elif self.receive_count == 4:
                commands.put(("pause", None))
                commands.put(("resume", None))
                commands.put(("stop", None))
            return frame

    playback = InteractivePlayback()
    session = NativeRelayRuntimeSession(
        replace(
            camera_config(),
            skill=json.dumps({"localStorage": 33554432 | 128}),
        ),
        app_session=APP_SESSION,
        end_idle_timeout=0.01,
    )
    initial = NativePlaybackRequest(
        start=100,
        end=110,
        fragments_json='{"fragments":[{"start":100,"end":110}]}',
        media_format=NativeMediaFormat.RAW_PACKETS,
    )

    asyncio.run(
        session._interactive_connected(
            playback,
            initial,
            events.append,
            commands,
        )
    )

    assert playback.start_calls == [
        {
            "start": 100,
            "end": 110,
            "play_time": 100,
            "encrypted": False,
            "fragments_json": '{"fragments":[{"start":100,"end":110}]}',
            "play_mode_supported": True,
        }
    ]
    assert playback.seek_calls == [
        {
            "start": 200,
            "end": 210,
            "play_time": 205,
            "encrypted": False,
            "fragments_json": '{"fragments":[{"start":200,"end":210}]}',
            "play_mode_supported": True,
        }
    ]
    assert playback.pause_count == 1
    assert playback.resume_count == 1
    assert playback.stop_count == 1
    assert [
        event["payload"]["info"].get("generation")
        for event in events
        if event["payload"]["type"] == "started"
    ] == [1, 2]


def test_interactive_timeline_collapses_rapid_seeks_to_the_latest_position():
    """Rapid timeline clicks must not make the camera replay every stop."""
    commands = queue.Queue()
    events = []

    class InteractivePlayback(Playback):
        def __init__(self):
            super().__init__()
            self.seek_calls = []

        async def seek(self, **kwargs):
            self.seek_calls.append(kwargs)

    playback = InteractivePlayback()
    session = NativeRelayRuntimeSession(
        replace(camera_config(), skill=json.dumps({"localStorage": 33554432 | 128})),
        app_session=APP_SESSION,
        end_idle_timeout=0.01,
    )
    initial = NativePlaybackRequest(
        start=100,
        end=110,
        fragments_json='{"fragments":[{"start":100,"end":110}]}',
        media_format=NativeMediaFormat.RAW_PACKETS,
    )
    for position in (201, 305, 407):
        commands.put((
            "seek",
            NativePlaybackRequest(
                start=400,
                end=410,
                play_time=position,
                fragments_json='{"fragments":[{"start":400,"end":410}]}',
                media_format=NativeMediaFormat.RAW_PACKETS,
            ),
        ))
    commands.put(("stop", None))

    asyncio.run(session._interactive_connected(playback, initial, events.append, commands))

    assert [call["play_time"] for call in playback.seek_calls] == [407]
    assert playback.stop_count == 1


def test_playback_is_not_announced_when_camera_rejects_start():
    events = []

    class RejectedPlayback(Playback):
        async def start(self, **kwargs):
            raise NativeRelayRuntimeError("camera rejected playback")

    session = NativeRelayRuntimeSession(
        camera_config(),
        app_session=APP_SESSION,
        end_idle_timeout=0.01,
    )

    with pytest.raises(NativeRelayRuntimeError, match="camera rejected playback"):
        asyncio.run(
            session._stream_connected(
                RejectedPlayback(),
                NativePlaybackRequest(
                    start=100,
                    end=110,
                    media_format=NativeMediaFormat.RAW_PACKETS,
                ),
                events.append,
            )
        )

    assert events == []


def test_interactive_worker_closes_transport_when_viewer_leaves():
    ReusablePlayback.instances.clear()
    worker = NativeRelayPlaybackWorker(
        camera_config(),
        APP_SESSION,
        playback_factory=lambda *args, **kwargs: ReusablePlayback(),
        end_idle_timeout=0.01,
    )
    commands = queue.Queue()
    commands.put(("stop", None))
    request = NativePlaybackRequest(
        start=100,
        end=110,
        media_format=NativeMediaFormat.RAW_PACKETS,
    )

    worker.interactive_stream(request, lambda event: None, commands, None)
    worker.close()
    worker.wait_closed()

    assert len(ReusablePlayback.instances) == 1
    assert ReusablePlayback.instances[0].stop_count == 1
    assert ReusablePlayback.instances[0].closed is True


def test_interactive_worker_discards_transport_when_stop_never_answers(monkeypatch):
    from custom_components.tuya_recordings.lib import native_relay_runtime

    class HangingStopPlayback(ReusablePlayback):
        async def stop(self):
            await asyncio.Event().wait()

        async def abort(self):
            self._playback_started = False
            self.closed = True

    playbacks = [HangingStopPlayback(), ReusablePlayback()]
    monkeypatch.setattr(
        native_relay_runtime,
        "_PLAYBACK_STOP_TIMEOUT_SECONDS",
        0.01,
    )
    worker = NativeRelayPlaybackWorker(
        camera_config(),
        APP_SESSION,
        playback_factory=lambda *args, **kwargs: playbacks.pop(0),
        end_idle_timeout=0.01,
    )
    commands = queue.Queue()
    commands.put(("stop", None))
    request = NativePlaybackRequest(
        start=100,
        end=110,
        media_format=NativeMediaFormat.RAW_PACKETS,
    )

    worker.interactive_stream(request, lambda event: None, commands, None)
    result = worker.catalog([date(2026, 9, 17)], None)
    worker.close()
    worker.wait_closed()

    assert result[0][1][0]["start_time"] == 100
    assert playbacks == []


def test_interactive_timeline_releases_audio_only_session():
    class AudioOnlyPlayback(Playback):
        async def start(self, **kwargs):
            self.started = kwargs
            self.start_calls.append(kwargs)
            self._playback_started = True
            self.frames.put_nowait(wire_frame(b"aac", frame_type=3, codec=4))

    playback = AudioOnlyPlayback()
    session = NativeRelayRuntimeSession(
        camera_config(),
        app_session=APP_SESSION,
        first_frame_timeout=0.01,
    )

    with pytest.raises(NativeRelayRuntimeError, match="audio but no video"):
        asyncio.run(
            session._interactive_connected(
                playback,
                NativePlaybackRequest(
                    start=100,
                    end=110,
                    media_format=NativeMediaFormat.RAW_PACKETS,
                ),
                lambda event: None,
                queue.Queue(),
            )
        )

    assert playback.stop_count == 1
