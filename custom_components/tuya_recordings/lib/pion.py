"""Pion helper process utilities for Tuya WebRTC sessions."""

from __future__ import annotations

import json
import os
import queue
import subprocess
from pathlib import Path
from typing import Any

from .webrtc import filter_webrtc_candidates, ice_servers


def create_webrtc_offer(helper_path: Path, process_module: Any = subprocess) -> str:
    if not helper_path.exists():
        raise RuntimeError(f"Pion WebRTC offer helper is missing: {helper_path}")

    result = process_module.run(
        [str(helper_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    sdp = result.stdout.strip()
    if not sdp.startswith("v=0"):
        raise RuntimeError("Pion WebRTC offer helper returned an invalid SDP offer")
    return filter_webrtc_candidates(sdp)


def start_pion_helper(
    helper_path: Path,
    config: dict[str, Any],
    *,
    query_timeout: int,
    playback_timeout: int,
    h264_output: Path | None = None,
    helper_timeout: int | None = None,
    process_module: Any = subprocess,
) -> subprocess.Popen[str]:
    if not helper_path.exists():
        raise RuntimeError(f"Pion WebRTC offer helper is missing: {helper_path}")

    proc_env = os.environ.copy()
    proc_env.update(
        {
            "TUYA_RECORDINGS_ICE_SERVERS": json.dumps(ice_servers(config)),
            "TUYA_RECORDINGS_REMOTE_SDP_TYPE": "answer",
            "TUYA_RECORDINGS_DUMMY_AUDIO_SENDRECV": "1",
            "TUYA_RECORDINGS_TRICKLE_CANDIDATES": "1",
            "TUYA_RECORDINGS_CHROME_SDP": "1",
            "TUYA_RECORDINGS_FORCE_STUN_CONTROLLED": "1",
        }
    )
    if h264_output is not None:
        proc_env["TUYA_RECORDINGS_H264_OUTPUT"] = str(h264_output)
    helper_timeout = helper_timeout or (playback_timeout if h264_output is not None else query_timeout)
    return process_module.Popen(
        [str(helper_path), "--interactive", "--timeout", f"{helper_timeout}s"],
        stdin=process_module.PIPE,
        stdout=process_module.PIPE,
        stderr=process_module.PIPE,
        text=True,
        env=proc_env,
    )


def read_helper_stdout(proc: subprocess.Popen[str], events: queue.Queue[dict[str, Any]]) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        events.put(event)


def read_helper_stderr(proc: subprocess.Popen[str], errors: queue.Queue[str]) -> None:
    assert proc.stderr is not None
    for line in proc.stderr:
        errors.put(line.strip())
