"""Exercise the real browser player with a local TRS2 stream, never a camera."""

from __future__ import annotations

import json
import base64
import struct
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "custom_components" / "tuya_recordings" / "frontend"
HEADER = struct.Struct(">BQI")


def fragmented_h264(path: Path) -> bytes:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=640x360:rate=15:duration=3",
            "-an",
            "-c:v",
            "libx264",
            "-profile:v",
            "baseline",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            "-f",
            "mp4",
            "-y",
            str(path),
        ],
        check=True,
        timeout=30,
    )
    return path.read_bytes()


def record(kind: int, timestamp: int, payload: bytes = b"") -> bytes:
    return HEADER.pack(kind, timestamp, len(payload)) + payload


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        media = fragmented_h264(Path(temporary) / "fixture.mp4")
        pcm_s16le = struct.pack("<hhhh", 0, 8192, -8192, 16384) * 3_000
        audio_format = struct.pack(">IB", 16000, 1)
        stream = (
            b"TRS2"
            + record(5, 0, audio_format)
            + record(2, 89000, pcm_s16le[:8000])
            + record(1, 90000, media)
            + record(2, 90000, pcm_s16le)
            + record(3, 0)
        )
        timeline_stream = stream[: -len(record(3, 0))]
        requests: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path == "/":
                    body = b"<html><body><div id='player'><video playsinline></video><div role='status'></div></div></body></html>"
                    content_type = "text/html"
                elif path == "/stream":
                    requests.append(self.path)
                    body = stream
                    content_type = "application/vnd.tuya-recordings.stream"
                elif path.startswith("/frontend/"):
                    source = (FRONTEND / path.removeprefix("/frontend/")).resolve()
                    if not source.is_relative_to(FRONTEND) or not source.is_file():
                        self.send_error(404)
                        return
                    body = source.read_bytes()
                    content_type = {
                        ".js": "text/javascript",
                        ".svg": "image/svg+xml",
                        ".css": "text/css",
                    }.get(source.suffix, "application/octet-stream")
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if path == "/stream":
                    time.sleep(0.5)
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    args=["--autoplay-policy=no-user-gesture-required"],
                )
                page = browser.new_page(viewport={"width": 390, "height": 844})
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                result = page.evaluate(
                    """async () => {
                      window.audioStarts = 0;
                      window.firstAudioSamples = null;
                      window.audioSampleRates = [];
                      window.audioChannelCounts = [];
                      const originalStart = AudioBufferSourceNode.prototype.start;
                      AudioBufferSourceNode.prototype.start = function(...args) {
                        window.audioStarts += 1;
                        window.audioSampleRates.push(this.buffer.sampleRate);
                        window.audioChannelCounts.push(this.buffer.numberOfChannels);
                        if (!window.firstAudioSamples) {
                          window.firstAudioSamples = Array.from(this.buffer.getChannelData(0).slice(0, 4));
                        }
                        return originalStart.apply(this, args);
                      };
                      const { attachCameraPlayer } = await import('/frontend/camera-player.js');
                      const video = document.querySelector('video');
                      window.playerDebug = [];
                      window.disposePlayer = attachCameraPlayer(video, {
                        cached: false,
                        duration: 3,
                        start: 100,
                        end: 103,
                        playback_url: '/stream',
                        stream_transport: 'native',
                      }, async path => path, { autoplay: true, debug: event => window.playerDebug.push(event) });
                      return true;
                    }"""
                )
                assert result is True
                page.wait_for_function(
                    "document.querySelector('.plyr')?.classList.contains('plyr--loading')",
                    timeout=2_000,
                )
                loading_seen = True
                switching_seen = page.evaluate(
                    "document.querySelector('video').parentElement.classList.contains('camera-player--switching')"
                )
                try:
                    page.wait_for_function(
                        "document.querySelector('video').currentTime > 0.2 && window.audioStarts > 0",
                        timeout=15_000,
                    )
                except Exception:
                    print(page.evaluate("""() => ({
                      debug: window.playerDebug,
                      status: document.querySelector('[role=status]').textContent,
                      time: document.querySelector('video').currentTime,
                      readyState: document.querySelector('video').readyState,
                      networkState: document.querySelector('video').networkState,
                      audioStarts: window.audioStarts,
                    })"""))
                    raise
                page.evaluate(
                    """async () => {
                      const video = document.querySelector('video');
                      window.initialPlayer = video.plyr;
                      window.disposePlayer.playClip({
                        cached: false,
                        duration: 3,
                        start: 200,
                        end: 203,
                        playback_url: '/stream',
                        stream_transport: 'native',
                      }, 0);
                    }"""
                )
                page.wait_for_function(
                    "document.querySelector('video').currentTime > 0.2 && window.audioStarts > 1",
                    timeout=15_000,
                )
                report = page.evaluate(
                    """() => {
                      const video = document.querySelector('video');
                      return {
                        time: video.currentTime,
                        paused: video.paused,
                        audioStarts: window.audioStarts,
                        firstAudioSamples: window.firstAudioSamples,
                        audioSampleRates: window.audioSampleRates,
                        audioChannelCounts: window.audioChannelCounts,
                        status: document.querySelector('[role=status]').textContent,
                        plyr: Boolean(video.plyr),
                        samePlayer: video.plyr === window.initialPlayer,
                        controls: document.querySelectorAll('.plyr__controls button').length,
                        loading: document.querySelector('.plyr').classList.contains('plyr--loading'),
                        switching: video.parentElement.classList.contains('camera-player--switching'),
                        playingEvents: window.playerDebug.filter(event => event.phase === 'playing').length,
                      };
                    }"""
                )
                page.evaluate(
                    """async () => {
                      window.disposePlayer();
                      const { attachCameraPlayer } = await import('/frontend/camera-player.js');
                      const video = document.querySelector('video');
                      window.disposePlayer = attachCameraPlayer(video, {
                        cached: false,
                        duration: 0.4,
                        start: 300,
                        end: 301,
                        playback_url: '/stream',
                        stream_transport: 'native',
                      }, async path => path, { autoplay: true });
                    }"""
                )
                page.wait_for_function(
                    "document.querySelector('[role=status]').textContent === 'Finished'",
                    timeout=15_000,
                )
                ended = page.evaluate(
                    """() => ({
                      status: document.querySelector('[role=status]').textContent,
                      hasSource: Boolean(document.querySelector('video').getAttribute('src')),
                    })"""
                )
                page.evaluate(
                    """async encodedStream => {
                      const bytes = Uint8Array.from(atob(encodedStream), value => value.charCodeAt(0));
                      window.timelineSockets = [];
                      window.timelineActions = [];
                      window.WebSocket = class FakeTimelineSocket extends EventTarget {
                        static OPEN = 1;
                        constructor(url) {
                          super();
                          this.url = url;
                          this.readyState = 0;
                          window.timelineSockets.push(this);
                          queueMicrotask(() => {
                            this.readyState = FakeTimelineSocket.OPEN;
                            this.dispatchEvent(new Event('open'));
                            this.dispatchEvent(new MessageEvent('message', { data: bytes.buffer.slice(0) }));
                          });
                        }
                        send(payload) {
                          const command = JSON.parse(payload);
                          window.timelineActions.push(command);
                          if (command.action === 'seek') {
                            queueMicrotask(() => this.dispatchEvent(new MessageEvent('message', { data: bytes.buffer.slice(0) })));
                          }
                        }
                        close() {
                          if (this.readyState !== 3) {
                            this.readyState = 3;
                            this.dispatchEvent(new CloseEvent('close'));
                          }
                        }
                      };
                      window.disposePlayer();
                      window.timelinePosition = 0;
                      window.finalSignCalls = 0;
                      const { attachCameraPlayer } = await import('/frontend/camera-player.js');
                      const video = document.querySelector('video');
                      window.disposePlayer = attachCameraPlayer(video, {
                        cached: false,
                        duration: 0.4,
                        start: 400,
                        end: 401,
                        playback_url: '/timeline/camera',
                        stream_transport: 'native',
                      }, async path => { window.finalSignCalls += 1; return path; }, {
                        autoplay: true,
                        mode: 'timeline',
                        onPosition: position => { window.timelinePosition = position; },
                      });
                      window.timelinePlayer = video.plyr;
                    }""",
                    base64.b64encode(timeline_stream).decode(),
                )
                try:
                    page.wait_for_function(
                        "window.timelinePosition > 400.8",
                        timeout=15_000,
                    )
                except Exception:
                    print(page.evaluate("""() => ({
                      debug: window.playerDebug,
                      status: document.querySelector('[role=status]').textContent,
                      time: document.querySelector('video').currentTime,
                      readyState: document.querySelector('video').readyState,
                      networkState: document.querySelector('video').networkState,
                      position: window.timelinePosition,
                      socketCount: window.timelineSockets.length,
                      actions: window.timelineActions,
                    })"""))
                    raise
                timeline = page.evaluate(
                    """() => ({
                      status: document.querySelector('[role=status]').textContent,
                      hasSource: Boolean(document.querySelector('video').getAttribute('src')),
                      position: window.timelinePosition,
                      progress: Boolean(document.querySelector('[data-plyr="seek"]')),
                    })"""
                )
                page.evaluate(
                    """() => {
                      const clip = {
                        cached: false,
                        duration: 3,
                        start: 500,
                        end: 503,
                        playback_url: '/timeline',
                        stream_transport: 'native',
                      };
                      window.disposePlayer.playClip(clip, 0);
                      window.disposePlayer.playClip(clip, 1);
                      window.disposePlayer.playClip(clip, 2);
                    }"""
                )
                page.wait_for_function(
                    "window.timelineActions.some(item => item.action === 'seek' && item.position === 502) && window.audioStarts > 3",
                    timeout=15_000,
                )
                timeline.update(page.evaluate(
                    """() => ({
                      socketCount: window.timelineSockets.length,
                      actions: window.timelineActions,
                      samePlayerAfterSeek: document.querySelector('video').plyr === window.timelinePlayer,
                    })"""
                ))
                stale_restart = page.evaluate(
                    """async () => {
                      const video = document.querySelector('video');
                      const plyr = video.plyr;
                      const originalDestroy = plyr.destroy.bind(plyr);
                      plyr.destroy = () => {
                        video.removeAttribute('src');
                        video.dispatchEvent(new Event('play'));
                        originalDestroy();
                      };
                      const before = window.finalSignCalls;
                      window.disposePlayer();
                      await new Promise(resolve => setTimeout(resolve, 100));
                      return window.finalSignCalls - before;
                    }"""
                )
                page.screenshot(path=str(Path(temporary) / "native-player.png"))
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    assert requests == [
        "/stream?position=100&transport=native",
        "/stream?position=200&transport=native",
        "/stream?position=300&transport=native",
    ]
    assert not errors
    assert report["plyr"] and report["controls"] >= 3
    assert report["samePlayer"] is True
    assert loading_seen is True
    assert switching_seen is True
    assert report["loading"] is False
    assert report["switching"] is False
    assert report["audioStarts"] == 2
    assert report["playingEvents"] == 2
    assert report["firstAudioSamples"] == [0, 0.25, -0.25, 0.5]
    assert report["audioSampleRates"] == [16000, 16000]
    assert report["audioChannelCounts"] == [1, 1]
    assert report["time"] > 0.2
    assert not report["paused"]
    assert report["status"] == ""
    assert ended == {"status": "Finished", "hasSource": False}
    assert timeline["position"] > 400.8
    assert timeline["status"] != "Finished"
    assert timeline["hasSource"] is True
    assert timeline["progress"] is False
    assert timeline["socketCount"] == 1
    assert timeline["samePlayerAfterSeek"] is True
    assert [item for item in timeline["actions"] if item["action"] == "seek"] == [
        {"action": "seek", "position": 502}
    ]
    assert stale_restart == 0
    print(json.dumps({"requests": requests, **report}, sort_keys=True))


if __name__ == "__main__":
    main()
