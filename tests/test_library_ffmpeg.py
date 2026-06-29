from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from custom_components.tuya_recordings.lib import ffmpeg


def test_extract_mp4_thumbnail_replaces_temp_file(monkeypatch, tmp_path):
    video_path = tmp_path / "clip.mp4"
    thumbnail_path = tmp_path / "thumb.jpg"
    video_path.write_bytes(b"mp4")

    def fake_run(command, **kwargs):
        Path(command[-1]).write_bytes(b"jpg")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)

    ffmpeg.extract_mp4_thumbnail(video_path, thumbnail_path)

    assert thumbnail_path.read_bytes() == b"jpg"
    assert not thumbnail_path.with_suffix(".tmp.jpg").exists()


def test_finalize_mp4_for_browser_writes_faststart_temp(monkeypatch, tmp_path):
    input_path = tmp_path / "fragmented.mp4"
    output_path = tmp_path / "clip.mp4"
    input_path.write_bytes(b"mp4")

    def fake_run(command, **kwargs):
        assert "-movflags" in command
        assert "+faststart" in command
        Path(command[-1]).write_bytes(b"final")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)

    ffmpeg.finalize_mp4_for_browser(input_path, output_path)

    assert output_path.read_bytes() == b"final"
    assert not output_path.with_suffix(".faststart.tmp.mp4").exists()


def test_extract_h264_thumbnail_removes_temp_on_failure(monkeypatch, tmp_path):
    h264_path = tmp_path / "clip.h264"
    thumbnail_path = tmp_path / "thumb.jpg"
    h264_path.write_bytes(b"h264")

    def fake_run(command, **kwargs):
        Path(command[-1]).write_bytes(b"partial")
        return subprocess.CompletedProcess(command, 1, "", "bad input")

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="thumbnail"):
        ffmpeg.extract_h264_thumbnail(h264_path, thumbnail_path)

    assert not thumbnail_path.exists()
    assert not thumbnail_path.with_suffix(".tmp.jpg").exists()


def test_finish_live_h264_remux_requires_output(tmp_path):
    output_path = tmp_path / "clip.mp4"

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout=None):
            return "", ""

    with pytest.raises(RuntimeError, match="playable"):
        ffmpeg.finish_live_h264_remux(FakeProcess(), output_path)
