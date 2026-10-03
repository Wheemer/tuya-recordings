import subprocess
from types import SimpleNamespace

import pytest

from custom_components.tuya_recordings.lib.native_frames import (
    NATIVE_PLAYBACK_EVENT_VERSION,
    NativePlaybackFrame,
    NativePlaybackFrameType,
    playback_frame_event,
)
from custom_components.tuya_recordings.lib.native_mux import NativeMuxError
from custom_components.tuya_recordings.lib.native_thumbnail import (
    NativePlaybackThumbnailExtractor,
    NativeThumbnailReady,
)


JPEG = b"\xff\xd8thumbnail\xff\xd9"


class FakeProcessModule:
    PIPE = subprocess.PIPE
    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, results):
        self.results = list(results)
        self.commands = []

    def run(self, command, **kwargs):
        self.commands.append((command, kwargs))
        return self.results.pop(0)


def packet(data=b"h264", *, key_frame=True, codec="h264"):
    return playback_frame_event(
        NativePlaybackFrame(
            NativePlaybackFrameType.MEDIA_CODEC,
            data,
            {
                "avChannel": 0,
                "frameNo": 1,
                "codecId": 27 if codec == "h264" else 86018,
                "codecName": codec,
                "isKeyFrame": key_frame,
                "timestamp": 1,
                "frameInfoBase64": "",
            },
        ),
        protocol=NATIVE_PLAYBACK_EVENT_VERSION,
    )


def test_extractor_writes_jpeg_and_stops_after_first_decodable_frame(tmp_path):
    process = FakeProcessModule([SimpleNamespace(returncode=0, stdout=JPEG, stderr=b"")])
    output = tmp_path / "thumb.jpg"

    with NativePlaybackThumbnailExtractor(output, process_module=process) as extractor:
        with pytest.raises(NativeThumbnailReady):
            extractor.handle_event(packet())
        extractor.require_thumbnail()

    assert output.read_bytes() == JPEG
    assert process.commands[0][1]["input"] == b"h264"
    assert "-frames:v" in process.commands[0][0]


def test_extractor_ignores_audio_and_waits_for_key_frame(tmp_path):
    process = FakeProcessModule([SimpleNamespace(returncode=0, stdout=JPEG, stderr=b"")])
    output = tmp_path / "thumb.jpg"

    with NativePlaybackThumbnailExtractor(output, process_module=process) as extractor:
        extractor.handle_event(packet(b"aac", codec="aac"))
        extractor.handle_event(packet(b"delta", key_frame=False))
        assert process.commands == []
        with pytest.raises(NativeThumbnailReady):
            extractor.handle_event(packet(b"key", key_frame=True))

    assert process.commands[0][1]["input"] == b"deltakey"


def test_extractor_retries_after_more_packets(tmp_path):
    process = FakeProcessModule(
        [
            SimpleNamespace(returncode=1, stdout=b"", stderr=b"incomplete"),
            SimpleNamespace(returncode=0, stdout=JPEG, stderr=b""),
        ]
    )
    output = tmp_path / "thumb.jpg"

    with NativePlaybackThumbnailExtractor(
        output, process_module=process, retry_packet_interval=2
    ) as extractor:
        extractor.handle_event(packet(b"key", key_frame=True))
        extractor.handle_event(packet(b"delta1", key_frame=False))
        with pytest.raises(NativeThumbnailReady):
            extractor.handle_event(packet(b"delta2", key_frame=False))

    assert len(process.commands) == 2
    assert output.read_bytes() == JPEG


def test_extractor_removes_partial_output_when_no_frame_is_decodable(tmp_path):
    process = FakeProcessModule([SimpleNamespace(returncode=1, stdout=b"", stderr=b"bad")])
    output = tmp_path / "thumb.jpg"

    with NativePlaybackThumbnailExtractor(output, process_module=process) as extractor:
        extractor.handle_event(packet())
        with pytest.raises(NativeMuxError, match="ended before"):
            extractor.require_thumbnail()

    assert not output.exists()


def test_extractor_reports_missing_keyframe_without_transport_details(tmp_path):
    process = FakeProcessModule([])
    output = tmp_path / "thumb.jpg"

    with NativePlaybackThumbnailExtractor(output, process_module=process) as extractor:
        extractor.handle_event(packet(b"delta", key_frame=False))
        with pytest.raises(NativeMuxError, match="before a thumbnail keyframe"):
            extractor.require_thumbnail()

    assert extractor.failure_reason == (
        "Native playback ended before a thumbnail keyframe was received"
    )
