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
from custom_components.tuya_recordings.lib.native_mux import NativePlaybackMuxer, NativePlaybackBrowserStreamMuxer, NativeMuxError
from custom_components.tuya_recordings.lib.ffmpeg import finalize_mp4_for_browser
from custom_components.tuya_recordings.lib.native_packet_cache import TimestampedVideoMuxer, TimestampedAacMuxer


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


@pytest.mark.parametrize("codec,expected,width", [
    ("pcm_s16le", "pcm_s16le", 2), ("g711u", "pcm_mulaw", 1), ("g711a", "pcm_alaw", 1),
])
def test_cache_preserves_raw_audio_and_uneven_video_clock(tmp_path, encoded_video, codec, expected, width):
    audio = (struct.pack("<hhhh", 0, 8192, -8192, 16384) * 2000 if width == 2
             else bytes([0x80, 0, 0xAA, 0x2A]) * 2000)
    events = [(100 + stamp, packet, False) for stamp, packet in
              zip([0, 180, 410, 600, 840], encoded_video, strict=True)]
    stride = 160 * width
    events.extend((offset // stride * 20, audio[offset:offset + stride], True)
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


def test_cache_rejects_reversed_camera_clock_and_cleans_up(tmp_path, encoded_video):
    output = tmp_path / "invalid.mp4"
    with pytest.raises(NativeMuxError, match="timestamps are not increasing"):
        with NativePlaybackMuxer(output, require_audio=False) as muxer:
            muxer.handle_event(media(encoded_video[0], "h264", 200))
            muxer.handle_event(media(encoded_video[1], "h264", 100))
    assert not output.exists()
    assert not list(tmp_path.glob("*.tmp*"))


def test_cache_preserves_aac_packets_without_encoding(tmp_path, encoded_video):
    audio_path = tmp_path / "source.aac"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "sine=frequency=500:sample_rate=8000:duration=1", "-c:a", "aac", "-y", str(audio_path)],
        check=True, timeout=15)
    with av.open(str(audio_path), format="aac") as audio:
        packets = [bytes(packet) for packet in audio.demux(audio=0) if packet.size]
    events = [(index * 200, packet, False) for index, packet in enumerate(encoded_video)]
    events.extend((index * 128, packet, True) for index, packet in enumerate(packets))
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


@pytest.mark.parametrize("rate,channels,audio_offset", [(8000, 1, 0), (16000, 2, 240)])
def test_aac_live_player_decodes_audio_seeks_and_releases(tmp_path, encoded_video, rate, channels, audio_offset):
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
            event = media(packet, "aac" if is_audio else "h264",
                          1_790_000_000_000 + stamp, audio=is_audio)
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

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
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
                    page.wait_for_function("window.peak > 0.02 && Math.abs(audioFrequency - 500) < 60 && document.querySelector('video').currentTime > 0.25", timeout=10000)
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
