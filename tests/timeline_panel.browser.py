"""Exercise the uncached SD timeline without contacting a camera."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "custom_components" / "tuya_recordings" / "frontend"


def main() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/":
                body, content_type = b"<html><body></body></html>", "text/html"
            elif path.startswith("/tuya_recordings_static/"):
                source = (FRONTEND / path.removeprefix("/tuya_recordings_static/")).resolve()
                if not source.is_relative_to(FRONTEND) or not source.is_file():
                    self.send_error(404)
                    return
                body = source.read_bytes()
                content_type = {
                    ".js": "text/javascript",
                    ".mjs": "text/javascript",
                    ".css": "text/css",
                    ".svg": "image/svg+xml",
                }.get(source.suffix, "application/octet-stream")
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.evaluate(
                """async () => {
                  document.body.style.cssText = 'margin:0;--primary-text-color:#eee;--secondary-text-color:#aaa;--primary-background-color:#111;--card-background-color:#222;--divider-color:#555';
                  await import('/tuya_recordings_static/panel.js');
                  const panel = document.createElement('tuya-recordings-panel');
                  panel._hass = {
                    locale: {language: 'en', time_format: '12'},
                    states: {'switch.tuya_recordings_pause_camera_activity': {state: 'off'}},
                    callApi: async () => panel.data,
                    connection: {sendMessagePromise: async message => ({path: message.path})},
                  };
                  panel.selectedCamera = 'fixture';
                  panel.selectedDate = '2026-09-21';
                  panel.data = {on_demand: true, selected_date: '2026-09-21', stats: {cache_only: false}, cameras: [{
                    dev_id: 'fixture', name: 'Front garden', online: true,
                    playback_mode: 'timeline', clips: [], dates: ['2026-09-21'],
                    recording_ranges: [
                      {dev_id: 'fixture', date: '2026-09-21', start: 1790011833, end: 1790011899, duration: 66, cached: false, stream_transport: 'native', playback_url: '/api/tuya_recordings/timeline/fixture'},
                      {dev_id: 'fixture', date: '2026-09-21', start: 1790011901, end: 1790011938, duration: 37, cached: false, stream_transport: 'native', playback_url: '/api/tuya_recordings/timeline/fixture'},
                    ],
                  }]};
                  panel.openTimelineRange = (range, time) => { window.timelineSelection = {start: range.start, time}; };
                  document.body.append(panel);
                  await panel.loadData();
                }"""
            )
            page.wait_for_function(
                "document.querySelector('tuya-recordings-panel').shadowRoot.querySelectorAll('.recording-segment').length === 2",
                timeout=10_000,
            )
            drag_report = page.evaluate(
                """async () => {
                  const panel = document.querySelector('tuya-recordings-panel');
                  const root = document.querySelector('tuya-recordings-panel').shadowRoot;
                  const ruler = root.querySelector('#recording-timeline');
                  const bounds = ruler.getBoundingClientRect();
                  const dayStart = new Date(`${panel.selectedDate}T00:00:00`).getTime() / 1000;
                  const targetSeconds = 4 * 3600;
                  const scrollWidth = ruler.scrollWidth - ruler.clientWidth;
                  const targetScroll = (targetSeconds / 86400) * scrollWidth;
                  const startX = bounds.left + bounds.width / 2;
                  const releaseX = startX - (targetScroll - ruler.scrollLeft);
                  const activeRange = panel.recordingRanges[0];
                  const calls = [];
                  const controller = () => {};
                  controller.pause = () => calls.push('pause');
                  controller.playClip = (clip, offset) => calls.push({start: clip.start, offset});
                  panel.disposePlayer = controller;
                  panel.selectedClip = {...activeRange, play_time: activeRange.start};
                  ruler.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, pointerId: 7, pointerType: 'mouse', button: 0, clientX: startX}));
                  window.dispatchEvent(new PointerEvent('pointermove', {bubbles: true, pointerId: 7, pointerType: 'mouse', button: 0, clientX: releaseX}));
                  window.dispatchEvent(new PointerEvent('pointerup', {bubbles: true, pointerId: 7, pointerType: 'mouse', button: 0, clientX: releaseX}));
                  await new Promise(resolve => setTimeout(resolve, 50));
                  const droppedValue = Number(ruler.getAttribute('aria-valuenow'));
                  panel.updateTimelinePlayhead(dayStart + 15 * 3600);
                  await new Promise(resolve => requestAnimationFrame(resolve));
                  return {
                    droppedValue,
                    afterStalePlayerUpdate: Number(ruler.getAttribute('aria-valuenow')),
                    dragging: ruler.dataset.dragging || '',
                    releasedOutside: releaseX > bounds.right,
                    selectionAfterDrag: window.timelineSelection || null,
                    calls,
                  };
                }"""
            )
            resume_report = page.evaluate(
                """async () => {
                  const panel = document.querySelector('tuya-recordings-panel');
                  const root = panel.shadowRoot;
                  const ruler = root.querySelector('#recording-timeline');
                  const bounds = ruler.getBoundingClientRect();
                  const dayStart = new Date(panel.selectedDate + 'T00:00:00').getTime() / 1000;
                  const range = panel.recordingRanges[0];
                  const target = range.start + 5;
                  const targetScroll = ((target - dayStart) / 86400) * (ruler.scrollWidth - ruler.clientWidth);
                  const startX = bounds.left + bounds.width / 2;
                  const releaseX = startX - (targetScroll - ruler.scrollLeft);
                  const calls = [];
                  const controller = () => {};
                  controller.pause = () => calls.push('pause');
                  controller.playClip = (clip, offset) => calls.push({start: clip.start, offset});
                  panel.disposePlayer = controller;
                  panel.selectedClip = {...range, play_time: range.start};
                  delete panel.openTimelineRange;
                  ruler.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, pointerId: 8, pointerType: 'mouse', button: 0, clientX: startX}));
                  window.dispatchEvent(new PointerEvent('pointermove', {bubbles: true, pointerId: 8, pointerType: 'mouse', button: 0, clientX: releaseX}));
                  window.dispatchEvent(new PointerEvent('pointerup', {bubbles: true, pointerId: 8, pointerType: 'mouse', button: 0, clientX: releaseX}));
                  await new Promise(resolve => setTimeout(resolve, 50));
                  const droppedValue = Number(ruler.getAttribute('aria-valuenow'));
                  panel.updateTimelinePlayhead(dayStart + 2 * 3600);
                  const afterOldStreamUpdate = Number(ruler.getAttribute('aria-valuenow'));
                  panel.updateTimelinePlayhead(target);
                  const afterReplacementStreamUpdate = Number(ruler.getAttribute('aria-valuenow'));
                  return {
                    calls,
                    droppedValue,
                    rangeStartValue: range.start - dayStart,
                    expectedValue: target - dayStart,
                    afterOldStreamUpdate,
                    afterReplacementStreamUpdate,
                  };
                }"""
            )
            persistent_player = page.evaluate(
                """async () => {
                  const panel = document.querySelector('tuya-recordings-panel');
                  const player = panel.shadowRoot.querySelector('.timeline-player');
                  const calls = [];
                  const controller = () => {};
                  controller.playClip = (clip, offset) => calls.push({start: clip.start, offset});
                  controller.pause = () => {};
                  panel.disposePlayer = controller;
                  delete panel.openTimelineRange;
                  const range = panel.recordingRanges[1];
                  await panel.openTimelineRange(range, range.start + 5);
                  return {
                    samePlayer: player === panel.shadowRoot.querySelector('.timeline-player'),
                    calls,
                  };
                }"""
            )
            report = page.evaluate(
                """() => {
                  const root = document.querySelector('tuya-recordings-panel').shadowRoot;
                  const player = root.querySelector('.timeline-player').getBoundingClientRect();
                  const poster = root.querySelector('.timeline-poster').getBoundingClientRect();
                  const selectedDate = document.querySelector('tuya-recordings-panel').selectedDate;
                  const report = {
                    selection: window.timelineSelection,
                    timelineItems: root.querySelectorAll('.recording-segment').length,
                    clipCards: root.querySelectorAll('.clip').length,
                    poster: Boolean(root.querySelector('.timeline-poster')),
                    playerWidth: player.width,
                    playerHeight: player.height,
                    posterWidth: poster.width,
                    posterHeight: poster.height,
                    pauseToggle: Boolean(root.querySelector('#pause-toggle')),
                    text: root.querySelector('.timeline-shell').textContent,
                    timelineRight: root.querySelector('.timeline-shell').getBoundingClientRect().right,
                    viewportWidth: window.innerWidth,
                    documentWidth: document.documentElement.scrollWidth,
                    selectedDate,
                  };
                  const panel = document.querySelector('tuya-recordings-panel');
                  panel.selectedDate = '2026-09-22';
                  panel.render();
                  report.emptyDayKeepsViewer = Boolean(panel.shadowRoot.querySelector('.timeline-player .timeline-empty-player'));
                  return report;
                }"""
            )
            stale_report = page.evaluate(
                """async () => {
                  const panel = document.createElement('tuya-recordings-panel');
                  const calls = [];
                  const stale = {on_demand: true, selected_date: '2026-09-21', stats: {cache_only: false}, cameras: [{
                    dev_id: 'fixture', name: 'Front garden', online: true,
                    playback_mode: 'timeline', clips: [], dates: ['2026-09-20'],
                    recording_ranges: [{dev_id: 'fixture', date: '2026-09-20', start: 1789963078, end: 1789963115, playback_url: '/old'}],
                  }]};
                  panel._hass = {
                    locale: {language: 'en', time_format: '12'},
                    states: {'switch.tuya_recordings_pause_camera_activity': {state: 'off'}},
                    callApi: async (method, path) => { calls.push(path); return structuredClone(stale); },
                    connection: {sendMessagePromise: async message => ({path: message.path})},
                  };
                  document.body.append(panel);
                  await panel.loadData();
                  return {
                    calls,
                    selectedDate: panel.selectedDate,
                    emptyViewer: Boolean(panel.shadowRoot.querySelector('.timeline-empty-player')),
                  };
                }"""
            )
            initial_time_report = page.evaluate(
                """() => {
                  const panel = document.querySelector('tuya-recordings-panel');
                  const now = Date.now() / 1000;
                  panel.selectedDate = panel.localDateString(new Date());
                  panel.selectedClip = null;
                  panel.timelineAnchor = null;
                  const dayStart = new Date(`${panel.selectedDate}T00:00:00`).getTime() / 1000;
                  const staleStart = Math.max(dayStart, now - 3600);
                  const stale = {start: staleStart, end: staleStart + 60};
                  const recent = {start: now - 120, end: now - 60};
                  return {
                    stale: panel.timelineInitialTime([stale], dayStart),
                    staleExpected: stale.start,
                    recent: panel.timelineInitialTime([recent], dayStart),
                    now,
                  };
                }"""
            )
            keyboard_report = page.evaluate(
                """async () => {
                  const panel = document.querySelector('tuya-recordings-panel');
                  panel.selectedDate = '2026-09-21';
                  panel.selectedClip = {...panel.recordingRanges[0], play_time: panel.recordingRanges[0].start};
                  panel.timelineAnchor = null;
                  panel.render();
                  await new Promise(resolve => requestAnimationFrame(resolve));
                  const ruler = panel.shadowRoot.querySelector('#recording-timeline');
                  const dayStart = new Date(panel.selectedDate + 'T00:00:00').getTime() / 1000;
                  ruler.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, key: 'PageDown'}));
                  const afterKey = Number(ruler.getAttribute('aria-valuenow'));
                  panel.updateTimelinePlayhead(dayStart + 18 * 3600);
                  return {
                    afterKey,
                    afterStaleUpdate: Number(ruler.getAttribute('aria-valuenow')),
                    anchorOffset: panel.timelineAnchor?.time - dayStart,
                  };
                }"""
            )
            cancellation_report = page.evaluate(
                """async () => {
                  const panel = document.querySelector('tuya-recordings-panel');
                  panel.selectedDate = '2026-09-21';
                  panel.render();
                  await new Promise(resolve => requestAnimationFrame(resolve));
                  const ruler = panel.shadowRoot.querySelector('#recording-timeline');
                  const bounds = ruler.getBoundingClientRect();
                  const dayStart = new Date(panel.selectedDate + 'T00:00:00').getTime() / 1000;
                  const targetSeconds = 19 * 3600;
                  const scrollWidth = ruler.scrollWidth - ruler.clientWidth;
                  const targetScroll = (targetSeconds / 86400) * scrollWidth;
                  const startX = bounds.left + bounds.width / 2;
                  const targetX = startX - (targetScroll - ruler.scrollLeft);
                  const calls = [];
                  const controller = () => {};
                  controller.playClip = (...args) => calls.push(args);
                  panel.disposePlayer = controller;
                  ruler.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, pointerId: 9, pointerType: 'mouse', button: 0, clientX: startX}));
                  window.dispatchEvent(new PointerEvent('pointermove', {bubbles: true, pointerId: 9, pointerType: 'mouse', button: 0, clientX: targetX}));
                  await new Promise(resolve => requestAnimationFrame(resolve));
                  const beforeCancellation = (ruler.scrollLeft / (ruler.scrollWidth - ruler.clientWidth)) * 86400;
                  window.dispatchEvent(new PointerEvent('pointercancel', {bubbles: true, pointerId: 9, pointerType: 'mouse', clientX: targetX}));
                  panel.render();
                  await new Promise(resolve => requestAnimationFrame(resolve));
                  const rebuilt = panel.shadowRoot.querySelector('#recording-timeline');
                  return {
                    beforeCancellation,
                    afterCancellationAndRender: Number(rebuilt.getAttribute('aria-valuenow')),
                    calls,
                    dragging: rebuilt.dataset.dragging || '',
                  };
                }"""
            )
            page.evaluate(
                """async () => {
                  const panel = document.querySelector('tuya-recordings-panel');
                  panel.selectedDate = '2026-09-21';
                  panel.render();
                  await new Promise(resolve => requestAnimationFrame(resolve));
                }"""
            )
            drag_box = page.evaluate(
                """() => {
                  const box = document.querySelector('tuya-recordings-panel').shadowRoot
                    .querySelector('#recording-timeline').getBoundingClientRect();
                  return {x: box.left + box.width / 2, y: box.top + box.height / 2};
                }"""
            )
            page.mouse.move(drag_box["x"], drag_box["y"])
            page.mouse.down()
            page.mouse.move(drag_box["x"] - 150, drag_box["y"], steps=8)
            redraw_while_dragging = page.evaluate(
                """() => {
                  const panel = document.querySelector('tuya-recordings-panel');
                  const before = panel.shadowRoot.querySelector('#recording-timeline');
                  panel.render();
                  return {
                    sameRuler: before === panel.shadowRoot.querySelector('#recording-timeline'),
                    pending: panel.pendingTimelineRender,
                  };
                }"""
            )
            native_drag_mid = page.evaluate(
                """() => {
                  const ruler = document.querySelector('tuya-recordings-panel').shadowRoot
                    .querySelector('#recording-timeline');
                  return {scrollLeft: ruler.scrollLeft, time: Number(ruler.getAttribute('aria-valuenow'))};
                }"""
            )
            page.mouse.move(drag_box["x"] - 350, drag_box["y"], steps=10)
            native_drag_far = page.evaluate(
                """() => document.querySelector('tuya-recordings-panel').shadowRoot
                  .querySelector('#recording-timeline').scrollLeft"""
            )
            page.mouse.move(drag_box["x"] - 520, drag_box["y"], steps=8)
            native_drag_outside = page.evaluate(
                """() => {
                  const ruler = document.querySelector('tuya-recordings-panel').shadowRoot
                    .querySelector('#recording-timeline');
                  return {scrollLeft: ruler.scrollLeft, dragging: ruler.dataset.dragging || ''};
                }"""
            )
            page.mouse.up()
            page.wait_for_timeout(50)
            page.set_viewport_size({"width": 1280, "height": 720})
            desktop_player_width = page.evaluate(
                "document.querySelector('tuya-recordings-panel').shadowRoot.querySelector('.timeline-player').getBoundingClientRect().width"
            )
            desktop_geometry = page.evaluate(
                """() => {
                  const root = document.querySelector('tuya-recordings-panel').shadowRoot;
                  const page = root.querySelector('.page').getBoundingClientRect();
                  const player = root.querySelector('.timeline-player').getBoundingClientRect();
                  const timeline = root.querySelector('.timeline-shell').getBoundingClientRect();
                  return {pageHeight: page.height, playerTop: player.top, playerBottom: player.bottom, timelineTop: timeline.top, pageRight: page.right};
                }"""
            )
            page.set_viewport_size({"width": 1024, "height": 600})
            compact_desktop_geometry = page.evaluate(
                """() => {
                  const root = document.querySelector('tuya-recordings-panel').shadowRoot;
                  const page = root.querySelector('.page').getBoundingClientRect();
                  const player = root.querySelector('.timeline-player').getBoundingClientRect();
                  const timeline = root.querySelector('.timeline-shell').getBoundingClientRect();
                  return {pageHeight: page.height, playerTop: player.top, playerBottom: player.bottom, timelineTop: timeline.top, documentWidth: document.documentElement.scrollWidth};
                }"""
            )
            calendar_report = page.evaluate(
                """() => {
                  const panel = document.querySelector('tuya-recordings-panel');
                  panel.selectedDate = '2026-09-21';
                  panel.calendarMonth = '2026-09';
                  panel.calendarOpen = true;
                  panel.render();
                  const root = panel.shadowRoot;
                  return {
                    availableDays: [...root.querySelectorAll('.calendar-day.has-recordings')].map(item => item.dataset.date),
                    selected: root.querySelector('.calendar-day.selected')?.dataset.date,
                    dialog: Boolean(root.querySelector('.calendar-popover[role="dialog"]')),
                  };
                }"""
            )
            redraw_player_report = page.evaluate(
                """() => {
                  const panel = document.querySelector('tuya-recordings-panel');
                  panel.afterRender = () => {};
                  panel.selectedClip = {...panel.recordingRanges[0], signed_playback_url: '/fixture'};
                  panel.render();
                  const player = panel.shadowRoot.querySelector('.camera-player');
                  let disconnected = 0;
                  customElements.define('player-connection-probe', class extends HTMLElement {
                    disconnectedCallback() { disconnected++; }
                  });
                  player.append(document.createElement('player-connection-probe'));
                  panel.playerRevision = panel.playbackRevision;
                  panel.calendarOpen = !panel.calendarOpen;
                  panel.render();
                  return {same: panel.shadowRoot.querySelector('.camera-player') === player,
                    connected: player.isConnected, disconnected};
                }"""
            )
            calendar_race_report = page.evaluate(
                """async () => {
                  const panel = document.querySelector('tuya-recordings-panel');
                  panel.stopPlayback();
                  panel.selectedClip = null;
                  panel.selectedCamera = 'fixture';
                  panel.selectedAccount = 'fixture-account';
                  panel.selectedDate = '2026-11-03';
                  panel.calendarMonth = '2026-11';
                  panel.calendarOpen = true;
                  panel.loading = false;
                  const base = {on_demand:true, entry_id:'fixture-account', catalog_refresh_failed:true,
                    selected_date:'2026-11-03', stats:{cache_only:false},
                    cameras:[{dev_id:'fixture',name:'Fixture',dates:['2026-11-01','2026-11-03'],
                      clips:[],recording_ranges:[],playback_mode:'timeline'}]};
                  panel.data = structuredClone(base);
                  let complete;
                  const delayed = new Promise(resolve => {complete = resolve;});
                  const calls = [];
                  panel._hass.callApi = async (method, path) => {
                    calls.push(path);
                    if (calls.length === 1) await delayed;
                    const reply = structuredClone(base);
                    reply.selected_date = new URLSearchParams(path.split('?')[1]).get('date');
                    return reply;
                  };
                  panel.render();
                  const inFlight = panel.loadData({quiet:true});
                  panel.shadowRoot.querySelector('[data-date="2026-11-01"]').click();
                  complete();
                  await inFlight;
                  await new Promise(resolve => setTimeout(resolve,100));
                  return {selected:panel.selectedDate, calls,
                    responseDate:panel.data.selected_date, loading:panel.loading,
                    status:panel.shadowRoot.querySelector('.status').textContent,
                    dates:panel.data.cameras[0].dates};
                }"""
            )
            browser.close()
        assert not errors, errors
        assert calendar_race_report["selected"] == "2026-11-01", calendar_race_report
        assert calendar_race_report["responseDate"] == "2026-11-01", calendar_race_report
        assert len(calendar_race_report["calls"]) == 2, calendar_race_report
        assert "date=2026-11-01" in calendar_race_report["calls"][1], calendar_race_report
        assert not calendar_race_report["loading"], calendar_race_report
        assert "Could not refresh recordings" in calendar_race_report["status"]
        assert calendar_race_report["dates"] == ["2026-11-03", "2026-11-01"]
        assert redraw_player_report == {"same": True, "connected": True, "disconnected": 0}
        assert report["timelineItems"] == 2
        assert persistent_player == {
            "samePlayer": True,
            "calls": [{"start": 1790011901, "offset": 5}],
        }
        assert report["clipCards"] == 0
        assert report["poster"] is True
        assert report["playerHeight"] >= 190
        assert abs(report["playerWidth"] / report["playerHeight"] - (16 / 9)) < 0.03
        assert abs(report["posterWidth"] - report["playerWidth"]) <= 2
        assert abs(report["posterHeight"] - report["playerHeight"]) <= 2
        assert report["pauseToggle"] is False
        assert report["selectedDate"] == "2026-09-21"
        assert report["emptyDayKeepsViewer"] is True
        assert report["timelineRight"] <= report["viewportWidth"] + 1
        assert report["documentWidth"] <= report["viewportWidth"]
        assert 498 <= desktop_player_width <= 502
        assert desktop_geometry["pageHeight"] <= 720
        assert 0 <= desktop_geometry["timelineTop"] - desktop_geometry["playerBottom"] <= 10
        assert compact_desktop_geometry["pageHeight"] <= 600, compact_desktop_geometry
        assert 0 <= compact_desktop_geometry["timelineTop"] - compact_desktop_geometry["playerBottom"] <= 10
        assert compact_desktop_geometry["documentWidth"] <= 1024
        assert calendar_report == {
            "availableDays": ["2026-09-21"],
            "selected": "2026-09-21",
            "dialog": True,
        }
        assert drag_report["dragging"] == ""
        assert drag_report["releasedOutside"] is True
        assert drag_report["selectionAfterDrag"] == {"start": 1790011833, "time": 1790011833}
        assert abs(drag_report["droppedValue"] - 4 * 3600) <= 30, drag_report
        assert abs(drag_report["afterStalePlayerUpdate"] - drag_report["droppedValue"]) <= 1, drag_report
        assert drag_report["afterStalePlayerUpdate"] == drag_report["droppedValue"]
        assert len(resume_report["calls"]) == 1, resume_report
        assert resume_report["calls"][0]["start"] == 1790011833, resume_report
        dropped_offset = resume_report["droppedValue"] - resume_report["rangeStartValue"]
        assert abs(resume_report["calls"][0]["offset"] - dropped_offset) <= 1, resume_report
        assert abs(resume_report["droppedValue"] - resume_report["expectedValue"]) <= 30, resume_report
        assert abs(resume_report["afterOldStreamUpdate"] - resume_report["droppedValue"]) <= 1, resume_report
        assert abs(resume_report["afterReplacementStreamUpdate"] - resume_report["expectedValue"]) <= 1, resume_report
        assert redraw_while_dragging == {"sameRuler": True, "pending": True}, redraw_while_dragging
        assert native_drag_mid["scrollLeft"] > 100, native_drag_mid
        assert native_drag_mid["time"] > 0, native_drag_mid
        assert native_drag_far > native_drag_mid["scrollLeft"] + 100, (native_drag_mid, native_drag_far)
        assert native_drag_outside["scrollLeft"] > native_drag_far + 100, native_drag_outside
        assert native_drag_outside["dragging"] == "true", native_drag_outside
        assert 0 < keyboard_report["afterKey"] <= 86400, keyboard_report
        assert keyboard_report["afterStaleUpdate"] == keyboard_report["afterKey"], keyboard_report
        assert abs(keyboard_report["anchorOffset"] - keyboard_report["afterKey"]) <= 1, keyboard_report
        assert cancellation_report["calls"] == [], cancellation_report
        assert cancellation_report["dragging"] == "", cancellation_report
        assert abs(cancellation_report["beforeCancellation"] - 19 * 3600) <= 30, cancellation_report
        assert abs(cancellation_report["afterCancellationAndRender"] - cancellation_report["beforeCancellation"]) <= 1, cancellation_report
        assert "Recorded video" in report["text"]
        assert "Drag to browse, tap a recording to play" in report["text"]
        assert stale_report["calls"] == [
            "tuya_recordings/panel?",
            "tuya_recordings/panel?camera=fixture&date=2026-09-21",
        ]
        assert stale_report["selectedDate"] == "2026-09-21"
        assert stale_report["emptyViewer"] is True
        assert initial_time_report["stale"] == initial_time_report["staleExpected"]
        assert abs(initial_time_report["recent"] - initial_time_report["now"]) < 2
        print(json.dumps(report, sort_keys=True))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
