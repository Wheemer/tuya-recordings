import pytest

from custom_components.tuya_recordings.lib.native_frames import (
    NATIVE_PLAYBACK_EVENT_VERSION,
    NativePlaybackFrame,
    NativePlaybackFrameType,
    playback_frame_event,
)
from custom_components.tuya_recordings.lib.native_mux import (
    BROWSER_STREAM_MAGIC,
    browser_stream_header,
    BROWSER_STREAM_VIDEO,
    NativeMuxError,
    NativePlaybackBrowserStreamMuxer,
    NativePlaybackFmp4StreamMuxer,
    NativePlaybackInteractiveBrowserMuxer,
    NativePlaybackMuxer,
)


def event(frame_type, payload=b"", info=None):
    return playback_frame_event(
        NativePlaybackFrame(frame_type, payload, info or {}),
        protocol=NATIVE_PLAYBACK_EVENT_VERSION,
    )


def media_info(codec_name, codec_id, *, frame_no=1, key_frame=False, sample_rate=None, channels=None):
    info = {
        "avChannel": 0,
        "frameNo": frame_no,
        "codecId": codec_id,
        "codecName": codec_name,
        "isKeyFrame": key_frame,
        "timestamp": frame_no,
        "frameInfoBase64": "",
    }
    if sample_rate is not None:
        info["sampleRate"] = sample_rate
    if channels is not None:
        info["channels"] = channels
    return info


def test_native_muxer_remuxes_raw_video_and_audio_packets(tmp_path, monkeypatch):
    from custom_components.tuya_recordings.lib import native_mux
    received = []

    def mux(video_path, audio_path, packet_path, output, **kwargs):
        received.append((video_path.read_bytes(), audio_path.read_bytes(), packet_path.read_bytes(), kwargs))
        output.write_bytes(b"mp4")

    monkeypatch.setattr(native_mux, "mux_camera_packets", mux)
    output = tmp_path / "clip.mp4"
    with NativePlaybackMuxer(output) as muxer:
        muxer.handle_event(event(NativePlaybackFrameType.STARTED))
        muxer.handle_event(event(NativePlaybackFrameType.MEDIA_CODEC, b"h264", media_info("h264", 27, key_frame=True)))
        muxer.handle_event(
            event(
                NativePlaybackFrameType.MEDIA_CODEC,
                b"aac",
                media_info("aac", 86018, frame_no=2, sample_rate=8000, channels=1),
            )
        )
        muxer.handle_event(event(NativePlaybackFrameType.FINISHED))

    assert output.read_bytes() == b"mp4"
    assert received[0][0] == b"h264"
    assert received[0][1].endswith(b"aac")
    assert received[0][3]["audio_codec"] == "aac"
    assert not output.with_suffix(".packets.tmp").exists()
    assert not output.with_suffix(".video.h264").exists()
    assert not output.with_suffix(".audio.aac").exists()


def test_native_muxer_rejects_decoded_frames_for_no_transcoding_path(tmp_path):
    muxer = NativePlaybackMuxer(tmp_path / "clip.mp4")
    muxer.open()
    muxer.handle_event(event(NativePlaybackFrameType.MEDIA_CODEC, b"h264", media_info("h264", 27, key_frame=True)))
    with pytest.raises(NativeMuxError, match="decoded frames"):
        muxer.handle_event(
            event(
                NativePlaybackFrameType.AUDIO_PCM,
                b"\0\0",
                {"sampleRate": 8000, "channels": 1, "bitWidth": 16, "timestamp": 1},
            )
        )
    muxer.cleanup()


def test_native_muxer_requires_audio_by_default(tmp_path):
    muxer = NativePlaybackMuxer(tmp_path / "clip.mp4")
    muxer.open()
    muxer.handle_event(event(NativePlaybackFrameType.MEDIA_CODEC, b"h264", media_info("h264", 27, key_frame=True)))
    with pytest.raises(NativeMuxError, match="no audio"):
        muxer.finish()
    muxer.cleanup()


def test_native_muxer_rejects_unknown_raw_packet_codec(tmp_path):
    muxer = NativePlaybackMuxer(tmp_path / "clip.mp4")
    muxer.open()
    with pytest.raises(NativeMuxError, match="Unsupported"):
        muxer.handle_event(event(NativePlaybackFrameType.MEDIA_CODEC, b"data", media_info("vp9", 99)))
    muxer.cleanup()


def test_native_muxer_rejects_raw_aac_without_audio_metadata(tmp_path):
    muxer = NativePlaybackMuxer(tmp_path / "clip.mp4")
    muxer.open()
    with pytest.raises(NativeMuxError, match="sampleRate"):
        muxer.handle_event(event(NativePlaybackFrameType.MEDIA_CODEC, b"aac", media_info("aac", 86018)))
    muxer.cleanup()


def test_native_stream_muxer_requires_posix_pipes():
    with pytest.raises(NativeMuxError, match="POSIX pipes"):
        NativePlaybackFmp4StreamMuxer(lambda chunk: None, pipe_factory=None).open()


def test_browser_stream_reader_emits_a_small_fragment_without_waiting_for_64k():
    fragment = b"small-initial-fmp4-fragment"

    class Stdout:
        def __init__(self):
            self.reads = 0

        def read1(self, size):
            assert size == 64 * 1024
            self.reads += 1
            return fragment if self.reads == 1 else b""

        def read(self, _size):
            raise AssertionError("Buffered read() would delay initial playback")

    chunks = []
    muxer = NativePlaybackBrowserStreamMuxer(chunks.append)
    muxer._proc = type("Process", (), {"stdout": Stdout()})()
    muxer._first_video_timestamp = 90000

    muxer._read_stdout()

    assert muxer._output_bytes == len(fragment)
    assert len(chunks) == 1
    assert chunks[0][0] == BROWSER_STREAM_VIDEO
    assert int.from_bytes(chunks[0][1:9], "big") == 90000
    assert chunks[0][13:] == fragment


def test_interactive_browser_muxer_replaces_only_media_segment_on_seek():
    instances = []

    class SegmentMuxer:
        def __init__(self, callback):
            self.callback = callback
            self.opened = False
            self.cleaned = False
            self.finished = False
            self.events = []
            instances.append(self)

        def open(self):
            self.opened = True

        def handle_event(self, item):
            self.events.append(item)

        def cleanup(self):
            self.cleaned = True

        def finish(self):
            self.finished = True

    muxer = NativePlaybackInteractiveBrowserMuxer(
        lambda chunk: None,
        muxer_factory=SegmentMuxer,
    )
    muxer.handle_event(event(NativePlaybackFrameType.STARTED, info={"generation": 1, "position": 100}))
    muxer.handle_event(
        event(
            NativePlaybackFrameType.MEDIA_CODEC,
            b"h264",
            media_info("h264", 27, key_frame=True),
        )
    )
    muxer.handle_event(event(NativePlaybackFrameType.STARTED, info={"generation": 2, "position": 200}))
    muxer.finish()

    assert len(instances) == 2
    assert instances[0].opened and instances[0].cleaned
    assert len(instances[0].events) == 2
    assert instances[0].events[0]["payload"]["type"] == "started"
    assert instances[1].opened and instances[1].finished


def test_browser_stream_header_carries_generation_and_camera_position():
    header = browser_stream_header(42, 1_728_000_123)

    assert header[:4] == BROWSER_STREAM_MAGIC
    assert int.from_bytes(header[4:8], "big") == 42
    assert int.from_bytes(header[8:16], "big") == 1_728_000_123


def test_interactive_muxer_applies_native_seek_identity_to_browser_segment():
    instances = []

    class HeaderMuxer(NativePlaybackBrowserStreamMuxer):
        def __init__(self, callback):
            super().__init__(callback)
            self.opened = False
            self.cleaned = False
            self.finished = False
            self.events = []
            instances.append(self)

        def open(self):
            self.opened = True

        def handle_event(self, item):
            self.events.append(item)

        def cleanup(self):
            self.cleaned = True

        def finish(self):
            self.finished = True

    muxer = NativePlaybackInteractiveBrowserMuxer(
        lambda chunk: None,
        muxer_factory=HeaderMuxer,
    )
    muxer.handle_event(
        event(NativePlaybackFrameType.STARTED, info={"generation": 7, "position": 1_728_000_123})
    )
    muxer.finish()

    assert len(instances) == 1
    assert instances[0].stream_header == browser_stream_header(7, 1_728_000_123)
    assert instances[0].opened and instances[0].finished


def test_browser_stream_waits_for_decoder_config_and_idr_before_writing():
    written = []

    class RecordingMuxer(NativePlaybackBrowserStreamMuxer):
        def _write_video(self, data):
            written.append(data)

    muxer = RecordingMuxer(lambda chunk: None)
    sps = b"\x00\x00\x00\x01\x67sps"
    pps = b"\x00\x00\x00\x01\x68pps"
    inter = b"\x00\x00\x00\x01\x41inter"
    idr = b"\x00\x00\x00\x01\x65idr"

    muxer.handle_event(event(NativePlaybackFrameType.MEDIA_CODEC, sps, media_info("h264", 27)))
    muxer.handle_event(event(NativePlaybackFrameType.MEDIA_CODEC, pps, media_info("h264", 27)))
    muxer.handle_event(event(NativePlaybackFrameType.MEDIA_CODEC, inter, media_info("h264", 27)))
    assert written == []

    muxer.handle_event(event(NativePlaybackFrameType.MEDIA_CODEC, idr, media_info("h264", 27)))

    assert written == [sps + pps + idr]


def test_browser_stream_skips_video_until_first_sps():
    muxer = NativePlaybackBrowserStreamMuxer(lambda chunk: None)
    muxer.handle_event(event(NativePlaybackFrameType.MEDIA_CODEC, b"\x00\x00\x00\x01\x41abc", media_info("h264", 27)))
    assert muxer._video_bytes == 0
