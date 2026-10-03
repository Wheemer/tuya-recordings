"""ffmpeg helpers for Tuya recording media files."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def finalize_mp4_for_browser(input_path: Path, output_path: Path, *, require_audio: bool = True) -> None:
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
    if require_audio:
        try:
            _require_audio_video_streams(temp_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
    temp_path.replace(output_path)


def extract_thumbnail_from_mp4(input_path: Path, output_path: Path) -> None:
    """Decode one JPEG from an already-cached MP4 without camera activity."""
    input_path = Path(input_path)
    output_path = Path(output_path)
    if not input_path.is_file() or input_path.stat().st_size <= 0:
        raise RuntimeError("Cached Tuya recording is unavailable for thumbnail extraction")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    temp_path.unlink(missing_ok=True)
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(input_path),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-vf",
                "scale=min(640\\,iw):-2",
                "-q:v",
                "3",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ],
            capture_output=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "ffmpeg failed to create a cached recording thumbnail: "
                f"{result.stderr.decode(errors='replace').strip()}"
            )
        image = result.stdout
        if not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9"):
            raise RuntimeError("ffmpeg did not create a valid cached recording JPEG")
        temp_path.write_bytes(image)
        temp_path.replace(output_path)
    finally:
        temp_path.unlink(missing_ok=True)


def _require_audio_video_streams(path: Path) -> None:
    streams = _probe_stream_codecs(path)
    if streams is None:
        return
    has_video = any(stream.get("codec_type") == "video" for stream in streams)
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    if not has_video or not has_audio:
        raise RuntimeError(
            "Tuya recording is not playable with audio: "
            f"video_stream={has_video} audio_stream={has_audio}"
        )


def _probe_stream_codecs(path: Path) -> list[dict] | None:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-hide_banner",
                "-loglevel",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed to inspect Tuya recording: {result.stderr.strip() or result.stdout.strip()}")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe returned invalid Tuya recording stream data") from exc
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise RuntimeError("ffprobe returned no Tuya recording stream list")
    return [stream for stream in streams if isinstance(stream, dict)]
