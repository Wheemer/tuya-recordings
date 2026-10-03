from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.tuya_recordings.lib.ffmpeg import (
    extract_thumbnail_from_mp4,
    finalize_mp4_for_browser,
)


def test_extract_thumbnail_from_cached_mp4_is_atomic(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    output = tmp_path / "thumb.jpg"
    source.write_bytes(b"cached recording")

    def fake_run(command, **kwargs):
        assert command[-1] == "pipe:1"
        return SimpleNamespace(
            returncode=0, stdout=b"\xff\xd8jpeg\xff\xd9", stderr=b""
        )

    monkeypatch.setattr(
        "custom_components.tuya_recordings.lib.ffmpeg.subprocess.run", fake_run
    )

    extract_thumbnail_from_mp4(source, output)

    assert output.read_bytes() == b"\xff\xd8jpeg\xff\xd9"
    assert not output.with_suffix(".jpg.tmp").exists()


def test_extract_thumbnail_rejects_invalid_output_and_cleans_up(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    output = tmp_path / "thumb.jpg"
    source.write_bytes(b"cached recording")

    def fake_run(command, **kwargs):
        assert command[-1] == "pipe:1"
        return SimpleNamespace(returncode=0, stdout=b"not a jpeg", stderr=b"")

    monkeypatch.setattr(
        "custom_components.tuya_recordings.lib.ffmpeg.subprocess.run", fake_run
    )

    with pytest.raises(RuntimeError, match="valid cached recording JPEG"):
        extract_thumbnail_from_mp4(source, output)

    assert not output.exists()
    assert not output.with_suffix(".jpg.tmp").exists()


def test_finalize_mp4_requires_audio_and_video_streams(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    output = tmp_path / "clip.mp4"
    source.write_bytes(b"source")

    def fake_run(command, **kwargs):
        if command[0] == "ffmpeg":
            output.with_suffix(".faststart.tmp.mp4").write_bytes(b"mp4")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout='{"streams":[{"codec_type":"video"},{"codec_type":"audio"}]}',
            stderr="",
        )

    monkeypatch.setattr("custom_components.tuya_recordings.lib.ffmpeg.subprocess.run", fake_run)

    finalize_mp4_for_browser(source, output)

    assert output.read_bytes() == b"mp4"


def test_finalize_mp4_rejects_silent_recording(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    output = tmp_path / "clip.mp4"
    temp = output.with_suffix(".faststart.tmp.mp4")
    source.write_bytes(b"source")

    def fake_run(command, **kwargs):
        if command[0] == "ffmpeg":
            temp.write_bytes(b"mp4")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout='{"streams":[{"codec_type":"video"}]}', stderr="")

    monkeypatch.setattr("custom_components.tuya_recordings.lib.ffmpeg.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="audio_stream=False"):
        finalize_mp4_for_browser(source, output)

    assert not output.exists()
    assert not temp.exists()


def test_finalize_mp4_allows_missing_ffprobe(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    output = tmp_path / "clip.mp4"
    source.write_bytes(b"source")

    def fake_run(command, **kwargs):
        if command[0] == "ffmpeg":
            output.with_suffix(".faststart.tmp.mp4").write_bytes(b"mp4")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise FileNotFoundError

    monkeypatch.setattr("custom_components.tuya_recordings.lib.ffmpeg.subprocess.run", fake_run)

    finalize_mp4_for_browser(source, output)

    assert output.exists()
