import pytest

from custom_components.tuya_recordings.lib.native_frames import (
    NativeAudioFrameInfo,
    NativeMediaCodecFrameInfo,
    NativePlaybackEventSink,
    NativePlaybackFrame,
    NativePlaybackFrameSink,
    NativePlaybackFrameType,
    NativeVideoFrameInfo,
    playback_frame_event,
    validate_playback_frame_event,
)


class CollectingSink(NativePlaybackFrameSink):
    def __init__(self):
        self.frames = []

    def frame(self, frame):
        self.frames.append(frame.validate())


def test_sink_collects_apk_video_and_audio_callbacks():
    sink = CollectingSink()

    sink.started({"method": "startPlayBack"})
    sink.video_yuv(
        b"yyyy",
        b"uu",
        b"vv",
        NativeVideoFrameInfo(
            width=2,
            height=2,
            frame_rate=15,
            is_key_frame=True,
            timestamp=100,
            progress=1,
            duration=10,
        ),
    )
    sink.audio_pcm(
        b"\x00\x01",
        NativeAudioFrameInfo(
            sample_rate=8000,
            channels=1,
            bit_width=16,
            timestamp=100,
            progress=1,
            duration=10,
        ),
    )
    sink.finished()

    assert [frame.frame_type for frame in sink.frames] == [
        NativePlaybackFrameType.STARTED,
        NativePlaybackFrameType.VIDEO_YUV,
        NativePlaybackFrameType.AUDIO_PCM,
        NativePlaybackFrameType.FINISHED,
    ]
    assert sink.frames[1].payload == b"yyyyuuvv"
    assert sink.frames[1].info["yLength"] == 4
    assert sink.frames[2].info["sampleRate"] == 8000


def test_sink_collects_apk_media_codec_callback_shape():
    sink = CollectingSink()

    sink.media_codec(
        b"\x00\x00\x00\x01",
        av_channel=0,
        frame_no=123,
        codec_id=27,
        is_key_frame=True,
        timestamp=456,
        frame_info=b"info",
    )

    frame = sink.frames[0]
    assert frame.frame_type == NativePlaybackFrameType.MEDIA_CODEC
    assert frame.payload == b"\x00\x00\x00\x01"
    assert frame.info == {
        "avChannel": 0,
        "frameNo": 123,
        "codecId": 27,
        "codec": 27,
        "isKeyFrame": True,
        "timestamp": 456,
        "frameInfoBase64": "aW5mbw==",
    }


def test_media_codec_info_rejects_invalid_callback_values():
    with pytest.raises(ValueError, match="frame_no"):
        NativeMediaCodecFrameInfo(0, -1, 27, True).to_jsonable()
    with pytest.raises(ValueError, match="is_key_frame"):
        NativeMediaCodecFrameInfo(0, 1, 27, 1).to_jsonable()


def test_frame_info_rejects_invalid_apk_callback_values():
    with pytest.raises(ValueError, match="sample_rate"):
        NativeAudioFrameInfo(0, 1, 16, 0).to_jsonable()
    with pytest.raises(ValueError, match="width"):
        NativeVideoFrameInfo(0, 2, 15, False, 0, 0, 0).to_jsonable()


def test_sink_rejects_empty_error_message():
    with pytest.raises(ValueError, match="message"):
        CollectingSink().error("")


def test_playback_frame_event_uses_json_safe_base64_payload():
    event = playback_frame_event(
        NativePlaybackFrame(NativePlaybackFrameType.AUDIO_PCM, b"\x00\x01", {"sampleRate": 8000}),
        protocol=1,
    )

    assert event == {
        "protocol": 1,
        "event": "playbackFrame",
        "payload": {
            "type": "audio-pcm",
            "payloadBase64": "AAE=",
            "info": {"sampleRate": 8000},
        },
    }


def test_validate_playback_frame_event_accepts_apk_audio_and_video_frames():
    video_event = playback_frame_event(
        NativePlaybackFrame(
            NativePlaybackFrameType.VIDEO_YUV,
            b"yyyyuuvv",
            {
                "width": 2,
                "height": 2,
                "frameRate": 15,
                "isKeyFrame": True,
                "timestamp": 100,
                "progress": 1,
                "duration": 10,
                "yLength": 4,
                "uLength": 2,
                "vLength": 2,
            },
        ),
        protocol=1,
    )
    audio_event = playback_frame_event(
        NativePlaybackFrame(
            NativePlaybackFrameType.AUDIO_PCM,
            b"\x00\x01",
            {
                "sampleRate": 8000,
                "channels": 1,
                "bitWidth": 16,
                "timestamp": 100,
                "progress": 1,
                "duration": 10,
            },
        ),
        protocol=1,
    )

    assert validate_playback_frame_event(video_event, protocol=1) == video_event
    assert validate_playback_frame_event(audio_event, protocol=1) == audio_event


def test_validate_playback_frame_event_accepts_apk_media_codec_callback():
    event = playback_frame_event(
        NativePlaybackFrame(
            NativePlaybackFrameType.MEDIA_CODEC,
            b"packet",
            {
                "avChannel": 0,
                "frameNo": 1,
                "codecId": 27,
                "codec": 27,
                "isKeyFrame": True,
                "timestamp": 100,
                "frameInfoBase64": "",
            },
        ),
        protocol=1,
    )

    assert validate_playback_frame_event(event, protocol=1) == event


def test_validate_playback_frame_event_rejects_bad_yuv_lengths():
    event = playback_frame_event(
        NativePlaybackFrame(
            NativePlaybackFrameType.VIDEO_YUV,
            b"yyyy",
            {
                "width": 2,
                "height": 2,
                "frameRate": 15,
                "isKeyFrame": True,
                "timestamp": 100,
                "progress": 1,
                "duration": 10,
                "yLength": 4,
                "uLength": 2,
                "vLength": 2,
            },
        ),
        protocol=1,
    )

    with pytest.raises(ValueError, match="YUV payload lengths"):
        validate_playback_frame_event(event, protocol=1)


def test_event_sink_emits_validated_helper_events():
    events = []
    sink = NativePlaybackEventSink(events.append, protocol=1)

    sink.started({"method": "startPlayBack"})
    sink.audio_pcm(
        b"\x00\x01",
        NativeAudioFrameInfo(
            sample_rate=8000,
            channels=1,
            bit_width=16,
            timestamp=100,
        ),
    )

    assert [event["payload"]["type"] for event in events] == ["started", "audio-pcm"]
    assert events[1]["payload"]["payloadBase64"] == "AAE="
