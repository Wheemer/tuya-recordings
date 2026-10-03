from pathlib import Path

import pytest

from custom_components.tuya_recordings.lib.native_frames import (
    NativePlaybackFrame,
    NativePlaybackFrameType,
    playback_frame_event,
)
from custom_components.tuya_recordings.lib.native_frames import NATIVE_PLAYBACK_EVENT_VERSION
from custom_components.tuya_recordings.lib.native_playback import (
    NativePlaybackEventAudit,
    NativePlaybackError,
    NativePlaybackSessionRunner,
    looks_like_mp4,
    native_playback_request,
)
from custom_components.tuya_recordings.lib.native_session import NativeMediaFormat


MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42"


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


class EmptyMuxer(FakeMuxer):
    def __exit__(self, exc_type, exc, tb):
        return None


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
        self.failure_reason = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def handle_event(self, event):
        if event["payload"]["type"] == NativePlaybackFrameType.MEDIA_CODEC.value:
            self.output_path.write_bytes(b"jpeg")
            self.ready = True
            raise RuntimeError("stop stream")

    def require_thumbnail(self):
        if not self.ready:
            raise NativePlaybackError("no thumbnail")


class FailedThumbnailExtractor(FakeThumbnailExtractor):
    def handle_event(self, event):
        self.failure_reason = "decoder attempt limit reached"
        raise RuntimeError("private transport detail")


def playback_event(frame_type, payload=b"", info=None):
    return playback_frame_event(
        NativePlaybackFrame(frame_type, payload, info or {}),
        protocol=NATIVE_PLAYBACK_EVENT_VERSION,
    )


def video_packet():
    return playback_event(
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
    )


def audio_packet():
    return playback_event(
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
    )


def emit_audio_video(event_handler):
    event_handler(playback_event(NativePlaybackFrameType.STARTED))
    event_handler(video_packet())
    event_handler(audio_packet())
    event_handler(playback_event(NativePlaybackFrameType.FINISHED))


def test_native_playback_request_always_uses_raw_packets():
    request = native_playback_request(10, 20, play_time=12)

    assert request.media_format == NativeMediaFormat.RAW_PACKETS
    assert request.call_spec.uses_raw_packets is True


def test_native_playback_runner_receives_file_after_fresh_catalog(tmp_path):
    playbacks = []
    runner = NativePlaybackSessionRunner(
        lambda request, event_handler=None: (
            playbacks.append((request, event_handler)),
            emit_audio_video(event_handler),
        ),
        muxer_factory=FakeMuxer,
    )

    output = tmp_path / "clip.mp4"
    runner.receive_clip(10, 20, output, play_time=12)

    assert output.read_bytes() == MP4_BYTES
    assert playbacks[0][0].args == (10, 20, 12)
    assert callable(playbacks[0][1])


def test_native_playback_runner_streams_after_fresh_catalog():
    chunks = []
    runner = NativePlaybackSessionRunner(
        lambda request, event_handler=None: emit_audio_video(event_handler),
        stream_muxer_factory=FakeStreamMuxer,
    )

    runner.stream_clip(10, 20, chunks.append, play_time=12)

    assert chunks == [b"fmp4"]


def test_native_playback_runner_stops_after_thumbnail_is_ready(tmp_path):
    runner = NativePlaybackSessionRunner(
        lambda request, event_handler=None: emit_audio_video(event_handler),
        thumbnail_extractor_factory=FakeThumbnailExtractor,
    )
    output = tmp_path / "thumb.jpg"

    runner.receive_thumbnail(10, 20, output)

    assert output.read_bytes() == b"jpeg"


def test_native_playback_runner_reports_safe_thumbnail_failure_reason(tmp_path):
    runner = NativePlaybackSessionRunner(
        lambda request, event_handler=None: event_handler(video_packet()),
        thumbnail_extractor_factory=FailedThumbnailExtractor,
    )

    with pytest.raises(
        NativePlaybackError,
        match="thumbnail extraction failed: decoder attempt limit reached",
    ):
        runner.receive_thumbnail(10, 20, tmp_path / "thumb.jpg")


def test_native_playback_runner_rejects_missing_mp4_output(tmp_path):
    runner = NativePlaybackSessionRunner(
        lambda request, event_handler=None: emit_audio_video(event_handler),
        muxer_factory=EmptyMuxer,
    )

    with pytest.raises(NativePlaybackError, match="did not create"):
        runner.receive_clip(10, 20, tmp_path / "clip.mp4")


def test_native_playback_runner_rejects_video_only_callback_stream(tmp_path):
    runner = NativePlaybackSessionRunner(
        lambda request, event_handler=None: event_handler(video_packet()),
        muxer_factory=FakeMuxer,
    )

    with pytest.raises(NativePlaybackError, match="no raw audio"):
        runner.receive_clip(10, 20, tmp_path / "clip.mp4")


def test_native_playback_event_audit_counts_raw_audio_and_video():
    events = []
    audit = NativePlaybackEventAudit(events.append)

    audit.handle_event(video_packet())
    audit.handle_event(audio_packet())
    audit.require_audio_video()

    assert audit.video_packets == 1
    assert audit.audio_packets == 1
    assert len(events) == 2


def test_native_playback_event_audit_counts_g711u_audio():
    events = []
    audit = NativePlaybackEventAudit(events.append)
    audit.handle_event(video_packet())
    audit.handle_event(
        playback_event(
            NativePlaybackFrameType.MEDIA_CODEC,
            b"g711u",
            {
                "avChannel": 1,
                "frameNo": 2,
                "codecId": 133,
                "isKeyFrame": False,
                "timestamp": 8000,
                "frameInfoBase64": "",
                "sampleRate": 8000,
                "channels": 1,
            },
        )
    )

    audit.require_audio_video()
    assert audit.audio_packets == 1


def test_native_playback_event_audit_counts_pcm_s16le_audio():
    events = []
    audit = NativePlaybackEventAudit(events.append)
    audit.handle_event(video_packet())
    audit.handle_event(
        playback_event(
            NativePlaybackFrameType.MEDIA_CODEC,
            b"\x00\x00\x00\x01",
            {
                "avChannel": 1,
                "frameNo": 2,
                "codecId": 0xFFFE,
                "isKeyFrame": False,
                "timestamp": 8000,
                "frameInfoBase64": "",
                "sampleRate": 8000,
                "channels": 1,
            },
        )
    )

    audit.require_audio_video()
    assert audit.audio_packets == 1


def test_looks_like_mp4_checks_file_type_box(tmp_path):
    output = tmp_path / "clip.mp4"

    assert looks_like_mp4(output) is False
    output.write_bytes(b"not mp4")
    assert looks_like_mp4(output) is False
    output.write_bytes(MP4_BYTES)
    assert looks_like_mp4(output) is True
