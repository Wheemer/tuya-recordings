import json
import os
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

from custom_components.tuya_recordings.lib.ffmpeg import extract_thumbnail_from_mp4
from custom_components.tuya_recordings.lib.native_frames import (
    NATIVE_PLAYBACK_EVENT_VERSION,
    NativePlaybackFrame,
    NativePlaybackFrameType,
    playback_frame_event,
)
from custom_components.tuya_recordings.lib.native_mux import (
    BROWSER_STREAM_AUDIO_PCM_S16LE,
    BROWSER_STREAM_AUDIO_FORMAT,
    BROWSER_STREAM_END,
    BROWSER_STREAM_MAGIC,
    BROWSER_STREAM_VIDEO,
    NativePlaybackBrowserStreamMuxer,
    NativePlaybackFmp4StreamMuxer,
)
from custom_components.tuya_recordings.lib.native_thumbnail import (
    NativePlaybackThumbnailExtractor,
    NativeThumbnailReady,
)


pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="real fragmented-MP4 validation requires POSIX, ffmpeg, and ffprobe",
)


def _event(frame_type, payload=b"", info=None):
    return playback_frame_event(
        NativePlaybackFrame(frame_type, payload, info or {}),
        protocol=NATIVE_PLAYBACK_EVENT_VERSION,
    )


def _media_info(
    codec_name,
    codec_id,
    *,
    channel,
    key_frame,
    sample_rate=None,
    channels=None,
    timestamp=1,
):
    info = {
        "avChannel": channel,
        "frameNo": 1,
        "codecId": codec_id,
        "codecName": codec_name,
        "isKeyFrame": key_frame,
        "timestamp": timestamp,
        "frameInfoBase64": "",
    }
    if sample_rate is not None:
        info["sampleRate"] = sample_rate
    if channels is not None:
        info["channels"] = channels
    return info


def _generate_elementary_streams(
    tmp_path: Path, *, duration: int = 1
) -> tuple[bytes, bytes]:
    video_path = tmp_path / "fixture.h264"
    audio_path = tmp_path / "fixture.aac"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=5",
            "-t",
            str(duration),
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-g",
            "5",
            "-an",
            "-f",
            "h264",
            "-y",
            str(video_path),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:sample_rate=8000",
            "-t",
            str(duration),
            "-c:a",
            "aac",
            "-b:a",
            "32k",
            "-ac",
            "1",
            "-f",
            "adts",
            "-y",
            str(audio_path),
        ],
        check=True,
        capture_output=True,
    )
    return video_path.read_bytes(), audio_path.read_bytes()


def test_progressive_muxer_emits_browser_mp4_with_h264_and_aac(tmp_path):
    video, audio = _generate_elementary_streams(tmp_path)
    chunks = []

    with NativePlaybackFmp4StreamMuxer(chunks.append) as muxer:
        muxer.handle_event(_event(NativePlaybackFrameType.STARTED))
        muxer.handle_event(
            _event(
                NativePlaybackFrameType.MEDIA_CODEC,
                video,
                _media_info("h264", 1, channel=0, key_frame=True),
            )
        )
        muxer.handle_event(
            _event(
                NativePlaybackFrameType.MEDIA_CODEC,
                audio,
                _media_info(
                    "aac",
                    4,
                    channel=1,
                    key_frame=False,
                    sample_rate=8000,
                    channels=1,
                ),
            )
        )
        muxer.handle_event(_event(NativePlaybackFrameType.FINISHED))

    output = b"".join(chunks)
    assert output[4:8] == b"ftyp"
    assert b"moof" in output
    assert b"mdat" in output
    output_path = tmp_path / "stream.mp4"
    output_path.write_bytes(output)
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,codec_type",
            "-of",
            "json",
            str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    streams = json.loads(result.stdout)["streams"]
    assert {(stream["codec_type"], stream["codec_name"]) for stream in streams} == {
        ("audio", "aac"),
        ("video", "h264"),
    }


def test_native_playback_thumbnail_extractor_decodes_real_h264(tmp_path):
    video, _ = _generate_elementary_streams(tmp_path)
    output = tmp_path / "thumb.jpg"

    with NativePlaybackThumbnailExtractor(output) as extractor:
        with pytest.raises(NativeThumbnailReady):
            extractor.handle_event(
                _event(
                    NativePlaybackFrameType.MEDIA_CODEC,
                    video,
                    _media_info("h264", 1, channel=0, key_frame=True),
                )
            )
        extractor.require_thumbnail()

    assert output.read_bytes().startswith(b"\xff\xd8")
    assert output.read_bytes().endswith(b"\xff\xd9")


def test_cached_mp4_thumbnail_extractor_decodes_real_video(tmp_path):
    source = tmp_path / "cached.mp4"
    output = tmp_path / "cached.jpg"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=5",
            "-t",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-an",
            "-y",
            str(source),
        ],
        check=True,
        capture_output=True,
    )

    extract_thumbnail_from_mp4(source, output)

    assert output.read_bytes().startswith(b"\xff\xd8")
    assert output.read_bytes().endswith(b"\xff\xd9")
    assert not output.with_suffix(".jpg.tmp").exists()


def test_browser_stream_copies_h264_and_preserves_pcm_s16le(tmp_path):
    video, _audio = _generate_elementary_streams(tmp_path)
    chunks = []
    pcm_s16le = b"\x00\x00\x00\x20\x00\xe0\x00\x40" * 40

    with NativePlaybackBrowserStreamMuxer(chunks.append) as muxer:
        muxer.handle_event(_event(NativePlaybackFrameType.STARTED))
        muxer.handle_event(
            _event(
                NativePlaybackFrameType.MEDIA_CODEC,
                video,
                _media_info("h264", 1, channel=0, key_frame=True, timestamp=90000),
            )
        )
        muxer.handle_event(
            _event(
                NativePlaybackFrameType.MEDIA_CODEC,
                pcm_s16le,
                _media_info(
                    "pcm_s16le",
                    0xFFFE,
                    channel=1,
                    key_frame=False,
                    sample_rate=16000,
                    channels=2,
                    timestamp=93000,
                ),
            )
        )
        muxer.handle_event(_event(NativePlaybackFrameType.FINISHED))

    assert chunks[0] == BROWSER_STREAM_MAGIC
    records = []
    for chunk in chunks[1:]:
        kind = chunk[0]
        timestamp = int.from_bytes(chunk[1:9], "big")
        size = int.from_bytes(chunk[9:13], "big")
        assert len(chunk) == 13 + size
        records.append((kind, timestamp, chunk[13:]))
    assert any(
        kind == BROWSER_STREAM_AUDIO_PCM_S16LE and data == pcm_s16le
        for kind, _, data in records
    )
    assert any(
        kind == BROWSER_STREAM_AUDIO_FORMAT
        and int.from_bytes(data[:4], "big") == 16000
        and data[4] == 2
        for kind, _, data in records
    )
    assert all(
        timestamp == 90000
        for kind, timestamp, _ in records
        if kind == BROWSER_STREAM_VIDEO
    )
    assert records[-1] == (BROWSER_STREAM_END, 0, b"")

    output = tmp_path / "browser-video.mp4"
    output.write_bytes(
        b"".join(
            data for kind, _, data in records if kind == BROWSER_STREAM_VIDEO
        )
    )
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,codec_type",
            "-of",
            "json",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout)["streams"] == [
        {"codec_name": "h264", "codec_type": "video"}
    ]


def test_browser_stream_emits_initial_video_before_input_closes(tmp_path):
    video, _audio = _generate_elementary_streams(tmp_path, duration=2)
    first_video = threading.Event()
    chunks = []

    def receive(chunk):
        chunks.append(chunk)
        if len(chunk) >= 13 and chunk[0] == BROWSER_STREAM_VIDEO:
            first_video.set()

    muxer = NativePlaybackBrowserStreamMuxer(receive)
    muxer.open()
    try:
        muxer.handle_event(_event(NativePlaybackFrameType.STARTED))
        muxer.handle_event(
            _event(
                NativePlaybackFrameType.MEDIA_CODEC,
                video,
                _media_info("h264", 1, channel=0, key_frame=True, timestamp=90000),
            )
        )

        assert first_video.wait(3), "initial fMP4 video stayed buffered until EOF"
        assert any(
            len(chunk) >= 13 and chunk[0] == BROWSER_STREAM_VIDEO
            for chunk in chunks
        )
    finally:
        muxer.cleanup()
