"""Exercise the real panel with local media fixtures, never a camera."""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "custom_components" / "tuya_recordings" / "frontend"


def create_fixtures(directory: Path) -> tuple[bytes, bytes]:
    video_path = directory / "fixture.mp4"
    thumbnail_path = directory / "fixture.jpg"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=640x360:rate=15:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-movflags", "+faststart", "-shortest", "-y", str(video_path),
        ],
        check=True,
        timeout=30,
    )
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i",
            str(video_path), "-frames:v", "1", "-y", str(thumbnail_path),
        ],
        check=True,
        timeout=30,
    )
    return video_path.read_bytes(), thumbnail_path.read_bytes()


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        video, thumbnail = create_fixtures(Path(temporary))

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path == "/":
                    body, content_type = b"<html><body></body></html>", "text/html"
                elif path == "/video.mp4":
                    body, content_type = video, "video/mp4"
                elif path == "/thumbnail.jpg":
                    body, content_type = thumbnail, "image/jpeg"
                elif path.startswith("/tuya_recordings_static/"):
                    source = (
                        FRONTEND / path.removeprefix("/tuya_recordings_static/")
                    ).resolve()
                    if not source.is_relative_to(FRONTEND) or not source.is_file():
                        self.send_error(404)
                        return
                    body = source.read_bytes()
                    content_type = {
                        ".js": "text/javascript", ".css": "text/css",
                        ".svg": "image/svg+xml",
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
        reports: list[dict[str, object]] = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    args=["--autoplay-policy=no-user-gesture-required"],
                )
                for width in (1280, 390, 320):
                    page = browser.new_page(viewport={"width": width, "height": 900})
                    errors: list[str] = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(f"http://127.0.0.1:{server.server_port}/")
                    report = page.evaluate(
                        """async ({base}) => {
                          document.body.style.cssText = 'margin:0;--primary-text-color:#202327;--secondary-text-color:#62666e;--primary-background-color:#f7f8fa;--card-background-color:#fff;--secondary-background-color:#eef0f3;--divider-color:#dce0e5;--error-color:#b42318';
                          await import('/tuya_recordings_static/panel.js');
                          const panel = document.createElement('tuya-recordings-panel');
                          document.body.append(panel);
                          const services = [];
                          let apiCalls = 0;
                          const states = {'switch.tuya_recordings_pause_camera_activity': {state: 'off'}};
                          panel._hass = {
                            locale: {language: 'en', time_format: '12'}, states,
                            callApi: async () => { apiCalls += 1; return panel.data; },
                            callService: async (domain, service, data) => {
                              services.push({domain, service, data});
                              states['switch.tuya_recordings_pause_camera_activity'].state = service === 'turn_on' ? 'on' : 'off';
                            },
                            connection: {sendMessagePromise: async message => ({path: message.path})},
                          };
                          panel.selectedCamera = 'fixture';
                          panel.selectedDate = '2026-09-12';
                          panel.data = {on_demand: false, stats: {cache_only: true}, cameras: [{
                            dev_id: 'fixture', name: 'Front garden', online: true,
                            playback_mode: 'clips', recording_ranges: [],
                            dates: ['2026-09-12'], clips: [{
                              dev_id: 'fixture', date: '2026-09-12', start: 1789253240,
                              end: 1789253243, duration: 3, cached: true, thumbnail_cached: true,
                              playback_url: `${base}/video.mp4`, signed_playback_url: `${base}/video.mp4`,
                              thumbnail_cached: true, thumbnail_url: `${base}/thumbnail.jpg`, signed_thumbnail_url: `${base}/thumbnail.jpg`,
                            }],
                          }]};
                          panel.render();
                          await panel.refreshIdleData();
                          const idleRefreshCalls = apiCalls;
                          panel.selectedClip = panel.data.cameras[0].clips[0];
                          await panel.refreshIdleData();
                          const playbackRefreshCalls = apiCalls;
                          panel.selectedClip = null;
                          panel.data.on_demand = true;
                          await panel.refreshIdleData();
                          const onDemandRefreshCalls = apiCalls;
                          panel.data.on_demand = false;
                          const root = panel.shadowRoot;
                          const pauseBefore = root.querySelector('#pause-toggle').textContent.trim();
                          root.querySelector('#pause-toggle').click();
                          await new Promise(resolve => setTimeout(resolve, 0));
                          const pauseAfter = root.querySelector('#pause-toggle').textContent.trim();
                          root.querySelector('.clip').click();
                          return {pauseBefore, pauseAfter, services, idleRefreshCalls, playbackRefreshCalls, onDemandRefreshCalls};
                        }""",
                        {"base": f"http://127.0.0.1:{server.server_port}"},
                    )
                    page.wait_for_function(
                        "document.querySelector('tuya-recordings-panel').shadowRoot.querySelector('video')?.currentTime > 0.1",
                        timeout=10_000,
                    )
                    geometry = page.evaluate(
                        """() => {
                          const root = document.querySelector('tuya-recordings-panel').shadowRoot;
                          const clip = root.querySelector('.clip[playing]');
                          const thumb = clip.querySelector('.thumb').getBoundingClientRect();
                          const meta = clip.querySelector('.meta').getBoundingClientRect();
                          const controls = root.querySelector('.plyr__controls').getBoundingClientRect();
                          const status = root.querySelector('.player-status');
                          const beforeStatus = thumb.height;
                          status.textContent = 'Waiting for video';
                          const afterStatus = clip.querySelector('.thumb').getBoundingClientRect().height;
                          return {
                            controlsRight: controls.right, metaTop: meta.top,
                            thumbBottom: thumb.bottom,
                            buttons: [...root.querySelectorAll('.plyr__controls button')].map(button => button.getBoundingClientRect().height),
                            fullscreen: Boolean(root.querySelector('[data-plyr="fullscreen"]')),
                            volume: Boolean(root.querySelector('[data-plyr="volume"]')),
                            statusPosition: getComputedStyle(status).position,
                            beforeStatus,
                            afterStatus,
                          };
                        }"""
                    )
                    assert not errors
                    assert report["pauseBefore"] == "Pause"
                    assert report["pauseAfter"] == "Resume"
                    assert report["services"][0]["service"] == "turn_on"
                    assert report["idleRefreshCalls"] == 1
                    assert report["playbackRefreshCalls"] == 1
                    assert report["onDemandRefreshCalls"] == 1
                    assert geometry["controlsRight"] <= width + 1
                    assert all(height >= 40 for height in geometry["buttons"])
                    assert geometry["fullscreen"] and geometry["volume"]
                    assert geometry["statusPosition"] == "absolute"
                    assert abs(geometry["beforeStatus"] - geometry["afterStatus"]) < 0.1
                    if width < 900:
                        assert geometry["metaTop"] >= geometry["thumbBottom"] - 1
                    reports.append({"width": width, **geometry})
                    page.close()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    print(json.dumps(reports, sort_keys=True))


if __name__ == "__main__":
    main()
