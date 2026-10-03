from datetime import date
import io
from pathlib import Path

from PIL import Image
import pytest

from custom_components.tuya_recordings.lib.native_backend import (
    ApkNativeRecordingBackend,
    NativeCameraSessionProvider,
    NativeBackendNotConfigured,
    RecordingBackendError,
    RecordingBackendAuthError,
    RecordingBackendUnavailable,
    ensure_backend_available,
    newest_first_days,
    _map_native_playback_error,
)
from custom_components.tuya_recordings.lib.commands import CameraWorkCancelled
from custom_components.tuya_recordings.lib.native_frames import (
    NativePlaybackFrame,
    NativePlaybackFrameType,
    playback_frame_event,
)
from custom_components.tuya_recordings.lib.native_frames import NATIVE_PLAYBACK_EVENT_VERSION
from custom_components.tuya_recordings.lib.native_session import NativeMediaFormat
from custom_components.tuya_recordings.lib.native_session import NativeCameraSessionConfig
from custom_components.tuya_recordings.lib.native_provider import NativeCameraConfigAuthError
from custom_components.tuya_recordings.lib.native_playback import NativePlaybackError


MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42"


def test_native_playback_cancellation_maps_to_expected_camera_cancellation():
    mapped = _map_native_playback_error(
        NativePlaybackError("APK-native playback stream failed: Native playback was cancelled")
    )

    assert isinstance(mapped, CameraWorkCancelled)


def session_config(dev_id: str = "camera") -> NativeCameraSessionConfig:
    return NativeCameraSessionConfig(
        dev_id=dev_id,
        local_key="local-key",
        camera_password="camera-password",
        token='{"session":{"sessionId":"sid"}}',
        skill='{"videos":[],"localStorage":167772160}',
        p2p_type=4,
        trace_id="trace",
    )


class FakeMuxer:
    def __init__(self, output_path):
        self.output_path = Path(output_path)
        self.events = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None and self.events:
            self.output_path.write_bytes(MP4_BYTES)

    def handle_event(self, event):
        self.events.append(event)


class FakeStreamMuxer:
    def __init__(self, chunk_callback):
        self.chunk_callback = chunk_callback
        self.events = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None and self.events:
            self.chunk_callback(b"fmp4")

    def handle_event(self, event):
        self.events.append(event)


class FakeThumbnailExtractor:
    def __init__(self, output_path):
        self.output_path = Path(output_path)
        self.ready = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def handle_event(self, event):
        if event["payload"]["type"] == NativePlaybackFrameType.MEDIA_CODEC.value:
            self.output_path.write_bytes(jpeg_bytes())
            self.ready = True
            raise RuntimeError("stop stream")

    def require_thumbnail(self):
        assert self.ready


def native_backend(
    session=None,
    *,
    muxer_factory=FakeMuxer,
    stream_muxer_factory=FakeStreamMuxer,
    thumbnail_extractor_factory=FakeThumbnailExtractor,
) -> ApkNativeRecordingBackend:
    return ApkNativeRecordingBackend(
        NativeCameraSessionProvider(
            session_config=session_config,
            open_session=lambda config, cancel_event: session,
        ),
        muxer_factory=muxer_factory,
        stream_muxer_factory=stream_muxer_factory,
        thumbnail_extractor_factory=thumbnail_extractor_factory,
    )


def jpeg_bytes():
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "orange").save(output, format="JPEG")
    return output.getvalue()


class FakeNativeSession:
    def __init__(self):
        self.closed = False
        self.catalog_days = []
        self.playbacks = []
        self.thumbnail_requests = []
        self.event_catalog_days = []
        self.catalog = [{"st": 10, "ed": 20, "type": "motion"}]

    def recordings_for_day(self, day):
        self.catalog_days.append(day)
        return list(self.catalog)

    def event_recordings_for_day(self, day):
        self.event_catalog_days.append(day)
        return list(self.catalog)

    def start_playback_stream(self, request, event_handler=None):
        self.playbacks.append(request)
        if event_handler is not None:
            event_handler(
                playback_frame_event(
                    NativePlaybackFrame(NativePlaybackFrameType.STARTED),
                    protocol=NATIVE_PLAYBACK_EVENT_VERSION,
                )
            )
            event_handler(
                playback_frame_event(
                    NativePlaybackFrame(
                        NativePlaybackFrameType.MEDIA_CODEC,
                        b"h264",
                        {
                            "avChannel": 0,
                            "frameNo": 1,
                            "codecId": 27,
                            "codecName": "h264",
                            "isKeyFrame": True,
                            "timestamp": 1,
                            "frameInfoBase64": "",
                        },
                    ),
                    protocol=NATIVE_PLAYBACK_EVENT_VERSION,
                )
            )
            event_handler(
                playback_frame_event(
                    NativePlaybackFrame(
                        NativePlaybackFrameType.MEDIA_CODEC,
                        b"aac",
                        {
                            "avChannel": 0,
                            "frameNo": 2,
                            "codecId": 86018,
                            "codecName": "aac",
                            "isKeyFrame": False,
                            "timestamp": 1,
                            "frameInfoBase64": "",
                            "sampleRate": 8000,
                            "channels": 1,
                        },
                    ),
                    protocol=NATIVE_PLAYBACK_EVENT_VERSION,
                )
            )
            event_handler(
                playback_frame_event(
                    NativePlaybackFrame(NativePlaybackFrameType.FINISHED),
                    protocol=NATIVE_PLAYBACK_EVENT_VERSION,
                )
            )

    def receive_clip(self, request, output_path):
        self.playbacks.append(request)
        Path(output_path).write_bytes(MP4_BYTES)

    def thumbnail_jpeg(self, request, *, seconds=2):
        self.thumbnail_requests.append((request, seconds))
        return jpeg_bytes()

    def download_thumbnail(self, start, end):
        self.thumbnail_requests.append((start, end))
        return jpeg_bytes()

    def close(self):
        self.closed = True


def test_newest_first_days_deduplicates_and_sorts():
    assert newest_first_days([
        date(2026, 9, 14),
        date(2026, 9, 16),
        date(2026, 9, 14),
    ]) == [date(2026, 9, 16), date(2026, 9, 14)]


def test_newest_first_days_rejects_non_dates():
    with pytest.raises(ValueError, match="date objects"):
        newest_first_days([date(2026, 9, 16), "2026-09-15"])


def test_native_backend_refuses_to_fake_catalog_until_helper_exists():
    with pytest.raises(RecordingBackendUnavailable, match="native camera session"):
        native_backend().recordings_for_day("camera", {}, {}, date(2026, 9, 16))


def test_native_backend_validates_batch_order_before_helper_use():
    with pytest.raises(ValueError, match="newest first"):
        native_backend().recordings_for_days(
            "camera",
            {},
            {},
            [date(2026, 9, 15), date(2026, 9, 16)],
        )


def test_native_backend_refuses_to_fake_playback_until_helper_exists(tmp_path):
    with pytest.raises(RecordingBackendUnavailable, match="native camera session"):
        native_backend().receive_clip("camera", {}, {}, 10, 20, tmp_path / "clip.mp4")


def test_native_backend_validates_playback_before_helper_use(tmp_path):
    with pytest.raises(ValueError, match="Playback end"):
        native_backend().receive_clip("camera", {}, {}, 20, 10, tmp_path / "clip.mp4")


def test_native_backend_preserves_app_session_auth_failure():
    def expired(_dev_id):
        raise NativeCameraConfigAuthError("Smart Life app-session authorization expired")

    backend = ApkNativeRecordingBackend(
        NativeCameraSessionProvider(
            session_config=expired,
            open_session=lambda config, cancel_event: None,
        )
    )

    with pytest.raises(RecordingBackendAuthError, match="authorization expired"):
        backend.recordings_for_day("camera", {}, {}, date(2026, 9, 16))


def test_native_backend_refuses_to_fake_thumbnail_until_helper_exists(tmp_path):
    with pytest.raises(RecordingBackendUnavailable, match="native camera session"):
        native_backend().receive_thumbnail("camera", {}, {}, 10, 20, tmp_path / "thumb.jpg")


def test_native_backend_refuses_missing_helper_operation(tmp_path):
    class EmptySession:
        def close(self):
            pass

    with pytest.raises(RecordingBackendUnavailable, match="startPlayBack"):
        native_backend(EmptySession()).receive_clip("camera", {}, {}, 10, 20, tmp_path / "clip.mp4")


def test_native_backend_refuses_missing_playback_after_fresh_catalog(tmp_path):
    class CatalogOnlySession:
        def recordings_for_day(self, day):
            return [{"st": 10, "ed": 20}]

        def close(self):
            pass

    with pytest.raises(RecordingBackendUnavailable, match="startPlayBack"):
        native_backend(CatalogOnlySession()).receive_clip("camera", {}, {}, 10, 20, tmp_path / "clip.mp4")


def test_native_backend_queries_catalog_through_one_helper_session():
    session = FakeNativeSession()
    result = native_backend(session).recordings_for_days(
        "camera",
        {},
        {},
        [date(2026, 9, 16), date(2026, 9, 15)],
    )
    assert session.catalog_days == [date(2026, 9, 16), date(2026, 9, 15)]
    assert [clip["start"] for _, clips in result for clip in clips] == [10, 10]
    assert [clip["end"] for _, clips in result for clip in clips] == [20, 20]
    assert all(clips[0]["raw"]["type"] == "motion" for _, clips in result)
    assert session.closed is True


def test_native_backend_normalizes_registered_v4_catalog_fields():
    session = FakeNativeSession()
    session.catalog = [
        {
            "start_time": 1782488168,
            "end_time": 1782488206,
            "event_type": 2,
            "video_type": 1,
            "encrypt": 0,
            "uuid": "native-clip",
            "encrypt_md5": "digest",
        }
    ]

    result = native_backend(session).recordings_for_day(
        "camera", {}, {}, date(2026, 6, 26)
    )

    assert result[0]["start"] == 1782488168
    assert result[0]["end"] == 1782488206
    assert result[0]["date"] == "2026-06-26"
    assert result[0]["raw"]["event_type"] == 2


def test_native_backend_uses_requested_day_across_host_timezone_boundary():
    session = FakeNativeSession()
    session.catalog = [
        {
            "start_time": 1789963078,
            "end_time": 1789963115,
            "event_type": 2,
        }
    ]

    result = native_backend(session).recordings_for_day(
        "camera", {}, {}, date(2026, 9, 21)
    )

    assert result[0]["date"] == "2026-09-21"


def test_native_backend_receives_clip_through_apk_stream(tmp_path):
    session = FakeNativeSession()
    output = tmp_path / "clip.mp4"
    native_backend(session).receive_clip("camera", {}, {}, 10, 20, output, play_time=12)
    assert session.catalog_days == []
    assert session.playbacks[0].args == (10, 20, 12)
    assert session.playbacks[0].media_format == NativeMediaFormat.RAW_PACKETS
    assert output.read_bytes() == MP4_BYTES
    assert session.closed is True


def test_native_backend_streams_clip_through_apk_playback_callbacks():
    session = FakeNativeSession()
    chunks = []

    native_backend(session).stream_clip("camera", {}, {}, 10, 20, chunks.append, play_time=12)

    assert session.catalog_days == []
    assert session.playbacks[0].args == (10, 20, 12)
    assert session.playbacks[0].media_format == NativeMediaFormat.RAW_PACKETS
    assert chunks == [b"fmp4"]
    assert session.closed is True


def test_native_backend_does_not_open_a_second_catalog_session_before_playback(tmp_path):
    session = FakeNativeSession()
    session.catalog = [{"st": 30, "ed": 40, "type": "motion"}]

    native_backend(session).receive_clip("camera", {}, {}, 10, 20, tmp_path / "clip.mp4")

    assert session.catalog_days == []
    assert len(session.playbacks) == 1
    assert session.closed is True


def test_native_backend_advertises_apk_native_clip_playback():
    assert native_backend(FakeNativeSession()).clip_playback_available is True
    assert NativeBackendNotConfigured().clip_playback_available is False


def test_native_backend_closes_its_runtime_provider():
    closed = []
    backend = ApkNativeRecordingBackend(
        NativeCameraSessionProvider(
            session_config=session_config,
            open_session=lambda config, cancel_event: FakeNativeSession(),
            close=lambda: closed.append(True),
        )
    )

    backend.close()

    assert closed == [True]


def test_native_backend_rejects_playback_when_helper_writes_no_media(tmp_path):
    class EmptyPlaybackSession(FakeNativeSession):
        def start_playback_stream(self, request, event_handler=None):
            del event_handler
            self.playbacks.append(request)

    with pytest.raises(RecordingBackendError, match="no raw video"):
        native_backend(EmptyPlaybackSession()).receive_clip("camera", {}, {}, 10, 20, tmp_path / "clip.mp4")


def test_native_backend_writes_native_thumbnail_jpeg(tmp_path):
    session = FakeNativeSession()
    output = tmp_path / "thumb.jpg"
    native_backend(session).receive_thumbnail("camera", {}, {}, 10, 20, output)
    assert output.read_bytes().startswith(b"\xff\xd8")
    assert session.thumbnail_requests == [(10, 20)]
    assert session.playbacks == []
    assert not output.with_suffix(".jpg.tmp").exists()
    assert session.closed is True


def test_native_backend_extracts_thumbnail_from_playback_stream(tmp_path):
    session = FakeNativeSession()
    output = tmp_path / "thumb.jpg"

    native_backend(session).receive_thumbnail_from_stream(
        "camera", {}, {}, 10, 20, output
    )

    assert output.read_bytes() == jpeg_bytes()
    assert len(session.playbacks) == 1
    assert session.thumbnail_requests == []
    assert session.closed is True


def test_native_backend_rejects_thumbnail_without_event_timeline_capability(tmp_path):
    opened = []

    def legacy_config(dev_id):
        config = session_config(dev_id)
        return NativeCameraSessionConfig(
            dev_id=config.dev_id,
            local_key=config.local_key,
            camera_password=config.camera_password,
            token=config.token,
            skill='{"videos":[],"localStorage":3975}',
            p2p_type=config.p2p_type,
            trace_id=config.trace_id,
        )

    backend = ApkNativeRecordingBackend(
        NativeCameraSessionProvider(
            session_config=legacy_config,
            open_session=lambda config, cancel_event: opened.append(config)
            or FakeNativeSession(),
        )
    )
    output = tmp_path / "thumb.jpg"

    with pytest.raises(
        RecordingBackendUnavailable,
        match="does not advertise direct recording JPEG transfer",
    ):
        backend.receive_thumbnail("camera", {}, {}, 10, 20, output)

    assert opened == []
    assert not output.exists()


def test_native_backend_accepts_thumbnail_with_event_timeline_capability(tmp_path):
    config = session_config()
    capable_config = NativeCameraSessionConfig(
        dev_id=config.dev_id,
        local_key=config.local_key,
        camera_password=config.camera_password,
        token=config.token,
        skill='{"videos":[],"localStorage":33554432}',
        p2p_type=config.p2p_type,
        trace_id=config.trace_id,
    )
    backend = ApkNativeRecordingBackend(
        NativeCameraSessionProvider(
            session_config=lambda dev_id: capable_config,
            open_session=lambda config, cancel_event: FakeNativeSession(),
        )
    )
    output = tmp_path / "thumb.jpg"

    backend.receive_thumbnail("camera", {}, {}, 10, 20, output)

    assert output.read_bytes() == jpeg_bytes()


def test_native_backend_uses_exact_event_catalog_bounds_for_thumbnail_rows():
    class EventCatalogSession(FakeNativeSession):
        def __init__(self):
            super().__init__()
            self.catalog = [{"start_time": 100, "end_time": 200}]

        def event_recordings_for_day(self, day):
            self.event_catalog_days.append(day)
            return [{"start_time": 125, "end_time": 140, "event_types": [1]}]

    session = EventCatalogSession()
    clips = native_backend(session).recordings_for_day(
        "camera", {}, {}, date(2026, 9, 16)
    )

    assert [(clip["start"], clip["end"]) for clip in clips] == [(125, 140)]
    assert clips[0]["raw"]["event_types"] == [1]
    assert session.catalog_days == [date(2026, 9, 16)]
    assert session.event_catalog_days == [date(2026, 9, 16)]


def test_native_backend_keeps_ordinary_catalog_when_event_catalog_is_empty():
    class EmptyEventCatalogSession(FakeNativeSession):
        def __init__(self):
            super().__init__()
            self.catalog = [{"start_time": 100, "end_time": 200}]

        def event_recordings_for_day(self, day):
            self.event_catalog_days.append(day)
            return []

    session = EmptyEventCatalogSession()
    clips = native_backend(session).recordings_for_day(
        "camera", {}, {}, date(2026, 9, 16)
    )

    assert [(clip["start"], clip["end"]) for clip in clips] == [(100, 200)]


def test_native_backend_does_not_query_events_without_event_capability():
    class OrdinaryOnlySession(FakeNativeSession):
        def event_recordings_for_day(self, day):
            raise AssertionError(f"Unexpected event query for {day}")

    config = session_config()
    ordinary_config = NativeCameraSessionConfig(
        dev_id=config.dev_id,
        local_key=config.local_key,
        camera_password=config.camera_password,
        token=config.token,
        skill='{"videos":[],"localStorage":3975}',
        p2p_type=config.p2p_type,
        trace_id=config.trace_id,
    )
    session = OrdinaryOnlySession()
    backend = ApkNativeRecordingBackend(
        NativeCameraSessionProvider(
            session_config=lambda dev_id: ordinary_config,
            open_session=lambda config, cancel_event: session,
        )
    )

    clips = backend.recordings_for_day("camera", {}, {}, date(2026, 9, 16))

    assert [(clip["start"], clip["end"]) for clip in clips] == [(10, 20)]


def test_native_backend_rejects_corrupt_marker_only_thumbnail(tmp_path):
    class CorruptThumbnailSession(FakeNativeSession):
        def download_thumbnail(self, start, end):
            return b"\xff\xd8not-an-image\xff\xd9"

    output = tmp_path / "thumb.jpg"
    with pytest.raises(RecordingBackendError, match="could not be decoded"):
        native_backend(CorruptThumbnailSession()).receive_thumbnail(
            "camera", {}, {}, 10, 20, output
        )

    assert not output.exists()
    assert not output.with_suffix(".jpg.tmp").exists()


def test_not_configured_backend_fails_closed():
    backend = NativeBackendNotConfigured()
    assert backend.name == "apk-native-not-configured"
    assert backend.apk_native is True
    ensure_backend_available(backend)
    with pytest.raises(RecordingBackendUnavailable, match="APK-native camera session is not configured"):
        backend.recordings_for_day("camera", {}, {}, date(2026, 9, 16))
