"""Exercise signed cache delivery through HA auth, HTTP views and a browser."""

import asyncio
from datetime import date, timedelta
from pathlib import Path
import subprocess
import threading
import os
import shutil

from aiohttp import web
import av
from playwright.sync_api import sync_playwright
import pytest
import requests

from homeassistant.auth import auth_manager_from_config
from homeassistant.components.http.auth import async_setup_auth, async_sign_path, STORAGE_KEY
from homeassistant.components.http.const import KEY_HASS_REFRESH_TOKEN_ID
from homeassistant.core import HomeAssistant
from homeassistant.components.media_source import MediaSourceItem
from homeassistant.components.media_source.error import Unresolvable
from homeassistant.components.media_source.local_source import LocalSource, LocalMediaView
from homeassistant.config_entries import ConfigEntries
from homeassistant.helpers.http import HomeAssistantView, KEY_HASS
from homeassistant.helpers import device_registry, entity_registry

from custom_components.tuya_recordings.client import TuyaRecordingsClient
from custom_components.tuya_recordings.const import DOMAIN
from types import SimpleNamespace
from custom_components.tuya_recordings import _async_register_views
from custom_components.tuya_recordings.lib.ffmpeg import finalize_mp4_for_browser
from custom_components.tuya_recordings.lib.native_frames import (
    NativePlaybackFrame, NativePlaybackFrameType, playback_frame_event,
)
from custom_components.tuya_recordings.lib.native_mux import NativePlaybackMuxer
from custom_components.tuya_recordings.media_source import TuyaRecordingsMediaSource, build_identifier

FRONTEND = Path(__file__).resolve().parents[1] / "custom_components/tuya_recordings/frontend"


def make_cache(client, directory, codec):
    video_path = directory / "source.h264"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=160x90:rate=5:duration=3", "-pix_fmt", "yuv420p",
                    "-c:v", "libx264", "-bf", "0", "-y", str(video_path)],
                   check=True, timeout=20)
    with av.open(str(video_path), format="h264") as source:
        events = [(i * 200, bytes(packet), False)
                  for i, packet in enumerate(p for p in source.demux(video=0) if p.size)]
    if codec == "aac":
        audio_path = directory / "source.aac"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=500:sample_rate=8000:duration=3",
                        "-c:a", "aac", "-y", str(audio_path)], check=True, timeout=20)
        with av.open(str(audio_path), format="aac") as source:
            events.extend((i * 128, bytes(packet), True)
                          for i, packet in enumerate(p for p in source.demux(audio=0) if p.size))
    else:
        events.extend((i * 200, bytes([0x80, 0, 0xAA, 0x2A]) * 400, True) for i in range(15))
    output = client.clip_path("fixture", 100, 103)
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = directory / "muxed.mp4"
    with NativePlaybackMuxer(raw) as muxer:
        for timestamp, payload, audio in sorted(events):
            muxer.handle_event(playback_frame_event(NativePlaybackFrame(
                NativePlaybackFrameType.MEDIA_CODEC, payload, {
                    "avChannel": int(audio), "codecName": codec if audio else "h264",
                    "frameNo": timestamp, "codecId": (4 if codec == "aac" else 133) if audio else 1,
                    "timestamp": 100000 + timestamp, "timestampScale": 1000,
                    "sampleRate": 8000, "channels": 1, "isKeyFrame": not audio,
                }), protocol=1))
    finalize_mp4_for_browser(raw, output)
    thumb = client.thumbnail_path("fixture", 100, 103)
    thumb.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-frames:v", "1",
                    "-y", str(thumb)], check=True, timeout=20)


@pytest.mark.parametrize("codec", ["g711u", "aac"])
def test_signed_cache_thumbnails_video_audio_and_range_requests(tmp_path, codec, monkeypatch):
    client = TuyaRecordingsClient({"media_sync_enabled": True},
                                  media_storage_path=tmp_path / "custom-storage")
    make_cache(client, tmp_path, codec)
    for path, other in ((client.clip_path("fixture", 100, 103), client.clip_path("fixture", 200, 203)),
                        (client.thumbnail_path("fixture", 100, 103), client.thumbnail_path("fixture", 200, 203))):
        shutil.copyfile(path, other)
    for start, folder in ((300, client.media_storage_path / "thumbs"), (400, client.media_storage_path)):
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(client.clip_path("fixture", 100, 103), folder / f"fixture_{start}_{start + 3}.mp4")
        shutil.copyfile(client.thumbnail_path("fixture", 100, 103), client.thumbnail_path("fixture", start, start + 3))
    index = {"cameras": [{"devId": "fixture", "name": "Cached camera", "online": False,
                          "bridgeStatus": {"status": "not_responding"},
                          "clips": [{"start": start, "end": start + 3, "date": date.today().isoformat()}
                                    for start in (100, 200, 300, 400)]}]}
    monkeypatch.setattr(client, "cached_camera_index", lambda: index)
    camera_calls = []
    def forbidden(*args, **kwargs):
        camera_calls.append(True)
        raise AssertionError("Cached delivery must not contact the camera")
    for method in ("stream_clip", "stream_timeline", "browse_recordings", "refresh_inventory"):
        if hasattr(client, method):
            monkeypatch.setattr(client, method, forbidden)
    client._cloud_pause_event.set()
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    state = {}

    async def start():
        config = tmp_path / "ha-config"
        config.mkdir()
        hass = HomeAssistant(str(config))
        device_registry.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        await hass.config_entries.async_initialize()
        await device_registry.async_load(hass, load_empty=True)
        await entity_registry.async_load(hass, load_empty=True)
        hass.auth = await auth_manager_from_config(hass, [], [])
        hass.config.media_dirs = {"archive": str(tmp_path)}
        hass.data[DOMAIN] = {"fixture-entry": {"client": client}}
        app = web.Application()
        app[KEY_HASS] = hass
        await async_setup_auth(hass, app)
        refresh = hass.auth.async_get_refresh_token(hass.data[STORAGE_KEY])
        token = hass.auth.async_create_access_token(refresh)

        class SignView(HomeAssistantView):
            url = "/fixture/sign"
            name = "fixture:sign"

            async def post(self, request):
                data = await request.json()
                return web.json_response({"path": async_sign_path(
                    hass, data["path"], timedelta(seconds=3600),
                    refresh_token_id=request[KEY_HASS_REFRESH_TOKEN_ID],
                )})

        hass.http = SimpleNamespace(
            register_view=lambda view: view.register(hass, app, app.router),
            supervisor_unix_socket_path=None,
        )
        _async_register_views(hass)
        _async_register_views(hass)
        for view in (SignView(),
                     LocalMediaView(hass, LocalSource(hass, "local", "My media", hass.config.media_dirs, "/media"))):
            view.register(hass, app, app.router)
        async def home(request):
            return web.Response(text="<html><body></body></html>", content_type="text/html")
        app.router.add_get("/", home)
        app.router.add_static("/tuya_recordings_static/", FRONTEND)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        state.update(hass=hass, runner=runner, token=token,
                     provider=TuyaRecordingsMediaSource(hass),
                     base=f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}")

    def serve():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(start())
        except BaseException as error:
            state["error"] = error
            ready.set()
            return
        ready.set()
        loop.run_forever()
        loop.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(20), "HA fixture did not start"
    if "error" in state:
        raise state["error"]
    try:
        base, token = state["base"], state["token"]
        headers = {"Authorization": f"Bearer {token}"}
        response = requests.get(base + "/api/tuya_recordings/panel", headers=headers, timeout=5)
        assert response.status_code == 200, response.text
        clip = response.json()["cameras"][0]["clips"][0]
        item = MediaSourceItem(state["hass"], DOMAIN, build_identifier({
            "entry_id": "fixture-entry", "dev_id": "fixture", "start": "200", "end": "203",
        }), None)
        resolved = asyncio.run_coroutine_threadsafe(state["provider"].async_resolve_media(item), loop).result(5)
        assert resolved.url == clip["playback_url"]
        async def browse(identifier):
            return await state["provider"].async_browse_media(
                MediaSourceItem(state["hass"], DOMAIN, identifier, None))
        root = asyncio.run_coroutine_threadsafe(browse(""), loop).result(5)
        assert [child.title for child in root.children] == ["Cached camera"]
        camera = asyncio.run_coroutine_threadsafe(browse(root.children[0].identifier), loop).result(5)
        day = asyncio.run_coroutine_threadsafe(browse(camera.children[0].identifier), loop).result(5)
        assert len(day.children) == 2
        for child in day.children:
            assert child.can_play and not child.can_expand
            assert child.media_content_type == "video/mp4"
            leaf = asyncio.run_coroutine_threadsafe(browse(child.identifier), loop).result(5)
            assert leaf.can_play and leaf.media_content_type == "video/mp4"
            assert "/thumb/" in leaf.thumbnail
        for start in (300, 400):
            with pytest.raises(Unresolvable, match="not cached"):
                asyncio.run_coroutine_threadsafe(browse(build_identifier({
                    "dev_id": "fixture", "start": str(start), "end": str(start + 3),
                })), loop).result(5)
        for key in ("thumbnail_url", "playback_url"):
            assert clip[key].startswith("/api/tuya_recordings/")
            assert "entry_id=fixture-entry" in clip[key]
            assert requests.get(base + clip[key], timeout=5).status_code == 401
            signed = requests.post(base + "/fixture/sign", headers=headers,
                                   json={"path": clip[key]}, timeout=5).json()["path"]
            assert requests.get(base + signed, timeout=5).status_code == 200
            path = (client.thumbnail_path("fixture", 200, 203) if key == "thumbnail_url"
                    else client.clip_path("fixture", 200, 203))
            local_url = "/media/archive/" + path.relative_to(tmp_path).as_posix()
            local_signed = requests.post(base + "/fixture/sign", headers=headers,
                                         json={"path": local_url}, timeout=5).json()["path"]
            local_response = requests.get(base + local_signed, timeout=5)
            assert local_response.status_code == 200
            assert local_response.content == requests.get(base + signed, timeout=5).content
            wrong_local = local_url.replace("/media/archive/", "/media/local/")
            wrong_signed = requests.post(base + "/fixture/sign", headers=headers,
                                         json={"path": wrong_local}, timeout=5).json()["path"]
            assert requests.get(base + wrong_signed, timeout=5).status_code == 404
            head = requests.head(base + signed, timeout=5)
            assert head.status_code == 200 and head.content == b""
            assert int(head.headers["Content-Length"]) > 0
            tampered = signed.replace("fixture-entry", "wrong-account")
            assert requests.get(base + tampered, timeout=5).status_code == 401
            if key == "playback_url":
                partial = requests.get(base + signed, headers={"Range": "bytes=0-63"}, timeout=5)
                assert partial.status_code == 206
                assert len(partial.content) == 64
                assert partial.headers["Content-Range"].startswith("bytes 0-63/")
                assert partial.content == client.clip_path("fixture", 200, 203).read_bytes()[:64]
        native_path = clip["playback_url"].replace("transport=file", "transport=native")
        signed_native = requests.post(base + "/fixture/sign", headers=headers,
                                      json={"path": native_path}, timeout=5).json()["path"]
        assert requests.head(base + signed_native, timeout=5).status_code == 405
        for path in ("/api/tuya_recordings/play/fixture/900/903?transport=file&entry_id=fixture-entry",
                     "/api/tuya_recordings/thumb/fixture/900/903?entry_id=fixture-entry"):
            missing = requests.post(base + "/fixture/sign", headers=headers,
                                    json={"path": path}, timeout=5).json()["path"]
            assert requests.get(base + missing, timeout=5).status_code == 404
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for width in (1280, 390):
                    page = browser.new_page(viewport={"width": width, "height": 900})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(base)
                    page.evaluate("""async token => {
                      await import('/tuya_recordings_static/panel.js');
                      window.cachedEvents = [];
                      const request = async (method, path, data) => {
                        const response = await fetch(path, {method,
                          headers: {Authorization: 'Bearer ' + token, 'Content-Type': 'application/json'},
                          body: data ? JSON.stringify(data) : undefined});
                        if (!response.ok) throw new Error('Fixture HTTP ' + response.status);
                        return response.json();
                      };
                      const panel = document.createElement('tuya-recordings-panel');
                      document.body.append(panel);
                      panel.hass = {
                        locale: {language:'en', time_format:'12'}, states:{},
                        callApi: (method, path, data) => {
                          if (path === 'tuya_recordings/debug') cachedEvents.push(data);
                          return request(method, '/api/' + path, data);
                        },
                        connection: {sendMessagePromise: data => request('POST', '/fixture/sign', data)}
                      };
                    }""", token)
                    page.wait_for_function("""() => {
                      const image = document.querySelector('tuya-recordings-panel').shadowRoot.querySelector('.thumb img');
                      return image?.complete && image.naturalWidth > 0;
                    }""", timeout=10000)
                    page.locator("tuya-recordings-panel").locator(".clip").first.click()
                    page.wait_for_function("""() =>
                      document.querySelector('tuya-recordings-panel').shadowRoot.querySelector('video')?.currentTime > 0.2
                    """, timeout=10000)
                    result = page.evaluate("""async () => {
                      const root = document.querySelector('tuya-recordings-panel').shadowRoot;
                      const video = root.querySelector('video');
                      const response = await fetch(video.src);
                      const context = new AudioContext({sampleRate:8000});
                      try {
                        const decoded = await context.decodeAudioData(await response.arrayBuffer());
                        const samples = decoded.getChannelData(0);
                        return {width:video.videoWidth, frames:video.getVideoPlaybackQuality().totalVideoFrames,
                          peak:Math.max(...samples.slice(0,8000).map(Math.abs)),
                          playing:cachedEvents.some(event => event.detail?.phase === 'playing'),
                          stages:cachedEvents.map(event => event.detail?.phase || event.phase),
                          builds:[...new Set(cachedEvents.flatMap(event =>
                            [event.detail?.panel_build, event.detail?.player_build]).filter(Boolean))],
                          safeDebug:!JSON.stringify(cachedEvents).includes('authSig='),
                          plyr:Boolean(root.querySelector('.plyr__controls'))};
                      } finally { await context.close(); }
                    }""")
                    assert result["width"] == 160 and result["frames"] > 0, result
                    assert result["peak"] > 0.02 and result["playing"] and result["plyr"], result
                    assert {"initialize", "controls-create", "controls-ready", "cached-play-request",
                            "playing", "progress", "player-ready"}.issubset(result["stages"]), result
                    assert result["safeDebug"], result
                    assert {"20261114-camera-availability-37", "20261114-private-cache-36"}.issubset(
                        result["builds"]), result
                    if directory := os.environ.get("TUYA_TEST_ARTIFACT_DIR"):
                        artifacts = Path(directory)
                        artifacts.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(artifacts / f"{codec}-{width}.png"), full_page=True)
                    page.locator("tuya-recordings-panel").locator(".clip").nth(1).click()
                    page.wait_for_function("""() => {
                      const panel = document.querySelector('tuya-recordings-panel');
                      return panel.selectedClip.start === 100 && panel.shadowRoot.querySelector('video')?.currentTime > 0.2;
                    }""", timeout=10000)
                    page.evaluate("""() => {
                      document.querySelector('tuya-recordings-panel').shadowRoot.querySelector('video').currentTime = 1.25;
                    }""")
                    page.wait_for_function("""() =>
                      document.querySelector('tuya-recordings-panel').shadowRoot.querySelector('video')?.currentTime > 1.25
                    """, timeout=5000)
                    page.evaluate("""async () => {
                      const panel = document.querySelector('tuya-recordings-panel');
                      const clip = panel.data.cameras[0].clips[0];
                      clip.thumbnail_url = '/api/tuya_recordings/thumb/fixture/900/903?entry_id=fixture-entry';
                      await panel.signClip(clip, {playback:false, thumbnail:true});
                      panel.render();
                    }""")
                    page.wait_for_function("""() =>
                      cachedEvents.some(event => event.phase === 'thumbnail-error')
                    """, timeout=5000)
                    sign_error = page.evaluate("""async () => {
                      const panel = document.querySelector('tuya-recordings-panel');
                      panel.signedPaths.clear();
                      panel._hass.connection.sendMessagePromise = async () => ({});
                      await panel.openClip(panel.data.cameras[0].clips[0]);
                      return panel.error;
                    }""")
                    assert "did not return a signed media path" in sign_error
                    assert not errors, errors
                    page.close()
            finally:
                browser.close()
        client.media_sync_enabled = False
        for key in ("thumbnail_url", "playback_url"):
            assert requests.get(base + clip[key], headers=headers, timeout=5).status_code == 200
        assert not camera_calls
    finally:
        async def close():
            await state["runner"].cleanup()
            await state["hass"].async_stop(force=True)
        asyncio.run_coroutine_threadsafe(close(), loop).result(20)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        assert not thread.is_alive()
