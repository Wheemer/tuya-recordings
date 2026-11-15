"""Panel camera ordering must not change recording transport or Sync order."""

from pathlib import Path
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from playwright.sync_api import sync_playwright

from custom_components.tuya_recordings.client import TuyaRecordingsClient
from custom_components.tuya_recordings.http import build_panel_data
from custom_components import tuya_recordings as integration
from custom_components.tuya_recordings.const import DOMAIN


@pytest.mark.parametrize("sync", [False, True])
def test_preference_is_account_scoped_and_only_reorders_panel(sync, tmp_path):
    client = TuyaRecordingsClient({"camera_order": ["doorbell", "other", "ptz"], "media_sync_enabled": sync},
                                  media_storage_path=tmp_path)
    index = {"cameras": [{"devId": dev, "clips": []} for dev in ["ptz", "doorbell", "other"]]}
    data = build_panel_data(client, index)
    assert data["camera_order"] == ["doorbell", "other", "ptz"]
    assert [item["dev_id"] for item in data["cameras"]] == ["doorbell", "other", "ptz"]
    assert [item["devId"] for item in index["cameras"]] == ["ptz", "doorbell", "other"]
    other = TuyaRecordingsClient({}, media_storage_path=tmp_path / "other")
    assert build_panel_data(other, index)["camera_order"] == []
    client.update_options({"camera_order": ["removed", "other", "other"]})
    assert client.camera_order == ["removed", "other"]
    assert build_panel_data(client, index)["camera_order"] == ["other", "ptz", "doorbell"]
    client.update_options({"camera_order": ["removed"]})
    assert build_panel_data(client, index)["camera_order"] == []


def test_service_persists_full_order_without_other_option_or_account_changes(tmp_path, monkeypatch):
    import voluptuous as vol

    client = TuyaRecordingsClient({}, media_storage_path=tmp_path)
    client.set_camera_inventory({"a": "A", "b": "B", "c": "C"})
    other = TuyaRecordingsClient({}, media_storage_path=tmp_path / "other")
    entry = SimpleNamespace(entry_id="account", data={}, options={"media_retention_hours": 96})
    registered = {}
    def register(domain, name, handler, *, schema):
        registered[name] = (handler, schema)
    def update(target, *, options):
        assert target is entry
        target.options = options
    hass = SimpleNamespace(
        services=SimpleNamespace(has_service=lambda *args: False, async_register=register),
        config_entries=SimpleNamespace(async_update_entry=update),
        data={DOMAIN: {"account": {"entry": entry, "client": client},
                       "other": {"client": other}}},
    )
    monkeypatch.setattr(integration, "async_dispatcher_send", Mock())
    integration._async_register_services(hass)
    handler, schema = registered["set_camera_order"]
    params = {"entry_id": "account", "camera_order": ["c", "a", "b"]}
    asyncio.run(handler(SimpleNamespace(data=schema(params))))
    assert entry.options == {"media_retention_hours": 96, "camera_order": ["c", "a", "b"]}
    assert client.camera_order == ["c", "a", "b"] and other.camera_order == []
    restarted = TuyaRecordingsClient({**entry.data, **entry.options}, media_storage_path=tmp_path)
    assert restarted.camera_order == ["c", "a", "b"]
    with pytest.raises(vol.Invalid):
        schema({**params, "camera_order": ["a", "a"]})
    with pytest.raises(ValueError, match="not discovered"):
        asyncio.run(handler(SimpleNamespace(data=schema({**params, "camera_order": ["foreign"]}))))
    assert entry.options["camera_order"] == ["c", "a", "b"]
    asyncio.run(handler(SimpleNamespace(data=schema({**params, "camera_order": []}))))
    assert client.camera_order == [] and entry.options["camera_order"] == []


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("sync", [False, True])
def test_browser_default_manual_refresh_reopen_and_missing_camera(width, sync):
    frontend = Path(__file__).resolve().parents[1] / "custom_components/tuya_recordings/frontend"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 900})
            def serve(route):
                path = route.request.url.split("example.test", 1)[-1].split("?", 1)[0]
                if path == "/":
                    route.fulfill(body="<html><body></body></html>", content_type="text/html")
                    return
                source = (frontend / path.removeprefix("/tuya_recordings_static/")).resolve()
                assert source.is_relative_to(frontend) and source.is_file(), path
                route.fulfill(body=source.read_bytes(), content_type=(
                    "text/css" if source.suffix == ".css" else "application/javascript"))
            page.route("**/*", serve)
            page.goto("http://example.test/")
            page.evaluate("""async sync => {
              document.documentElement.style.cssText = '--primary-text-color:#212121;--secondary-text-color:#666;--divider-color:#ddd;--card-background-color:#fff;--secondary-background-color:#f2f2f2;font-family:Arial,sans-serif';
              await import('/tuya_recordings_static/panel.js');
              window.calls = [];
              window.payload = {
                entry_id:'account', camera_order:['doorbell','other','ptz'], on_demand:!sync,
                selected_date:'2026-11-15', day_start:1794700800, day_end:1794787200,
                stats:{cache_only:sync}, cameras:[
                  {dev_id:'ptz',name:'PTZ',online:true,dates:['2026-11-15']},
                  {dev_id:'doorbell',name:'Doorbell',online:false,dates:[]},
                  {dev_id:'other',name:'Other',online:true,dates:[]}
                ].map(camera => ({...camera,clips:[],recording_ranges:[],
                                  playback_mode:sync?'clips':'timeline'}))
              };
              window.hassFixture = {states:{},locale:{language:'en',time_format:'12'},
                callApi:async (method,path) => {
                  if (method !== 'GET') throw new Error('Unexpected camera command');
                  calls.push(path); return structuredClone(payload);
                }, callService:async (domain, service, data) => {
                  if (domain !== 'tuya_recordings' || service !== 'set_camera_order') throw new Error('Unexpected camera command');
                  if (window.failSave) throw new Error('Save rejected');
                  window.savedOrders ||= [];
                  savedOrders.push(structuredClone(data));
                  payload.camera_order = data.camera_order;
                  payload.cameras.sort((a,b) => data.camera_order.indexOf(a.dev_id) - data.camera_order.indexOf(b.dev_id));
                }
              };
              window.openPanel = async () => {
                window.panel?.remove();
                window.panel = document.createElement('tuya-recordings-panel');
                panel._hass = hassFixture; document.body.append(panel);
                await panel.loadData();
              };
              await openPanel();
            }""", sync)
            camera = page.locator("tuya-recordings-panel #camera")
            assert camera.input_value() == "doorbell"
            # Arrange the second and third cameras, not just the opening camera.
            page.locator("tuya-recordings-panel #camera-order").click()
            dialog = page.locator("tuya-recordings-panel #camera-order-dialog")
            assert dialog.is_visible()
            assert dialog.locator('[data-order-index="0"][data-order-direction="-1"]').is_disabled()
            dialog.locator('[data-order-index="2"][data-order-direction="-1"]').click()
            dialog.locator('[data-order-index="1"][data-order-direction="-1"]').press("Enter")
            assert page.evaluate("panel.cameraOrderDraft") == ["other", "ptz", "doorbell"]
            assert dialog.locator('ha-icon[icon="mdi:arrow-up"]').count() == 3
            assert dialog.evaluate("""dialog => {
              const bounds = dialog.getBoundingClientRect();
              return [...dialog.querySelectorAll('button')].every(button => {
                const r = button.getBoundingClientRect();
                return r.left >= bounds.left && r.right <= bounds.right && r.width >= 44;
              });
            }""")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            artifacts = Path(os.environ.get("TUYA_ORDER_ARTIFACTS", str(Path.cwd() / '.pytest_cache' / 'camera-order')))
            artifacts.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(artifacts / f'camera-order-{width}-{sync}.png'))
            page.locator("tuya-recordings-panel #camera-order-cancel").click()
            assert page.evaluate("window.savedOrders?.length || 0") == 0
            assert camera.input_value() == "doorbell"
            page.locator("tuya-recordings-panel #camera-order").click()
            dialog.locator('[data-order-index="2"][data-order-direction="-1"]').click()
            dialog.locator('[data-order-index="1"][data-order-direction="-1"]').click()
            page.evaluate("window.failSave = true")
            page.locator("tuya-recordings-panel #camera-order-save").click()
            page.wait_for_function("() => !panel.cameraOrderSaving && Boolean(panel.cameraOrderError)")
            assert "Save rejected" in dialog.inner_text()
            assert page.evaluate("panel.cameraOrderDraft") == ["other", "ptz", "doorbell"]
            page.evaluate("window.failSave = false")
            page.locator("tuya-recordings-panel #camera-order-save").click()
            page.wait_for_function("() => panel.cameraOrderDraft === null")
            assert page.evaluate("savedOrders[0]") == {"entry_id": "account", "camera_order": ["other", "ptz", "doorbell"]}
            assert camera.locator("option").all_text_contents() == ["Other", "PTZ", "Doorbell (Offline)"]
            assert camera.input_value() == "doorbell"
            # A preferred offline camera remains viewable; no unsolicited probes
            # or playback are introduced to decide which camera is 'active'.
            if not sync:
                assert "camera=doorbell" in page.evaluate("calls.at(-1)")
                assert not any("camera=ptz" in call for call in page.evaluate("calls"))
            camera.select_option("ptz")
            page.wait_for_function("() => panel.selectedCamera === 'ptz' && !panel.loading")
            page.evaluate("async () => { await panel.loadData({quiet:true}); }")
            assert camera.input_value() == "ptz"
            page.evaluate("async () => { await openPanel(); }")
            assert camera.input_value() == "other"
            page.locator("tuya-recordings-panel #camera-order").click()
            page.keyboard.press("Escape")
            assert not dialog.count()
            page.evaluate("async () => { payload.cameras = payload.cameras.filter(c => c.dev_id !== 'other'); payload.camera_order = ['ptz','doorbell']; await openPanel(); }")
            assert camera.input_value() == "ptz"
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        finally:
            browser.close()
