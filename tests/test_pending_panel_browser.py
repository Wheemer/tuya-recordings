"""Real panel, production panel data and local files; never camera commands."""

import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import threading

import pytest
from playwright.sync_api import sync_playwright

from test_panel_data import FakeClient
from custom_components.tuya_recordings.http import build_panel_data


@pytest.mark.parametrize("width", [1280, 390])
def test_pending_date_survives_and_becomes_cached_playable_without_thumbnail(tmp_path, width):
    client = FakeClient(tmp_path)
    index = {"cameras": [{"devId": "doorbell", "name": "Wifi videodeurbel", "online": True,
                           "clips": [{"start": 100, "end": 130, "date": "2026-11-14"}]}]}
    state = {"data": build_panel_data(client, index, media_root=tmp_path)}
    frontend = Path(__file__).resolve().parents[1] / "custom_components/tuya_recordings/frontend"
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            requests.append(self.path)
            if path == "/":
                body, mime = b"<!doctype html><html><body></body></html>", "text/html"
            elif path == "/api/panel":
                body, mime = json.dumps(state["data"]).encode(), "application/json"
            elif path.startswith("/tuya_recordings_static/"):
                source = (frontend / path.removeprefix("/tuya_recordings_static/")).resolve()
                if not source.is_relative_to(frontend) or not source.is_file():
                    self.send_error(404)
                    return
                body = source.read_bytes()
                mime = mimetypes.guess_type(source)[0] or "application/octet-stream"
            elif path == "/api/tuya_recordings/play/doorbell/100/130":
                source = client.clip_path("doorbell", 100, 130)
                if not source.is_file():
                    self.send_error(404)
                    return
                body, mime = source.read_bytes(), "video/mp4"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    artifacts = Path(os.environ.get("TUYA_TEST_ARTIFACTS", str(tmp_path / "screenshots")))
    artifacts.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": width, "height": 900})
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                page.evaluate("""async () => {
                    document.documentElement.style.cssText = `
                      --primary-text-color:#212121; --secondary-text-color:#666;
                      --divider-color:#ddd; --card-background-color:#fff;
                      --secondary-background-color:#f2f2f2;
                      font-family:Arial,sans-serif; background:#fafafa;`;
                    await import('/tuya_recordings_static/panel.js');
                    window.panel = document.createElement('tuya-recordings-panel');
                    document.body.append(panel);
                    window.signs = [];
                    panel._hass = {
                      locale: {language: 'en', time_format: '12'}, states: {},
                      callApi: async (method) => method === 'GET'
                        ? await (await fetch('/api/panel')).json() : {},
                      callService: () => { throw new Error('Unexpected device command'); },
                      connection: {sendMessagePromise: async message => {
                        signs.push(message.path); return {path: message.path};
                      }},
                    };
                    await panel.loadData();
                }""")
                card = page.locator("tuya-recordings-panel .clip")
                assert card.count() == 1
                assert card.get_attribute("role") == "article"
                assert card.get_attribute("aria-disabled") == "true"
                assert "Awaiting download" in card.inner_text()
                assert card.locator(".play-mark").count() == 0
                assert card.locator(".missing-thumb ha-icon").get_attribute("icon") == "mdi:clock-outline"
                assert page.locator("tuya-recordings-panel #date").input_value() == "2026-11-14"
                assert "No recordings for this date" not in page.locator("tuya-recordings-panel").inner_text()
                awaitable = "async () => { await panel.openClip(panel.clips[0]); return panel.selectedClip; }"
                assert page.evaluate(awaitable) is None
                card.click()
                card.press("Enter")
                assert page.evaluate("signs.length") == 0
                assert not any("/play/" in path for path in requests)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(artifacts / f"pending-recording-{width}.png"), full_page=True)

                video = client.clip_path("doorbell", 100, 130)
                video.parent.mkdir(parents=True)
                subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=160x90:rate=10:duration=2", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=16000:duration=2", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart",
                    "-shortest", "-y", str(video)], check=True, timeout=20)
                state["data"] = build_panel_data(client, index, media_root=tmp_path)
                assert not state["data"]["cameras"][0]["clips"][0]["thumbnail_cached"]
                page.evaluate("async () => { await panel.loadData(); }")
                card = page.locator("tuya-recordings-panel .clip")
                assert card.get_attribute("role") == "button"
                assert "Saved locally" in card.inner_text()
                assert page.locator("tuya-recordings-panel #date").input_value() == "2026-11-14"
                card.click()
                page.wait_for_function("""() => {
                    const video = panel.shadowRoot.querySelector('video');
                    return video && video.videoWidth === 160 && video.currentTime > 0.05;
                }""", timeout=10000)
                assert any("/play/" in path and "transport=file" in path for path in requests)
                assert not any("transport=native" in path or "/timeline/" in path for path in requests)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(artifacts / f"pending-recording-ready-{width}.png"), full_page=True)
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
