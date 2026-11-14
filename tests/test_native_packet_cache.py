"""Actual packet-remux and browser checks with uneven camera timestamps."""

import base64
import io
import struct
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import av
import pytest
from playwright.sync_api import sync_playwright

from custom_components.tuya_recordings.lib.native_frames import (
    NativePlaybackFrame, NativePlaybackFrameType, playback_frame_event,
)
from custom_components.tuya_recordings.lib.native_mux import NativePlaybackMuxer, NativePlaybackBrowserStreamMuxer, NativeMuxError, browser_stream_header
from custom_components.tuya_recordings.lib.ffmpeg import finalize_mp4_for_browser
from custom_components.tuya_recordings.lib.native_packet_cache import TimestampedVideoMuxer, TimestampedAacMuxer, NativePresentationClock


def native_stream_server(stream):
    frontend = Path(__file__).resolve().parents[1] / "custom_components/tuya_recordings/frontend"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                data = b"<div><video playsinline></video><div role='status'></div></div>"
                mime = "text/html"
            elif path == "/stream":
                data, mime = stream, "application/octet-stream"
            else:
                file = (frontend / path.removeprefix("/frontend/")).resolve()
                if not file.is_relative_to(frontend) or not file.is_file():
                    self.send_error(404)
                    return
                data = file.read_bytes()
                mime = "text/javascript" if file.suffix == ".js" else "image/svg+xml"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def test_presentation_clock_keeps_scale_and_valid_sparse_cadence():
    clock = NativePresentationClock()
    assert clock.map(1, 1000, 1000) == (1000, 1000)
    assert clock.map(1, 1200000, 1_000_000) == (1200, 1000)
    assert clock.map(1, 2249, 1000) == (2249, 1000)
    assert clock._epoch == 0
    clock.map(1, 3299, 1000)
    assert clock._epoch == 1


def test_audio_packets_sharing_a_source_timestamp_do_not_reset_video_clock():
    from fractions import Fraction
    clock = NativePresentationClock()
    base = 1794650510000
    duration = Fraction(62, 1000)
    for offset in (0, 124, 248, 374, 498, 622, 748, 874):
        video, scale = clock.map(1, base + offset, 1000)
        assert (video, scale) == (base + offset, 1000)
        first, _ = clock.map(2, base + offset, 1000, duration=duration)
        second, _ = clock.map(2, base + offset, 1000, duration=duration)
        assert second - first == 62
        assert clock._epoch == 0


def test_presentation_clock_allows_queued_old_track_to_finish_before_joining_new_epoch():
    from fractions import Fraction
    clock = NativePresentationClock()
    clock.map(1, 1000, 1000)
    clock.map(2, 1000, 1000, duration=Fraction(1, 50))
    clock.map(1, 1040, 1000)
    clock.map(2, 1020, 1000, duration=Fraction(1, 50))
    new_audio = clock.map(2, 70000, 1000, duration=Fraction(1, 50))
    assert clock.map(1, 1060, 1000) == (1060, 1000)
    assert clock.map(1, 70000, 1000) == new_audio
    assert clock._epoch == 1 and len(clock._tracks) == 2


def test_presentation_clock_reanchors_late_track_after_old_tail_overtakes_epoch():
    from fractions import Fraction
    clock = NativePresentationClock()
    clock.map(1, 1000, 1000)
    clock.map(2, 1000, 1000, duration=Fraction(1, 50))
    clock.map(1, 1040, 1000)
    clock.map(2, 1020, 1000, duration=Fraction(1, 50))
    clock.map(2, 70000, 1000, duration=Fraction(1, 50))
    for timestamp in (1080, 1120, 1160):
        clock.map(1, timestamp, 1000)
    video, scale = clock.map(1, 70000, 1000)
    audio, _ = clock.map(2, 70020, 1000, duration=Fraction(1, 50))
    assert scale == 1000 and video > 1160
    assert audio - video == 20
    assert len(clock._tracks) == 2


@pytest.mark.parametrize("first", [1, 2])
def test_presentation_clock_repeated_staggered_transitions_are_monotonic(first):
    from fractions import Fraction
    clock = NativePresentationClock()
    previous = {1: -1, 2: -1}
    base = 10000
    for fragment in range(100):
        next_base = base + (69000 if fragment % 2 == 0 else -50000)
        order = (first, 3 - first)
        for step in range(5):
            for kind in order:
                timestamp = next_base + step * 40
                value, _ = clock.map(kind, timestamp, 1000,
                                     duration=Fraction(1, 25) if kind == 2 else None)
                assert value > previous[kind]
                previous[kind] = value
        assert abs(previous[1] - previous[2]) <= 80
        base = next_base
    assert len(clock._tracks) == 2


def test_packet_mux_rejects_oversized_duration_before_ffmpeg(encoded_video):
    muxer = TimestampedVideoMuxer(encoded_video[0], lambda data: None)
    try:
        muxer.push(encoded_video[0], 0, 1_000_000, True)
        with pytest.raises(ValueError, match="duration_us=3377270400"):
            muxer.push(encoded_video[1], 3377270400, 1_000_000, False)
    finally:
        muxer.close(flush=False)


def test_live_mux_preserves_initialization_error_if_cleanup_fails(monkeypatch):
    class BrokenSource:
        @property
        def streams(self):
            raise ValueError("invalid codec header")

        def close(self):
            raise RuntimeError("source cleanup failed")

    monkeypatch.setattr(av, "open", lambda *args, **kwargs: BrokenSource())
    with pytest.raises(ValueError, match="invalid codec header") as caught:
        TimestampedVideoMuxer(b"invalid", lambda data: None)
    assert caught.value.__notes__ == [
        "Packet mux cleanup also failed: RuntimeError"
    ]


@pytest.fixture
def encoded_video(tmp_path):
    path = tmp_path / "source.h264"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "testsrc=size=160x90:rate=5:duration=1", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-g", "5", "-an", "-y", str(path)], check=True, timeout=15)
    with av.open(str(path), format="h264") as source:
        return [bytes(packet) for packet in source.demux(video=0) if packet.size]


def media(data, codec, timestamp, *, audio=False):
    return playback_frame_event(NativePlaybackFrame(NativePlaybackFrameType.MEDIA_CODEC,
        data, {"avChannel": int(audio), "frameNo": 1, "codecId": {"aac": 4, "g711u": 133, "g711a": 132, "pcm_s16le": 0xFFFE}.get(codec, 1),
        "codecName": codec, "isKeyFrame": not audio, "timestamp": timestamp,
        "timestampScale": 1000, "sampleRate": 8000, "channels": 1}), protocol=1)


@pytest.mark.parametrize("audio_codec,width", [("g711u", 1), ("pcm_s16le", 2)])
@pytest.mark.parametrize("fragment_reset", [False, True])
@pytest.mark.parametrize("separate_headers", [False, True])
@pytest.mark.parametrize("via_sync", [False, True])
def test_cached_reordered_h264_retains_pictures_audio_and_distinct_decode_clock(tmp_path, monkeypatch, audio_codec, width, fragment_reset, separate_headers, via_sync):
    import hashlib
    path = tmp_path / "bframes.h264"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "testsrc=size=160x90:rate=10:duration=2", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", "medium", "-bf", "2", "-g", "10",
        "-x264-params", "b-adapt=0:b-pyramid=0:scenecut=0", "-an", "-y", str(path)],
        check=True, timeout=20)
    with av.open(str(path), format="h264") as source:
        assert source.streams.video[0].codec_context.has_b_frames
        packets = [(bytes(packet), packet.is_keyframe) for packet in source.demux(video=0) if packet.size]
    def hashes(source):
        result = []
        for frame in source.decode(video=0):
            rgb = frame.reformat(format="rgb24")
            plane = rgb.planes[0]
            raw = bytes(plane)
            pixels = b"".join(raw[y * plane.line_size:y * plane.line_size + rgb.width * 3]
                              for y in range(rgb.height))
            result.append(hashlib.sha256(pixels).hexdigest())
        return result
    with av.open(str(path), format="h264") as source:
        expected = hashes(source)
    audio = (bytes([0x80, 0, 0xAA, 0x2A]) * 4000 if width == 1
             else struct.pack("<hhhh", 0, 8192, -8192, 16384) * 4000)
    output = tmp_path / "cached.mp4"
    from custom_components.tuya_recordings.lib.native_mux import _h264_parameter_sets
    headers = _h264_parameter_sets(packets[0][0])
    def receive(_dev, _one, _two, _start, _end, destination, **kwargs):
        with NativePlaybackMuxer(destination) as muxer:
            write_events(muxer)

    def write_events(muxer):
        for index, (packet, keyframe) in enumerate(packets):
            stamp = 100000 + index * 100 + (60000 if fragment_reset and index >= 10 else 0)
            if separate_headers and keyframe:
                muxer.handle_event(media(headers, "h264", stamp))
            event = media(packet, "h264", stamp)
            event["payload"]["info"]["isKeyFrame"] = keyframe
            muxer.handle_event(event)
            muxer.handle_event(media(audio[index * 800 * width:(index + 1) * 800 * width], audio_codec, stamp, audio=True))
        if separate_headers:
            muxer.handle_event(media(headers, "h264", stamp + 100))
    if via_sync:
        from types import SimpleNamespace
        from custom_components.tuya_recordings import client as module
        from custom_components.tuya_recordings.lib.commands import CameraCommandQueue
        monkeypatch.setattr(module, "CAMERA_COMMANDS", CameraCommandQueue())
        client = module.TuyaRecordingsClient({"media_sync_enabled": True}, media_storage_path=tmp_path / "library")
        monkeypatch.setattr(client, "_backend", SimpleNamespace(clip_playback_available=True, receive_clip=receive))
        monkeypatch.setattr(client, "_clip_playback_metadata", lambda *args: {})
        monkeypatch.setattr(client, "refresh_recent_recordings", lambda: {
            "cameras": [{"devId": "camera", "clips": [{"start": 100, "end": 102}]}],
        })
        monkeypatch.setattr(client, "cleanup_cached_media", lambda *args: {})
        result = client.sync_recordings()
        assert result["downloaded"] == 1 and result["failed"] == 0
        assert client._media_failures == {}
        output = client.clip_path("camera", 100, 102)
        assert client.sync_recordings()["skipped"] == 1
    else:
        with NativePlaybackMuxer(output) as muxer:
            write_events(muxer)
        final = tmp_path / "final.mp4"
        finalize_mp4_for_browser(output, final)
        output = final
    with av.open(str(output)) as result:
        assert hashes(result) == expected
    with av.open(str(output)) as result:
        data = [packet for packet in result.demux() if packet.size]
        video = [packet for packet in data if packet.stream.type == "video"]
        assert all(a.dts < b.dts for a, b in zip(video, video[1:]))
        assert any(packet.pts != packet.dts for packet in video)
        assert b"".join(bytes(packet) for packet in data if packet.stream.type == "audio") == audio
        assert result.streams.video[0].codec_context.name == "h264"
        assert result.streams.audio[0].codec_context.name == ("pcm_mulaw" if width == 1 else "pcm_s16le")
    with av.open(str(output)) as result:
        times = [float(frame.pts * frame.time_base) for frame in result.decode(video=0)]
        assert times == pytest.approx([i / 10 for i in range(20)], abs=0.001)
    with av.open(str(output)) as result:
        audio_frames = list(result.decode(audio=0))
        assert sum(frame.samples for frame in audio_frames) == 16000
        assert float(audio_frames[0].pts * audio_frames[0].time_base) == pytest.approx(0, abs=0.001)
        assert any(bytes(audio_frames[0].planes[0]))
    with av.open(str(output)) as result:
        result.seek(1_000_000)
        seeked = hashes(result)
        assert seeked[-10:] == expected[-10:]


@pytest.mark.parametrize("codec,expected,width", [
    ("pcm_s16le", "pcm_s16le", 2), ("g711u", "pcm_mulaw", 1), ("g711a", "pcm_alaw", 1),
])
@pytest.mark.parametrize("repeated_audio_clock", [False, True])
def test_cache_preserves_raw_audio_and_uneven_video_clock(tmp_path, encoded_video, codec, expected, width, repeated_audio_clock):
    audio = (struct.pack("<hhhh", 0, 8192, -8192, 16384) * 2000 if width == 2
             else bytes([0x80, 0, 0xAA, 0x2A]) * 2000)
    events = [(100 + stamp, packet, False) for stamp, packet in
              zip([0, 180, 410, 600, 840], encoded_video, strict=True)]
    stride = 160 * width
    events.extend(((offset // stride // 2 * 40 if repeated_audio_clock else offset // stride * 20), audio[offset:offset + stride], True)
                  for offset in range(0, len(audio), stride))
    output = tmp_path / "clip.mp4"
    epoch = 1_790_000_000_000
    with NativePlaybackMuxer(output) as muxer:
        for stamp, payload, is_audio in sorted(events, key=lambda row: row[0]):
            muxer.handle_event(media(payload, codec if is_audio else "h264", epoch + stamp, audio=is_audio))
    with av.open(str(output)) as result:
        assert result.streams.audio[0].codec_context.name == expected
        assert result.streams.video[0].codec_context.name == "h264"
        packets = [packet for packet in result.demux() if packet.size]
        times = [float(packet.pts * packet.time_base) for packet in packets if packet.stream.type == "video"]
        assert times == pytest.approx([0.1, 0.28, 0.51, 0.7, 0.94], abs=0.001)
        assert b"".join(bytes(packet) for packet in packets if packet.stream.type == "audio") == audio
    # The final client fast-start step must preserve these codecs too.
    final = tmp_path / "final.mp4"
    finalize_mp4_for_browser(output, final)
    with av.open(str(final)) as result:
        assert result.streams.audio[0].codec_context.name == expected
        final_times = [float(packet.pts * packet.time_base) for packet in result.demux(video=0) if packet.size]
        assert final_times == pytest.approx([0.1, 0.28, 0.51, 0.7, 0.94], abs=0.001)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--autoplay-policy=no-user-gesture-required"])
        try:
            page = browser.new_page()
            page.set_content('<video controls></video>')
            page.evaluate("""async data => {
                const video = document.querySelector('video');
                video.src = `data:video/mp4;base64,${data}`;
                await video.play();
            }""", base64.b64encode(final.read_bytes()).decode())
            page.wait_for_function("document.querySelector('video').currentTime > 0.2", timeout=5000)
            assert page.locator("video").evaluate("v => v.videoWidth") == 160
            samples = page.evaluate("""async data => {
                const context = new AudioContext({sampleRate: 8000});
                try {
                    const raw = Uint8Array.from(atob(data), c => c.charCodeAt(0));
                    const audio = await context.decodeAudioData(raw.buffer);
                    return Array.from(audio.getChannelData(0).slice(0, 100));
                } finally { await context.close(); }
            }""", base64.b64encode(final.read_bytes()).decode())
            assert max(abs(sample) for sample in samples) > 0.1
        finally:
            browser.close()
    assert not list(tmp_path.glob("*.packets.tmp"))


def test_cache_rejects_invalid_camera_clock_and_cleans_up(tmp_path, encoded_video):
    output = tmp_path / "invalid.mp4"
    with pytest.raises(NativeMuxError, match="invalid camera timing"):
        with NativePlaybackMuxer(output, require_audio=False) as muxer:
            muxer.handle_event(media(encoded_video[0], "h264", 200))
            event = media(encoded_video[1], "h264", 100)
            event["payload"]["info"]["timestampScale"] = 0
            muxer.handle_event(event)
    assert not output.exists()
    assert not list(tmp_path.glob("*.tmp*"))


def test_cache_bounds_header_only_packets_and_cleans_up(tmp_path, monkeypatch):
    from custom_components.tuya_recordings.lib import native_mux
    monkeypatch.setattr(native_mux, "MAX_PACKET_BYTES", 16)
    output = tmp_path / "headers.mp4"
    with pytest.raises(NativeMuxError, match="exceed packet limit"):
        with NativePlaybackMuxer(output) as muxer:
            for _ in range(4):
                muxer.handle_event(media(b"\x00\x00\x01\x67\x01", "h264", 100))
    assert not output.exists()
    assert not list(tmp_path.glob("*.tmp*"))
    assert muxer._pending_video_headers == b""


@pytest.mark.parametrize("nal_type", [0, 1, 5, 13, 20, 31, 0x87])
def test_cache_does_not_classify_pictures_or_invalid_nals_as_headers(nal_type):
    from custom_components.tuya_recordings.lib.native_mux import _h264_metadata_only
    assert not _h264_metadata_only(b"\x00\x00\x01" + bytes([nal_type]) + b"data")


def test_cache_does_not_count_configuration_as_received_video(tmp_path):
    output = tmp_path / "no-pictures.mp4"
    with pytest.raises(NativeMuxError, match="no video packets"):
        with NativePlaybackMuxer(output, require_audio=False) as muxer:
            muxer.handle_event(media(b"\x00\x00\x01\x67\x01", "h264", 100))
    assert not output.exists()
    assert not list(tmp_path.glob("*.h264"))


@pytest.mark.parametrize("repeated_audio_clock", [False, True])
def test_cache_preserves_aac_packets_without_encoding(tmp_path, encoded_video, repeated_audio_clock):
    audio_path = tmp_path / "source.aac"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "sine=frequency=500:sample_rate=8000:duration=1", "-c:a", "aac", "-y", str(audio_path)],
        check=True, timeout=15)
    with av.open(str(audio_path), format="aac") as audio:
        packets = [bytes(packet) for packet in audio.demux(audio=0) if packet.size]
    events = [(index * 200, packet, False) for index, packet in enumerate(encoded_video)]
    events.extend(((index // 2 * 256 if repeated_audio_clock else index * 128), packet, True)
                  for index, packet in enumerate(packets))
    output = tmp_path / "aac.mp4"
    with NativePlaybackMuxer(output) as muxer:
        for stamp, payload, is_audio in sorted(events, key=lambda row: row[0]):
            muxer.handle_event(media(payload, "aac" if is_audio else "h264", stamp, audio=is_audio))
    with av.open(str(output)) as result:
        assert result.streams.audio[0].codec_context.name == "aac"
        raw_packets = [bytes(packet) for packet in result.demux(audio=0) if packet.size]
    assert raw_packets == [packet[7:] for packet in packets]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            peak = page.evaluate("""async data => {
                const context = new AudioContext({sampleRate:8000});
                try {
                    const raw = Uint8Array.from(atob(data), value => value.charCodeAt(0));
                    const decoded = await context.decodeAudioData(raw.buffer);
                    return Math.max(...decoded.getChannelData(0).map(Math.abs));
                } finally { await context.close(); }
            }""", base64.b64encode(output.read_bytes()).decode())
            assert peak > 0.02
        finally:
            browser.close()


def test_live_packet_mux_preserves_uneven_camera_clock(encoded_video):
    chunks = []
    with NativePlaybackBrowserStreamMuxer(chunks.append) as muxer:
        muxer.handle_event(playback_frame_event(NativePlaybackFrame(NativePlaybackFrameType.STARTED, b"", {}), protocol=1))
        for stamp, packet in zip([100, 280, 510, 700, 940], encoded_video, strict=True):
            muxer.handle_event(media(packet, "h264", 1_790_000_000_000 + stamp))
        muxer.handle_event(media(b"\x00\x00" * 8000, "pcm_s16le", 1_790_000_000_100, audio=True))
        assert muxer._proc is None
    video_bytes = b"".join(chunk[13:] for chunk in chunks[1:] if chunk[0] == 1)
    with av.open(io.BytesIO(video_bytes), format="mp4") as output:
        packets = [packet for packet in output.demux(video=0) if packet.size]
        assert [float(packet.pts * packet.time_base) for packet in packets] == pytest.approx(
            [0, 0.18, 0.41, 0.6, 0.84], abs=0.001)


@pytest.mark.parametrize("jump", [69_000, 3_377_270.4, -50_000, -(2**32) + 2000])
@pytest.mark.parametrize("audio_first", [False, True])
def test_live_fragment_transitions_keep_video_and_audio_on_one_clock(encoded_video, jump, audio_first):
    chunks = []
    epoch = (2**32 - 1000) if jump < -1_000_000 else 1_790_000_000_000
    with NativePlaybackBrowserStreamMuxer(chunks.append) as muxer:
        muxer.handle_event(playback_frame_event(NativePlaybackFrame(NativePlaybackFrameType.STARTED, b"", {}), protocol=1))
        for fragment in range(2):
            events = [(stamp, False, packet) for stamp, packet in zip([100, 280, 510, 700, 940], encoded_video, strict=True)]
            events.extend((100 + index * 20, True, b"\x80" * 160) for index in range(50))
            for stamp, audio, packet in sorted(events, key=lambda row: (row[0], row[1] != audio_first)):
                event = media(packet, "g711u" if audio else "h264", 0, audio=audio)
                event["payload"]["info"].update({
                    "timestamp": round((epoch + fragment * jump + stamp) * 1000),
                    "timestampScale": 1_000_000,
                })
                muxer.handle_event(event)
    video_bytes = b"".join(chunk[13:] for chunk in chunks if chunk[0] == 1)
    with av.open(io.BytesIO(video_bytes), format="mp4") as output:
        packets = [packet for packet in output.demux(video=0) if packet.size]
        times = [float(packet.pts * packet.time_base) for packet in packets]
        durations = [float(packet.duration * packet.time_base) for packet in packets]
        assert len(packets) == 10
        assert all(0 < right - left < 1 for left, right in zip(times, times[1:]))
        assert all(0 < duration < 1 for duration in durations)
        assert len(list(av.open(io.BytesIO(video_bytes), format="mp4").decode(video=0))) == 10
    audio = [(struct.unpack_from(">Q", chunk, 1)[0], chunk[13:]) for chunk in chunks if chunk[0] == 4]
    assert b"".join(payload for _, payload in audio) == b"\x80" * 16000
    audio_times = [stamp / 1_000_000 for stamp, _ in audio]
    assert all(0 < right - left < 1 for left, right in zip(audio_times, audio_times[1:]))
    first_video = next(struct.unpack_from(">Q", chunk, 1)[0] / 1_000_000 for chunk in chunks if chunk[0] == 1)
    # Shared epoch offsets preserve A/V alignment at the new recording.
    assert abs((audio_times[50] - first_video) - times[5]) < 0.025


@pytest.mark.parametrize("jump", [69000, -50000, 3_377_270.4])
def test_cached_fragments_keep_packets_and_audio_video_alignment(tmp_path, encoded_video, jump):
    output = tmp_path / "fragments.mp4"
    with NativePlaybackMuxer(output) as muxer:
        for fragment in range(2):
            events = [(stamp, False, packet) for stamp, packet in zip([100, 280, 510, 700, 940], encoded_video, strict=True)]
            events.extend((100 + index * 20, True, b"\x80" * 160) for index in range(50))
            for stamp, audio, packet in sorted(events, key=lambda row: row[0]):
                event = media(packet, "g711u" if audio else "h264", 0, audio=audio)
                event["payload"]["info"].update(
                    timestamp=round((1_790_000_000_000 + fragment * jump + stamp) * 1000), timestampScale=1_000_000,
                )
                muxer.handle_event(event)
    with av.open(str(output)) as result:
        packets = [packet for packet in result.demux() if packet.size]
        video = [packet for packet in packets if packet.stream.type == "video"]
        audio = [packet for packet in packets if packet.stream.type == "audio"]
        assert len(video) == 10
        assert b"".join(bytes(packet) for packet in audio) == b"\x80" * 16000
        assert all(0 < float(packet.duration * packet.time_base) < 1 for packet in video)
        consumed = 0
        for packet in audio:
            if consumed <= 8000 < consumed + packet.size:
                boundary = float(packet.pts * packet.time_base) + (8000 - consumed) / 8000
                break
            consumed += packet.size
        assert abs(boundary - float(video[5].pts * video[5].time_base)) < 0.025


def test_repeated_interleaved_fragment_tails_decode_video_and_preserve_audio(encoded_video):
    chunks = []
    fragments = 20
    epoch = 1_790_000_000_000
    with NativePlaybackBrowserStreamMuxer(chunks.append) as muxer:
        muxer.handle_event(playback_frame_event(NativePlaybackFrame(NativePlaybackFrameType.STARTED, b"", {}), protocol=1))
        previous_base = None
        for fragment in range(fragments):
            base = epoch + fragment * 69000
            muxer.handle_event(media(b"\x80" * 160, "g711u", base, audio=True))
            if previous_base is not None:
                muxer.handle_event(media(encoded_video[-1], "h264", previous_base + 100))
            for index, packet in enumerate(encoded_video[:-1]):
                muxer.handle_event(media(packet, "h264", base + index * 20))
                muxer.handle_event(media(b"\x80" * 160, "g711u", base + (index + 1) * 20, audio=True))
            previous_base = base
        muxer.handle_event(media(encoded_video[-1], "h264", previous_base + 100))
    video = b"".join(chunk[13:] for chunk in chunks if chunk[0] == 1)
    with av.open(io.BytesIO(video), format="mp4") as output:
        assert len(list(output.decode(video=0))) == fragments * 5
    with av.open(io.BytesIO(video), format="mp4") as output:
        packets = [packet for packet in output.demux(video=0) if packet.size]
        times = [float(packet.pts * packet.time_base) for packet in packets]
        assert all(0 < right - left < 0.2 for left, right in zip(times, times[1:]))
    audio = [(struct.unpack_from(">Q", chunk, 1)[0] / 1000, chunk[13:]) for chunk in chunks if chunk[0] == 4]
    assert b"".join(data for _, data in audio) == b"\x80" * 160 * 5 * fragments
    first_video = next(struct.unpack_from(">Q", chunk, 1)[0] / 1000 for chunk in chunks if chunk[0] == 1)
    for fragment in range(fragments):
        assert abs(audio[fragment * 5][0] - first_video - times[fragment * 5]) < 0.1


@pytest.mark.parametrize("jump", [69000, -50000, 3_377_270.4])
def test_g711_browser_keeps_playing_across_fragment_clock_reset(tmp_path, encoded_video, jump):
    audio_path = tmp_path / "source.mulaw"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=500:sample_rate=8000:duration=4", "-c:a", "pcm_mulaw",
                    "-f", "mulaw", "-y", str(audio_path)], check=True, timeout=15)
    audio = audio_path.read_bytes()
    events = [(index * 200, False, packet) for index, packet in enumerate(encoded_video * 4)]
    events.extend((index // 160 * 20, True, audio[index:index + 160]) for index in range(0, len(audio), 160))
    chunks = []
    with NativePlaybackBrowserStreamMuxer(chunks.append, stream_header=browser_stream_header(1, 100)) as muxer:
        muxer.handle_event(playback_frame_event(NativePlaybackFrame(NativePlaybackFrameType.STARTED, b"", {}), protocol=1))
        for stamp, is_audio, data in sorted(events, key=lambda row: row[0]):
            event = media(data, "g711u" if is_audio else "h264", 0, audio=is_audio)
            event["payload"]["info"].update(
                timestamp=round((1_790_000_000_000 + stamp + (jump if stamp >= 2000 else 0)) * 1000),
                timestampScale=1_000_000,
            )
            muxer.handle_event(event)
    server = native_stream_server(b"".join(chunks))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--autoplay-policy=no-user-gesture-required"])
            try:
                page = browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                page.evaluate("""async () => {
                    window.framesRendered = 0; window.latePeak = 0; window.sources = new Set();
                    window.cameraPositions = [];
                    window.WebSocket = class extends EventTarget {
                        static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
                        constructor() {
                            super(); this.readyState = 0;
                            setTimeout(async () => {
                                this.readyState = 1; this.dispatchEvent(new Event('open'));
                                const data = await (await fetch('/stream')).arrayBuffer();
                                if (this.readyState === 1) this.dispatchEvent(new MessageEvent('message', {data}));
                            }, 0);
                        }
                        send() {}
                        close() { this.readyState = 3; this.dispatchEvent(new Event('close')); }
                    };
                    const video = document.querySelector('video');
                    const count = () => { framesRendered++; video.requestVideoFrameCallback(count); };
                    video.requestVideoFrameCallback(count);
                    const gain = AudioContext.prototype.createGain;
                    AudioContext.prototype.createGain = function() {
                        const node = gain.call(this);
                        window.analyser = this.createAnalyser(); node.connect(analyser);
                        return node;
                    };
                    const create = AudioContext.prototype.createBufferSource;
                    AudioContext.prototype.createBufferSource = function() {
                        const node = create.call(this), start = node.start.bind(node), stop = node.stop.bind(node);
                        node.start = (...args) => { sources.add(node); start(...args); };
                        node.stop = (...args) => { sources.delete(node); stop(...args); };
                        node.addEventListener('ended', () => sources.delete(node), {once:true});
                        return node;
                    };
                    const monitor = () => {
                        if (window.stopMonitor) return;
                        if (window.analyser && video.currentTime > 2.5) {
                            const samples = new Float32Array(analyser.fftSize);
                            analyser.getFloatTimeDomainData(samples);
                            latePeak = Math.max(latePeak, ...samples.map(Math.abs));
                        }
                        requestAnimationFrame(monitor);
                    }; monitor();
                    const {attachCameraPlayer} = await import('/frontend/camera-player.js');
                    window.disposePlayer = attachCameraPlayer(video,
                        {cached:false, duration:4, start:100, end:104, playback_url:'/timeline/stream', stream_transport:'native'},
                        async path => path, {autoplay:true, mode:'timeline', onPosition: value => cameraPositions.push({position:value, time:video.currentTime})});
                }""")
                page.wait_for_function("framesRendered >= 12 && latePeak > 0.01 && document.querySelector('video').currentTime > 2.6", timeout=10000)
                assert not page.locator("video").evaluate("video => Boolean(video.error)")
                last = page.evaluate("cameraPositions.at(-1)")
                assert last["time"] > 2.2
                assert last["position"] - last["time"] == pytest.approx(100 + max(0, jump / 1000), abs=0.025)
                assert not errors
                page.evaluate("disposePlayer(); window.stopMonitor = true")
                assert page.evaluate("sources.size") == 0
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize("jump", [0, 69000, -50000, 3_377_270.4])
@pytest.mark.parametrize("rate,channels,audio_offset", [(8000, 1, 0), (16000, 2, 240)])
def test_aac_live_player_decodes_audio_seeks_and_releases(tmp_path, encoded_video, rate, channels, audio_offset, jump):
    audio_path = tmp_path / "live.aac"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    f"sine=frequency=500:sample_rate={rate}:duration=4",
                    "-ac", str(channels), "-c:a", "aac", "-y", str(audio_path)], check=True, timeout=15)
    with av.open(str(audio_path), format="aac") as source:
        audio = [bytes(packet) for packet in source.demux(audio=0) if packet.size]
    chunks = []
    events = [(i * 200, packet, False) for i, packet in enumerate(encoded_video * 4)]
    events.extend((audio_offset + i * 1024 * 1000 // rate, packet, True) for i, packet in enumerate(audio))
    with NativePlaybackBrowserStreamMuxer(chunks.append) as muxer:
        muxer.handle_event(playback_frame_event(NativePlaybackFrame(
            NativePlaybackFrameType.STARTED, b"", {}), protocol=1))
        for stamp, packet, is_audio in sorted(events, key=lambda item: item[0]):
            event = media(packet, "aac" if is_audio else "h264", 0, audio=is_audio)
            event["payload"]["info"].update(
                timestamp=round((1_790_000_000_000 + stamp + (jump if stamp >= 2000 else 0)) * 1000),
                timestampScale=1_000_000,
            )
            if is_audio:
                event["payload"]["info"].update(sampleRate=rate, channels=channels)
            muxer.handle_event(event)
        assert muxer._proc is None
    stream = b"".join(chunks)
    # Verify the AAC payloads survived the native remux byte-for-byte.
    aac_bytes = b"".join(chunk[13:] for chunk in chunks[1:] if chunk[0] == 7)
    (tmp_path / "live-audio.mp4").write_bytes(aac_bytes)
    with av.open(io.BytesIO(aac_bytes), format="mp4") as source:
        assert [bytes(packet) for packet in source.demux(audio=0) if packet.size] == [packet[7:] for packet in audio]
    server = native_stream_server(stream)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--autoplay-policy=no-user-gesture-required"])
            try:
                page = browser.new_page()
                media_errors = []
                cdp = page.context.new_cdp_session(page)
                cdp.send("Media.enable")
                cdp.on("Media.playerErrorsRaised", lambda event: media_errors.append(event))
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                page.evaluate("""async () => {
                    window.audios = [];
                    window.peak = 0;
                    window.audioFrequency = 0;
                    const original = AudioContext.prototype.createMediaElementSource;
                    AudioContext.prototype.createMediaElementSource = function(audio) {
                        audios.push(audio);
                        const node = original.call(this, audio);
                        const analyser = this.createAnalyser();
                        node.connect(analyser);
                        const samples = new Float32Array(analyser.fftSize);
                        const frequencies = new Float32Array(analyser.frequencyBinCount);
                        const monitor = () => {
                            if (this.state === 'closed') return;
                            analyser.getFloatTimeDomainData(samples);
                            window.peak = Math.max(window.peak, ...samples.map(Math.abs));
                            analyser.getFloatFrequencyData(frequencies);
                            let strongest = 1;
                            for (let index = 2; index < frequencies.length; index++) {
                                if (frequencies[index] > frequencies[strongest]) strongest = index;
                            }
                            window.audioFrequency = strongest * this.sampleRate / analyser.fftSize;
                            requestAnimationFrame(monitor);
                        };
                        monitor();
                        return node;
                    };
                    const {attachCameraPlayer} = await import('/frontend/camera-player.js');
                    window.clip = {cached:false, duration:4, start:100, end:104,
                        playback_url:'/stream', stream_transport:'native'};
                    window.playerDebug = [];
                    window.disposePlayer = attachCameraPlayer(document.querySelector('video'),
                        clip, async path => path, {autoplay:true, debug: detail => playerDebug.push(detail)});
                }""")
                try:
                    page.wait_for_function("window.peak > 0.02 && Math.abs(audioFrequency - 500) < 60 && document.querySelector('video').currentTime > 2.5", timeout=10000)
                except Exception:
                    print(page.evaluate("""() => ({peak, audioFrequency, playerDebug,
                        status: document.querySelector('[role=status]').textContent,
                        audio: audios.map(a => ({time:a.currentTime, paused:a.paused, ready:a.readyState, error:a.error?.message})),
                        video: {time:document.querySelector('video').currentTime, ready:document.querySelector('video').readyState}
                    })"""))
                    print(media_errors)
                    raise
                assert page.evaluate("(offset) => Math.abs(audios.at(-1).currentTime + offset - document.querySelector('video').currentTime)", audio_offset / 1000) < 0.3
                page.evaluate("""() => {
                    const video = document.querySelector('video');
                    Object.defineProperty(video, 'readyState', {configurable:true, value:1});
                    video.dispatchEvent(new Event('waiting'));
                }""")
                assert page.evaluate("audios.at(-1).paused")
                page.evaluate("""() => {
                    const video = document.querySelector('video');
                    delete video.readyState;
                    video.dispatchEvent(new Event('playing'));
                }""")
                page.wait_for_function("!audios.at(-1).paused", timeout=2000)
                page.evaluate("disposePlayer.pause()")
                assert page.evaluate("audios.every(audio => audio.paused)")
                page.evaluate("disposePlayer.playClip(clip, 1)")
                page.wait_for_function("audios.length === 2 && audios[1].currentTime > 0.15 && !audios[1].paused", timeout=10000)
                assert page.evaluate("audios[0].paused && !audios[0].getAttribute('src')")
                page.evaluate("disposePlayer()")
                assert page.evaluate("audios.every(audio => audio.paused && !audio.getAttribute('src'))")
                assert not errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize("packet", [b"", b"\xff\xf1", b"\xff\xf1" + b"\x00" * 5])
def test_live_aac_rejects_invalid_transport_headers(packet):
    with pytest.raises(ValueError, match="Native AAC"):
        TimestampedAacMuxer(packet, lambda data: pytest.fail("invalid AAC produced output"))
