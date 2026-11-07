"""Exercise the real browser player with local TRS3 streams, never a camera."""

from __future__ import annotations

import json
import base64
import struct
import subprocess
import tempfile
import threading
import time
import av
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

from custom_components.tuya_recordings.lib.native_frames import (
    NATIVE_PLAYBACK_EVENT_VERSION,
    NativePlaybackFrame,
    NativePlaybackFrameType,
    playback_frame_event,
)
from custom_components.tuya_recordings.lib.native_mux import (
    BROWSER_STREAM_END,
    NativePlaybackBrowserStreamMuxer,
    browser_stream_header,
)


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "custom_components" / "tuya_recordings" / "frontend"
HEADER = struct.Struct(">BQI")


def native_browser_stream(path: Path) -> bytes:
    """Build the same fMP4 record stream the native camera path emits."""
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
            "-g",
            "15",
            "-f",
            "h264",
            "-y",
            str(path),
        ],
        check=True,
        timeout=30,
    )
    def event(frame_type, payload=b"", info=None):
        return playback_frame_event(
            NativePlaybackFrame(frame_type, payload, info or {}),
            protocol=NATIVE_PLAYBACK_EVENT_VERSION,
        )

    chunks: list[bytes] = []
    # Keep native audio alive beyond the deliberate browser-stall regression
    # below. A short fixture can end naturally before the 1.5-second stall
    # detector runs, which would hide a broken audio-stop path.
    pcm_s16le = struct.pack("<hhhh", 0, 8192, -8192, 16384) * 16_000
    with NativePlaybackBrowserStreamMuxer(chunks.append) as muxer:
        muxer.handle_event(event(NativePlaybackFrameType.STARTED))
        with av.open(str(path), format="h264") as source:
            packets = [bytes(packet) for packet in source.demux(video=0) if packet.size]
        for index, packet in enumerate(packets):
            muxer.handle_event(event(
                NativePlaybackFrameType.MEDIA_CODEC,
                packet,
                {
                "avChannel": 0,
                "frameNo": index + 1,
                "codecId": 27,
                "codecName": "h264",
                "isKeyFrame": index % 15 == 0,
                "timestamp": 90000 + index * 6000,
                "timestampScale": 90000,
                "frameInfoBase64": "",
                },
            ))
        muxer.handle_event(event(
            NativePlaybackFrameType.MEDIA_CODEC,
            pcm_s16le,
            {
                "avChannel": 1,
                "frameNo": 2,
                "codecId": 0xFFFE,
                "codecName": "pcm_s16le",
                "isKeyFrame": False,
                "timestamp": 90000,
                "timestampScale": 90000,
                "sampleRate": 16000,
                "channels": 1,
                "frameInfoBase64": "",
            },
        ))
    return b"".join(chunks)

def record(kind: int, timestamp: int, payload: bytes = b"") -> bytes:
    return HEADER.pack(kind, timestamp, len(payload)) + payload


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        stream = native_browser_stream(Path(temporary) / "fixture.h264")
        # Deliver enough fragments to start Chromium, then withhold the rest
        # until the test has observed a genuine buffered-media starvation.
        stall_resume = threading.Event()
        stall_split = len(browser_stream_header())
        video_records = 0
        while video_records < 30:
            kind, _timestamp, size = HEADER.unpack_from(stream, stall_split)
            stall_split += HEADER.size + size
            video_records += kind == 1
        record_bytes = stream[len(browser_stream_header()) : -len(record(BROWSER_STREAM_END, 0))]
        timeline_streams = {
            "initial": browser_stream_header(1, 400) + record_bytes,
            # This arrives after the final seek was accepted. It must be
            # ignored wholesale, including its audio records.
            "stale": (
                browser_stream_header(1, 501)
                # If stale records reach the parser, this would fail the test.
                + record(5, 0, b"\\0")
                + record_bytes
            ),
            "fresh": browser_stream_header(2, 502) + record_bytes,
            "later": browser_stream_header(3, 501) + record_bytes,
            "rapid_old": browser_stream_header(4, 500) + record_bytes,
            "rapid_final": browser_stream_header(5, 502) + record_bytes,
            "paused_seek": browser_stream_header(6, 501) + record_bytes,
        }
        requests: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path == "/":
                    body = b"<html><body><div id='player'><video playsinline></video><div role='status'></div></div></body></html>"
                    content_type = "text/html"
                elif path in ("/stream", "/stallstream"):
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
                if path == "/stallstream":
                    self.wfile.write(body[:stall_split])
                    self.wfile.flush()
                    if not stall_resume.wait(12):
                        return
                    self.wfile.write(body[stall_split:])
                else:
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
                listener_cleanup = page.evaluate(
                    """async () => {
                      const text = await (await fetch('/frontend/camera-player.js')).text();
                      const helpers = new Function(text.slice(text.indexOf('function once('),
                        text.indexOf('function concatBytes(')) + ';return {once, appendBuffer};')();
                      const tracked = target => {
                        const listeners = new Map();
                        const add = target.addEventListener.bind(target);
                        const remove = target.removeEventListener.bind(target);
                        target.addEventListener = (type, fn, ...args) => {
                          if (!listeners.has(type)) listeners.set(type, new Set());
                          listeners.get(type).add(fn);
                          add(type, fn, ...args);
                        };
                        target.removeEventListener = (type, fn, ...args) => {
                          listeners.get(type)?.delete(fn);
                          remove(type, fn, ...args);
                        };
                        target.count = () => [...listeners.values()].reduce((n, set) => n + set.size, 0);
                        return target;
                      };
                      const controller = new AbortController();
                      const signal = tracked(controller.signal);
                      const buffer = tracked(new EventTarget());
                      buffer.appendBuffer = () => queueMicrotask(() => buffer.dispatchEvent(new Event('updateend')));
                      const counts = [];
                      for (let i = 0; i < 100; i++) {
                        await helpers.appendBuffer(buffer, new Uint8Array(1), signal);
                        const opened = helpers.once(buffer, 'sourceopen', signal);
                        buffer.dispatchEvent(new Event('sourceopen'));
                        await opened;
                        counts.push(buffer.count() + signal.count());
                      }
                      buffer.appendBuffer = () => { throw new Error('fixture sync error'); };
                      await helpers.appendBuffer(buffer, new Uint8Array(1), signal).catch(() => {});
                      counts.push(buffer.count() + signal.count());
                      buffer.appendBuffer = () => queueMicrotask(() => buffer.dispatchEvent(new Event('error')));
                      await helpers.appendBuffer(buffer, new Uint8Array(1), signal).catch(() => {});
                      counts.push(buffer.count() + signal.count());
                      buffer.appendBuffer = () => {};
                      const pending = helpers.appendBuffer(buffer, new Uint8Array(1), signal);
                      controller.abort();
                      const abortName = await pending.catch(error => error.name);
                      counts.push(buffer.count() + signal.count());
                      const alreadyAborted = await helpers.appendBuffer(buffer, new Uint8Array(1), signal).catch(error => error.name);
                      return {maxRetained: Math.max(...counts), abortName, alreadyAborted};
                    }"""
                )
                assert listener_cleanup == {
                    "maxRetained": 0, "abortName": "AbortError", "alreadyAborted": "AbortError",
                }, listener_cleanup
                result = page.evaluate(
                    """async () => {
                      window.audioStarts = 0;
                      window.audioStops = 0;
                      window.audioSuspends = 0;
                      window.audioResumes = 0;
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
                      const originalStop = AudioBufferSourceNode.prototype.stop;
                      AudioBufferSourceNode.prototype.stop = function(...args) {
                        window.audioStops += 1;
                        return originalStop.apply(this, args);
                      };
                      const originalSuspend = AudioContext.prototype.suspend;
                      AudioContext.prototype.suspend = function(...args) {
                        window.audioSuspends += 1;
                        return originalSuspend.apply(this, args);
                      };
                      const originalResume = AudioContext.prototype.resume;
                      AudioContext.prototype.resume = function(...args) {
                        window.audioResumes += 1;
                        return originalResume.apply(this, args);
                      };
                      const { attachCameraPlayer } = await import('/frontend/camera-player.js');
                      const video = document.querySelector('video');
                      window.videoFrameCallbacks = 0;
                      if (typeof video.requestVideoFrameCallback === 'function') {
                        const nativeRequestVideoFrameCallback = video.requestVideoFrameCallback.bind(video);
                        video.requestVideoFrameCallback = callback => {
                          window.videoFrameCallbacks += 1;
                          return nativeRequestVideoFrameCallback(callback);
                        };
                      }
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
                        videoFrameCallbacks: window.videoFrameCallbacks,
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
                        duration: 2.5,
                        start: 300,
                        end: 303,
                        playback_url: '/stallstream',
                        stream_transport: 'native',
                      }, async path => path, { autoplay: true });
                    }"""
                )
                page.wait_for_function(
                    "document.querySelector('video').currentTime > 0.2",
                    timeout=5_000,
                )
                # Seek the actual element into the withheld portion: Chromium
                # must wait for those bytes, without a mocked media clock.
                page.evaluate("document.querySelector('video').currentTime = 2")
                page.wait_for_function(
                    """() => document.querySelector('video').currentTime > 0.2
                      && document.querySelector('video').readyState <= 2
                      && !document.querySelector('video').paused""",
                    timeout=5_000,
                )
                finite_stall = page.evaluate(
                    """async () => {
                      const video = document.querySelector('video');
                      const before = video.currentTime;
                      await new Promise(resolve => setTimeout(resolve, 2800));
                      const retained = Boolean(video.getAttribute('src'));
                      const status = document.querySelector('[role=status]').textContent;
                      const after = video.currentTime;
                      return {retained, status, before, after};
                    }"""
                )
                assert finite_stall["retained"], finite_stall
                assert finite_stall["status"] != "Finished", finite_stall
                assert abs(finite_stall["after"] - finite_stall["before"]) < 0.05
                stall_resume.set()
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
                    """async encodedStreams => {
                      const decode = encoded => Uint8Array.from(atob(encoded), value => value.charCodeAt(0));
                      const initial = decode(encodedStreams.initial);
                      const stale = decode(encodedStreams.stale);
                      const fresh = decode(encodedStreams.fresh);
                      const later = decode(encodedStreams.later);
                      const rapidOld = decode(encodedStreams.rapid_old);
                      const rapidFinal = decode(encodedStreams.rapid_final);
                      const pausedSeek = decode(encodedStreams.paused_seek);
                      window.timelineResponses = [
                        {data: fresh, delay: 20},
                        {data: later, delay: 20},
                        // The camera can finish the first seek after a newer
                        // scrub position was already requested.
                        {data: rapidOld, delay: 200},
                        {data: rapidFinal, delay: 20},
                        {data: pausedSeek, delay: 20},
                      ];
                      window.timelineSockets = [];
                      window.timelineActions = [];
                      window.timelineDebug = [];
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
                            // WebSocket framing is arbitrary: prove the
                            // browser preserves a split segment header.
                            this.dispatchEvent(new MessageEvent('message', { data: initial.buffer.slice(0, 7) }));
                            setTimeout(() => this.dispatchEvent(new MessageEvent('message', {
                              data: initial.buffer.slice(7),
                            })), 1);
                          });
                        }
                        send(payload) {
                          const command = JSON.parse(payload);
                          window.timelineActions.push(command);
                          if (command.action === 'seek') {
                            queueMicrotask(() => {
                              this.dispatchEvent(new MessageEvent('message', {
                                data: JSON.stringify({ type: 'seeking', position: command.position }),
                              }));
                              // A seek can have one old native segment queued.
                              // It must not touch the fresh media pipeline.
                              setTimeout(() => this.dispatchEvent(new MessageEvent('message', {
                                data: stale.buffer.slice(0),
                              })), 5);
                              const response = window.timelineResponses.shift();
                              if (response) {
                                setTimeout(() => this.dispatchEvent(new MessageEvent('message', {
                                  data: response.data.buffer.slice(0),
                                })), response.delay);
                              }
                            });
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
                        debug: event => window.timelineDebug.push(event),
                      });
                      window.timelinePlayer = video.plyr;
                    }""",
                    {name: base64.b64encode(value).decode() for name, value in timeline_streams.items()},
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
                      // A detached MediaSource can deliver a late ended event
                      // after the seek has begun. It must not pause the fresh
                      // pipeline before its first video frame.
                      const seekVideo = document.querySelector('video');
                      Object.defineProperty(seekVideo, 'ended', { value: true, configurable: true });
                      seekVideo.dispatchEvent(new Event('ended'));
                      delete seekVideo.ended;
                    }"""
                )
                page.wait_for_function(
                    "window.timelineActions.some(item => item.action === 'seek' && item.position === 502) && window.audioStarts > 3",
                    timeout=15_000,
                )
                # A seek is not successful merely because the camera accepted
                # it: the rebuilt MediaSource must advance at the new epoch.
                page.wait_for_function(
                    "window.timelinePosition > 502.2 && document.querySelector('video').currentTime > 0.2",
                    timeout=15_000,
                )
                page.wait_for_function(
                    "document.querySelector('video').buffered.length && document.querySelector('video').buffered.start(0) < 0.1",
                    timeout=5_000,
                )
                page.evaluate(
                    """() => {
                      window.disposePlayer.playClip({
                        cached: false,
                        duration: 3,
                        start: 500,
                        end: 503,
                        playback_url: '/timeline',
                        stream_transport: 'native',
                      }, 1);
                    }"""
                )
                page.wait_for_function(
                    """() => window.timelineActions.at(-1)?.position === 501
                      && window.timelineDebug.some(event => event.phase === 'stream-header'
                        && event.generation === 3 && event.position === 501)
                      && document.querySelector('video').currentTime > 0.2""",
                    timeout=15_000,
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
                      // Let the first delayed request leave the debounce queue,
                      // then scrub again before its native segment arrives.
                      window.disposePlayer.playClip(clip, 0);
                      setTimeout(() => window.disposePlayer.playClip(clip, 2), 80);
                    }"""
                )
                page.wait_for_function(
                    """() => window.timelineActions.at(-1)?.position === 502
                      && window.timelineDebug.some(event => event.phase === 'segment-discarded'
                        && event.generation === 4 && event.position === 500)
                      && window.timelineDebug.some(event => event.phase === 'stream-header'
                        && event.generation === 5 && event.position === 502)
                      && document.querySelector('video').currentTime > 0.2""",
                    timeout=15_000,
                )
                timeline.update(page.evaluate(
                    """() => ({
                      socketCount: window.timelineSockets.length,
                      actions: window.timelineActions,
                      samePlayerAfterSeek: document.querySelector('video').plyr === window.timelinePlayer,
                      debug: window.timelineDebug,
                    })"""
                ))
                post_seek_error = page.evaluate(
                    """async () => {
                      const video = document.querySelector('video');
                      video.dispatchEvent(new Event('error'));
                      await new Promise(resolve => setTimeout(resolve, 50));
                      return {
                        source: Boolean(video.getAttribute('src')),
                        paused: video.paused,
                        status: document.querySelector('[role=status]').textContent,
                        ignored: window.timelineDebug.some(event => event.phase === 'stale-media-error-ignored'),
                      };
                    }"""
                )
                stalled_audio = page.evaluate(
                    """async () => {
                      const video = document.querySelector('video');
                      // Capture-phase suppression freezes the player's
                      // observed video clock without pausing the element.
                      // That models a decoder stall while a separately
                      // scheduled audio buffer still has data to play.
                      window.suppressProgress = event => event.stopImmediatePropagation();
                      window.addEventListener('timeupdate', window.suppressProgress, true);
                      Object.defineProperty(video, 'readyState', { value: 1, configurable: true });
                      const stopsBefore = window.audioStops;
                      const suspendsBefore = window.audioSuspends;
                      const resumesBefore = window.audioResumes;
                      video.dispatchEvent(new Event('waiting'));
                      await new Promise(resolve => setTimeout(resolve, 1700));
                      const stalledStatus = document.querySelector('[role=status]').textContent;
                      const stalledLoading = document.querySelector('.plyr').classList.contains('plyr--loading');
                      video.dispatchEvent(new Event('playing'));
                      await new Promise(resolve => setTimeout(resolve, 20));
                      return {
                        stalledStatus,
                        stalledLoading,
                        stops: window.audioStops - stopsBefore,
                        suspends: window.audioSuspends - suspendsBefore,
                        resumes: window.audioResumes - resumesBefore,
                        recoveredStatus: document.querySelector('[role=status]').textContent,
                        stalled: window.timelineDebug.some(event => event.phase === 'audio-stalled'),
                      };
                    }"""
                )
                page.evaluate("""() => {
                  window.removeEventListener('timeupdate', window.suppressProgress, true);
                  delete document.querySelector('video').readyState;
                }""")
                page.evaluate(
                    """() => {
                      window.disposePlayer.pause();
                      window.disposePlayer.playClip({
                        cached: false,
                        duration: 3,
                        start: 500,
                        end: 503,
                        playback_url: '/timeline',
                        stream_transport: 'native',
                      }, 1);
                    }"""
                )
                page.wait_for_function(
                    """() => {
                      const actions = window.timelineActions;
                      const seek = actions.findLastIndex(item => item.action === 'seek' && item.position === 501);
                      return seek >= 0 && actions[seek + 1]?.action === 'resume';
                    }""",
                    timeout=15_000,
                )
                paused_seek_actions = page.evaluate("window.timelineActions.slice(-3)")
                page.wait_for_function(
                    """() => window.timelineDebug.some(e => e.phase === 'stream-header' && e.generation === 6)
                      && document.querySelector('video').currentTime > 0.2
                      && !document.querySelector('video').paused""",
                    timeout=5_000,
                )
                plain_resume = page.evaluate(
                    """async () => {
                      const video = document.querySelector('video');
                      const before = video.plyr.currentTime;
                      const mediaBefore = video.currentTime;
                      const src = video.src;
                      const actionCount = window.timelineActions.length;
                      window.disposePlayer.pause();
                      const pausedPosition = video.plyr.currentTime;
                      await new Promise(resolve => setTimeout(resolve, 100));
                      video.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                      await new Promise(resolve => setTimeout(resolve, 250));
                      return {before, mediaBefore, pausedPosition,
                        after: video.plyr.currentTime, mediaAfter: video.currentTime,
                        sameSource: src === video.src,
                        samePlayer: video.plyr === window.timelinePlayer,
                        sockets: window.timelineSockets.length,
                        actions: window.timelineActions.slice(actionCount)};
                    }"""
                )
                assert abs(plain_resume["pausedPosition"] - plain_resume["before"]) < 0.05, plain_resume
                assert plain_resume["mediaAfter"] > plain_resume["mediaBefore"], plain_resume
                assert abs((plain_resume["after"] - plain_resume["mediaAfter"]) - 1) < 0.05, plain_resume
                assert plain_resume["sameSource"] and plain_resume["samePlayer"], plain_resume
                assert plain_resume["sockets"] == 1, plain_resume
                assert plain_resume["actions"] == [{"action": "pause"}, {"action": "resume"}], plain_resume
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
                      const audioStopsBefore = window.audioStops;
                      window.disposePlayer();
                      await new Promise(resolve => setTimeout(resolve, 100));
                      return {
                        unexpectedSignCalls: window.finalSignCalls - before,
                        audioStops: window.audioStops - audioStopsBefore,
                        socketClosed: window.timelineSockets.at(-1)?.readyState === 3,
                      };
                    }"""
                )
                page.screenshot(path=str(Path(temporary) / "native-player.png"))
                browser.close()
        finally:
            stall_resume.set()
            server.shutdown()
            server.server_close()
            thread.join()

    assert requests == [
        "/stream?position=100&transport=native",
        "/stream?position=200&transport=native",
        "/stallstream?position=300&transport=native",
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
    assert report["videoFrameCallbacks"] >= 2
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
        {"action": "seek", "position": 502},
        {"action": "seek", "position": 501},
        {"action": "seek", "position": 500},
        {"action": "seek", "position": 502},
    ]
    assert paused_seek_actions[-2:] == [
        {"action": "seek", "position": 501},
        {"action": "resume"},
    ]
    assert any(event["phase"] == "seek-requested" and event["position"] == 502 for event in timeline["debug"])
    assert any(event["phase"] == "segment-discarded" and event["position"] == 501 for event in timeline["debug"])
    assert any(event["phase"] == "stream-header" and event["generation"] == 2 and event["position"] == 502 for event in timeline["debug"])
    assert any(event["phase"] == "stream-header" and event["generation"] == 3 and event["position"] == 501 for event in timeline["debug"])
    assert any(event["phase"] == "segment-discarded" and event["generation"] == 4 and event["position"] == 500 for event in timeline["debug"])
    assert any(event["phase"] == "stream-header" and event["generation"] == 5 and event["position"] == 502 for event in timeline["debug"])
    assert post_seek_error == {"source": True, "paused": False, "status": "", "ignored": True}
    assert stalled_audio["stalledStatus"] == "Waiting for video"
    assert stalled_audio["stalledLoading"] is True
    assert stalled_audio["stops"] > 0
    assert stalled_audio["suspends"] == 1
    assert stalled_audio["resumes"] == 1
    assert stalled_audio["recoveredStatus"] == ""
    assert stalled_audio["stalled"] is True
    assert stale_restart["unexpectedSignCalls"] == 0, stale_restart
    assert stale_restart["audioStops"] > 0, stale_restart
    assert stale_restart["socketClosed"] is True, stale_restart
    print(json.dumps({
        "requests": requests, **report,
        "finiteStall": finite_stall,
        "listenerCleanup": listener_cleanup,
        "plainResume": plain_resume,
        "pausedSeekGeneration": 6,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
