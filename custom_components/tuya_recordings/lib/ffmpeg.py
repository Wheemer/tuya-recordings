"""ffmpeg helpers for Tuya recording media files."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def remux_h264_to_mp4(h264_path: Path, output_path: Path) -> None:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "+genpts",
            "-f",
            "h264",
            "-i",
            str(h264_path),
            "-an",
            "-c:v",
            "copy",
            "-movflags",
            "+faststart",
            "-y",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed to remux Tuya recording: {result.stderr.strip() or result.stdout.strip()}")
    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError("ffmpeg did not create a playable Tuya recording")


def finalize_mp4_for_browser(input_path: Path, output_path: Path) -> None:
    temp_path = output_path.with_suffix(".faststart.tmp.mp4")
    temp_path.unlink(missing_ok=True)
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-i",
            str(input_path),
            "-map",
            "0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-y",
            str(temp_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed to finalize Tuya recording for browser playback: {result.stderr.strip() or result.stdout.strip()}")
    if not temp_path.exists() or temp_path.stat().st_size <= 0:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg did not create a browser-playable Tuya recording")
    temp_path.replace(output_path)


def start_live_h264_remux(h264_path: Path, output_path: Path) -> subprocess.Popen[str]:
    if hasattr(os, "mkfifo"):
        os.mkfifo(h264_path)
    else:
        h264_path.touch()
    return subprocess.Popen(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "+genpts",
            "-f",
            "h264",
            "-i",
            str(h264_path),
            "-an",
            "-c:v",
            "copy",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            "-y",
            str(output_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def finish_live_h264_remux(proc: subprocess.Popen[str], output_path: Path) -> None:
    try:
        stdout, stderr = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired as exc:
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=5)
        raise RuntimeError("ffmpeg did not finish live Tuya remux") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed to live-remux Tuya recording: {stderr.strip() or stdout.strip()}")
    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError("ffmpeg did not create a playable Tuya recording")


def extract_mp4_thumbnail(video_path: Path, thumbnail_path: Path) -> None:
    temp_path = thumbnail_path.with_suffix(".tmp.jpg")
    temp_path.unlink(missing_ok=True)
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-ss",
            "1",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-vf",
            "scale='min(640,iw)':-2",
            "-q:v",
            "3",
            "-y",
            str(temp_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed to create Tuya recording thumbnail: {result.stderr.strip() or result.stdout.strip()}")
    if not temp_path.exists() or temp_path.stat().st_size <= 0:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg did not create a Tuya recording thumbnail")
    temp_path.replace(thumbnail_path)


def extract_h264_thumbnail(h264_path: Path, thumbnail_path: Path) -> None:
    temp_path = thumbnail_path.with_suffix(".tmp.jpg")
    temp_path.unlink(missing_ok=True)
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "h264",
            "-i",
            str(h264_path),
            "-frames:v",
            "1",
            "-vf",
            "scale='min(640,iw)':-2",
            "-q:v",
            "3",
            "-y",
            str(temp_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed to create Tuya recording thumbnail: {result.stderr.strip() or result.stdout.strip()}")
    if not temp_path.exists() or temp_path.stat().st_size <= 0:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg did not create a Tuya recording thumbnail")
    temp_path.replace(thumbnail_path)
