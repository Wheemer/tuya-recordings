"""Play one captured TRS2 camera stream through the real browser player."""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "custom_components" / "tuya_recordings" / "frontend"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stream", type=Path)
    args = parser.parse_args()
    stream = args.stream.read_bytes()
    if not stream.startswith(b"TRS2"):
        raise RuntimeError("Captured browser stream has no TRS2 header")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/":
                body = b"<html><body><div id='player'><video playsinline></video><div role='status'></div></div></body></html>"
                content_type = "text/html"
            elif path == "/stream":
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
            page.evaluate(
                """async () => {
                  window.audioStarts = 0;
                  window.playerDebug = [];
                  const originalStart = AudioBufferSourceNode.prototype.start;
                  AudioBufferSourceNode.prototype.start = function(...args) {
                    window.audioStarts += 1;
                    return originalStart.apply(this, args);
                  };
                  const { attachCameraPlayer } = await import('/frontend/camera-player.js');
                  const video = document.querySelector('video');
                  window.disposePlayer = attachCameraPlayer(video, {
                    cached: false,
                    duration: 120,
                    start: 100,
                    end: 220,
                    playback_url: '/stream',
                    stream_transport: 'native',
                  }, async path => path, {
                    autoplay: true,
                    debug: detail => window.playerDebug.push(detail),
                  });
                }"""
            )
            timed_out = False
            try:
                page.wait_for_function(
                    "document.querySelector('video').currentTime > 0.2 && window.audioStarts > 0",
                    timeout=20_000,
                )
            except Exception:
                timed_out = True
            report = page.evaluate(
                """() => ({
                  time: document.querySelector('video').currentTime,
                  paused: document.querySelector('video').paused,
                  readyState: document.querySelector('video').readyState,
                  networkState: document.querySelector('video').networkState,
                  videoError: document.querySelector('video').error?.message || null,
                  audioStarts: window.audioStarts,
                  status: document.querySelector('[role=status]').textContent,
                  debug: window.playerDebug,
                })"""
            )
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    print(json.dumps({"timedOut": timed_out, "pageErrors": errors, **report}, sort_keys=True))
    assert not timed_out
    assert not errors, errors
    assert report["time"] > 0.2
    assert report["audioStarts"] > 0
    assert not report["paused"]


if __name__ == "__main__":
    main()
