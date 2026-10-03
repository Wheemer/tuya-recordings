class TuyaRecordingsPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.data = null;
    this.selectedCamera = "";
    this.selectedDate = "";
    this.selectedClip = null;
    this.loading = false;
    this.error = "";
    this.signedPaths = new Map();
    this.playbackRevision = 0;
    this.playbackOffset = 0;
    this.playbackPaused = false;
    this.autoplaySelectedClip = false;
    this.pauseToggling = false;
    this.pauseStateOverride = null;
    this.idleRefreshTimer = null;
    this.catalogRefreshTimer = null;
    this.calendarOpen = false;
    this.calendarMonth = "";
    this.timeline = null;
    this.handleRouteChange = async () => {
      this.syncRouteFromHash();
      if (this.selectedClip) {
        await this.signClip(this.selectedClip);
      }
      this.autoplaySelectedClip = false;
      this.render();
    };
  }

  set hass(value) {
    const previousPauseState = this._hass?.states?.[this.pauseEntityId]?.state;
    this._hass = value;
    const nextPauseState = this._hass?.states?.[this.pauseEntityId]?.state;
    if (this.pauseStateOverride !== null && nextPauseState === this.pauseStateOverride) {
      this.pauseStateOverride = null;
    }
    if (!this.data && !this.loading) {
      this.loadData();
      return;
    }
    if (this.data && previousPauseState !== nextPauseState) {
      this.render();
    }
  }

  connectedCallback() {
    window.addEventListener("hashchange", this.handleRouteChange);
    window.addEventListener("popstate", this.handleRouteChange);
    this.syncRouteFromHash();
    this.startIdleRefresh();
    this.render();
  }

  disconnectedCallback() {
    window.removeEventListener("hashchange", this.handleRouteChange);
    window.removeEventListener("popstate", this.handleRouteChange);
    this.stopIdleRefresh();
    this.stopCatalogRefresh();
    this.stopPlayback();
  }

  startIdleRefresh() {
    this.stopIdleRefresh();
    this.idleRefreshTimer = window.setInterval(() => this.refreshIdleData(), 30_000);
  }

  stopIdleRefresh() {
    if (this.idleRefreshTimer !== null) {
      window.clearInterval(this.idleRefreshTimer);
      this.idleRefreshTimer = null;
    }
  }

  stopCatalogRefresh() {
    if (this.catalogRefreshTimer !== null) {
      window.clearTimeout(this.catalogRefreshTimer);
      this.catalogRefreshTimer = null;
    }
  }

  scheduleCatalogRefresh() {
    this.stopCatalogRefresh();
    if (!this.data?.catalog_refresh_pending || this.selectedClip || !this.isConnected) return;
    this.catalogRefreshTimer = window.setTimeout(async () => {
      this.catalogRefreshTimer = null;
      await this.loadData({ quiet: true });
    }, 1000);
  }

  async refreshIdleData() {
    if (
      !this.isConnected ||
      document.visibilityState === "hidden" ||
      this.loading ||
      this.selectedClip ||
      !this.data ||
      this.data.on_demand
    ) return;
    await this.loadData({ quiet: true });
  }

  stopPlayback() {
    this.playbackRevision += 1;
    this.disposePlayer?.();
    this.disposePlayer = null;
    this.timeline?.destroy?.();
    this.timeline = null;
    for (const video of this.shadowRoot.querySelectorAll("video")) {
      video.pause();
      video.removeAttribute("src");
      video.load();
    }
  }

  async loadData({ quiet = false } = {}) {
    if (!this._hass || this.loading) return;
    if (quiet && this.selectedClip) return;
    if (!quiet) this.stopPlayback();
    this.loading = true;
    this.error = "";
    if (!quiet) this.render();
    try {
      const params = new URLSearchParams();
      if (this.selectedCamera) params.set("camera", this.selectedCamera);
      if (this.selectedDate) params.set("date", this.selectedDate);
      let data = this.withReadyThumbnails(
        await this._hass.callApi("GET", `tuya_recordings/panel?${params}`),
      );
      this.data = data;
      const cameras = data.cameras || [];
      if (!this.selectedCamera || !cameras.some((camera) => camera.dev_id === this.selectedCamera)) {
        this.selectedCamera = cameras[0]?.dev_id || "";
      }
      if (data.on_demand) {
        this.selectedDate = data.selected_date;
        if (!params.has("camera") && this.selectedCamera) {
          params.set("camera", this.selectedCamera);
          params.set("date", this.selectedDate);
          data = this.withReadyThumbnails(
            await this._hass.callApi("GET", `tuya_recordings/panel?${params}`),
          );
          this.data = data;
        }
      }
      const camera = this.camera;
      if (!this.selectedDate) {
        this.selectedDate = data.selected_date || camera?.dates?.[0] || "";
      }
      this.syncRouteFromHash();
      await this.signVisibleClips();
      if (this.selectedClip) {
        await this.signClip(this.selectedClip);
      }
    } catch (err) {
      this.error = err?.message || String(err);
    } finally {
      this.loading = false;
      this.render();
      this.scheduleCatalogRefresh();
    }
  }

  get camera() {
    return (this.data?.cameras || []).find((camera) => camera.dev_id === this.selectedCamera);
  }

  get clips() {
    const camera = this.camera;
    if (!camera) return [];
    return (camera.clips || []).filter((clip) => clip.date === this.selectedDate);
  }

  get recordingRanges() {
    const camera = this.camera;
    if (!camera) return [];
    return (camera.recording_ranges || []).filter((item) => item.date === this.selectedDate);
  }

  get timelineMode() {
    return this.camera?.playback_mode === "timeline";
  }

  render() {
    this.disposePlayer?.();
    this.disposePlayer = null;
    this.timeline?.destroy?.();
    this.timeline = null;
    const cameras = this.data?.cameras || [];
    const camera = this.camera;
    const clips = this.clips;
    const clipCount = this.timelineMode ? this.recordingRanges.length : clips.length;
    this.shadowRoot.innerHTML = `
      <link rel="stylesheet" href="/tuya_recordings_static/vendor/plyr/plyr.css">
      <style>
        .camera-player { position: relative; width: 100%; height: 100%; min-width: 0; min-height: 0; overflow: hidden; background: #000; contain: layout paint; --plyr-color-main: #e85b24; --plyr-control-icon-size: 20px; --plyr-control-spacing: 8px; --plyr-control-radius: 4px; --plyr-video-control-background-hover: #c54816; }
        .camera-player .plyr { position: absolute; inset: 0; width: 100%; height: 100%; min-width: 0; min-height: 0; border-radius: 0; }
        .camera-player .plyr__video-wrapper { width: 100%; height: 100%; min-height: 0; background: #000; }
        .camera-player .plyr__control--overlaid { display: none !important; }
        .camera-player .plyr button { min-height: 44px; min-width: 44px; margin: 0; }
        .camera-player .plyr .plyr__control {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          padding: 8px;
        }
        .camera-player .plyr .plyr__control svg {
          margin: 0;
        }
        .camera-player .plyr video { width: 100%; height: 100%; min-height: 0; object-fit: contain; }
        .camera-player--switching video { visibility: hidden; }
        .camera-player .player-status { position: absolute; z-index: 4; top: 10px; left: 50%; translate: -50% 0; max-width: calc(100% - 24px); border-radius: 4px; background: #101010d9; color: #eee; font-size: 12px; line-height: 1.25; text-align: center; pointer-events: none; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .camera-player .player-status:not(:empty) { padding: 6px 9px; }
        .clip[playing] .thumb:has(.camera-player) { aspect-ratio: 16 / 9; }
        .timeline-view { display: grid; grid-template-columns: minmax(0, 760px); justify-content: center; align-items: start; gap: 8px; background: var(--card-background-color); }
        .timeline-player { position: relative; width: min(100%, 500px); aspect-ratio: 16 / 9; justify-self: center; margin: 0; background: #101010; border: 1px solid var(--divider-color); border-radius: 6px; overflow: hidden; contain: layout paint; }
        .timeline-player > * { position: absolute; inset: 0; }
        .timeline-player .camera-player, .timeline-player .timeline-poster { width: 100%; height: 100%; min-height: 0; }
        .timeline-player .camera-player { display: flex; flex-direction: column; }
        .timeline-player .camera-player .plyr { flex: 1 1 auto; min-height: 0; }
        .timeline-poster { position: relative; width: 100%; border: 0; padding: 0; display: grid; place-items: center; overflow: hidden; background: #161616; color: #fff; cursor: pointer; }
        .timeline-poster img { width: 100%; height: 100%; object-fit: contain; }
        .timeline-poster > ha-icon { --mdc-icon-size: 58px; color: #777; }
        .timeline-poster .play-mark { position: absolute; inset: 0; display: grid; place-items: center; }
        .timeline-poster .play-mark ha-icon { --mdc-icon-size: 62px; color: var(--recordings-accent); filter: drop-shadow(0 2px 4px #0008); }
        .timeline-empty-player { cursor: default; grid-template-rows: auto auto; align-content: center; gap: 10px; color: #aaa; font-size: 13px; }
        .timeline-shell { margin: 0; width: 100%; padding: 8px 0 4px; background: var(--card-background-color); overflow: hidden; }
        .timeline-heading { display: grid; justify-items: center; gap: 2px; padding: 0 12px 6px; }
        .timeline-heading strong { font-size: 13px; font-weight: 500; color: var(--secondary-text-color); }
        .timeline-heading span { font-size: 18px; line-height: 1.2; font-variant-numeric: tabular-nums; color: var(--primary-text-color); }
        .time-ruler-wrap { position: relative; height: 88px; border-top: 1px solid var(--divider-color); border-bottom: 1px solid var(--divider-color); background: var(--secondary-background-color, #f3f4f6); overflow: hidden; }
        .time-ruler { width: 100%; height: 100%; overflow-x: auto; overflow-y: hidden; overscroll-behavior-x: contain; scrollbar-width: none; touch-action: pan-y; cursor: grab; }
        .time-ruler:active, .time-ruler[data-dragging="true"] { cursor: grabbing; user-select: none; }
        .time-ruler::-webkit-scrollbar { display: none; }
        .time-ruler-content { position: relative; width: 2880px; height: 100%; margin-inline: calc(50% - 1px); background-image: repeating-linear-gradient(to right, transparent 0, transparent 19px, color-mix(in srgb, var(--secondary-text-color) 28%, transparent) 19px, color-mix(in srgb, var(--secondary-text-color) 28%, transparent) 20px); background-size: 120px 12px; background-position: 0 32px; background-repeat: repeat-x; }
        .timeline-hour { position: absolute; top: 9px; translate: -50% 0; font-size: 11px; color: var(--secondary-text-color); font-variant-numeric: tabular-nums; white-space: nowrap; }
        .timeline-hour::after { content: ""; position: absolute; left: 50%; top: 19px; width: 1px; height: 18px; background: color-mix(in srgb, var(--secondary-text-color) 60%, transparent); }
        .recording-lane { position: absolute; inset: 49px 0 auto; height: 24px; background: color-mix(in srgb, var(--divider-color) 55%, transparent); }
        .recording-segment { position: absolute; top: 3px; height: 18px; min-width: 4px; border: 0; border-radius: 2px; padding: 0; background: #ff592a; cursor: pointer; box-shadow: inset 0 0 0 1px #e2471d; }
        .recording-segment:hover, .recording-segment:focus-visible { filter: brightness(1.08); outline: 2px solid var(--primary-text-color); outline-offset: 1px; }
        .timeline-playhead { pointer-events: none; position: absolute; z-index: 3; left: 50%; top: 0; bottom: 0; width: 2px; translate: -1px 0; background: #ff592a; box-shadow: 0 0 0 1px color-mix(in srgb, var(--card-background-color) 70%, transparent); }
        .timeline-playhead::before { content: ""; position: absolute; top: 0; left: 50%; translate: -50% 0; width: 0; height: 0; border-left: 7px solid transparent; border-right: 7px solid transparent; border-top: 9px solid #ff592a; }
        .timeline-legend { display: flex; justify-content: center; align-items: center; gap: 8px; min-height: 27px; padding: 5px 14px 0; font-size: 12px; color: var(--secondary-text-color); }
        .timeline-legend i { width: 13px; height: 7px; border-radius: 2px; background: #ff592a; }
        :host {
          --recordings-accent: #e85b24;
          display: block;
          min-height: 100vh;
          color: var(--primary-text-color);
          background: var(--primary-background-color);
          box-sizing: border-box;
          font-family: var(--paper-font-body1_-_font-family, Roboto, Arial, sans-serif);
        }
        * { box-sizing: border-box; }
        .page {
          max-width: 1600px;
          margin: 0 auto;
          padding: 12px 20px;
        }
        .page-heading { display: flex; align-items: center; gap: 10px; margin: 0 0 10px; }
        .heading-row {
          display: flex;
          align-items: flex-start;
          justify-content: space-between;
          gap: 16px;
          margin-bottom: 10px;
        }
        .heading-row .page-heading { margin: 0; }
        .page-heading > ha-icon { color: var(--recordings-accent); --mdc-icon-size: 26px; }
        h1 { font-size: 20px; line-height: 1.2; font-weight: 600; margin: 0; letter-spacing: 0; }
        .page-heading p { margin: 1px 0 0; font-size: 12px; color: var(--secondary-text-color); }
        .pause-toggle {
          height: 44px;
          flex: 0 0 auto;
          border: 1px solid var(--recordings-accent);
          background: var(--card-background-color);
          color: var(--recordings-accent);
          font-weight: 500;
          white-space: nowrap;
        }
        .pause-toggle[aria-pressed="true"],
        .pause-toggle:hover {
          background: var(--recordings-accent);
          color: #fff;
        }
        .pause-toggle ha-icon {
          color: currentColor;
          --mdc-icon-size: 20px;
        }
        .toolbar {
          display: grid;
          grid-template-columns: minmax(180px, 260px) minmax(150px, 220px) 1fr auto;
          gap: 12px;
          align-items: end;
          padding-bottom: 10px;
          margin-bottom: 10px;
          border-bottom: 1px solid var(--divider-color);
        }
        label, .field {
          display: grid;
          gap: 6px;
          color: var(--secondary-text-color);
          font-size: 13px;
        }
        .date-field { position: relative; }
        .date-button {
          width: 100%;
          justify-content: space-between;
          padding-inline: 12px 8px;
          color: var(--primary-text-color);
        }
        .date-button ha-icon { --mdc-icon-size: 20px; color: var(--secondary-text-color); }
        .calendar-popover {
          position: absolute;
          z-index: 20;
          top: calc(100% + 6px);
          left: 0;
          width: min(310px, calc(100vw - 32px));
          padding: 10px;
          border: 1px solid var(--divider-color);
          border-radius: 6px;
          background: var(--card-background-color);
          box-shadow: 0 8px 24px #0004;
          color: var(--primary-text-color);
        }
        .calendar-header { display: grid; grid-template-columns: 40px 1fr 40px; align-items: center; margin-bottom: 6px; }
        .calendar-header strong { text-align: center; font-size: 14px; font-weight: 600; }
        .calendar-header button { width: 40px; height: 40px; padding: 0; border: 0; }
        .calendar-header ha-icon { --mdc-icon-size: 20px; }
        .calendar-grid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 3px; }
        .calendar-weekday { min-height: 24px; display: grid; place-items: center; color: var(--secondary-text-color); font-size: 11px; }
        .calendar-day {
          position: relative;
          width: 100%;
          min-width: 0;
          height: 36px;
          padding: 0;
          border: 0;
          border-radius: 4px;
          color: var(--secondary-text-color);
          background: transparent;
          font-variant-numeric: tabular-nums;
        }
        .calendar-day.has-recordings { color: var(--primary-text-color); font-weight: 600; }
        .calendar-day.has-recordings::after {
          content: "";
          position: absolute;
          left: 50%;
          bottom: 3px;
          width: 5px;
          height: 5px;
          translate: -50% 0;
          border-radius: 50%;
          background: var(--recordings-accent);
        }
        .calendar-day.selected { background: var(--recordings-accent); color: #fff; }
        .calendar-day.selected::after { background: #fff; }
        .calendar-day:disabled { cursor: default; opacity: 0.3; }
        .calendar-blank { min-height: 36px; }
        select, button, input[type=date] {
          height: 44px;
          min-width: 0;
          border: 1px solid var(--divider-color);
          border-radius: 6px;
          background: var(--card-background-color);
          color: var(--primary-text-color);
          font: inherit;
        }
        select, input[type=date] { padding: 0 12px; width: 100%; color-scheme: light dark; }
        button:focus-visible, .clip:focus-visible, select:focus-visible, input:focus-visible {
          outline: 2px solid var(--recordings-accent); outline-offset: 3px;
        }
        .icon-button { width: 44px; padding: 0; }
        .cache-summary { display: flex; flex-wrap: wrap; gap: 16px 28px; padding: 14px 0; margin-bottom: 20px; border-block: 1px solid var(--divider-color); }
        .cache-summary > div { display: flex; align-items: center; gap: 8px; font-size: 13px; }
        .cache-summary ha-icon { color: var(--secondary-text-color); --mdc-icon-size: 20px; }
        .cache-summary strong { font-weight: 600; }
        .cache-summary span { color: var(--secondary-text-color); }
        button {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          gap: 8px;
          padding: 0 14px;
          cursor: pointer;
        }
        button:hover { background: var(--secondary-background-color); }
        .stats-grid {
          display: grid;
          grid-template-columns: repeat(4, minmax(150px, 1fr));
          gap: 10px;
          margin-bottom: 14px;
        }
        .stat-card {
          min-height: 78px;
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          background: var(--card-background-color);
          padding: 10px 12px;
          display: grid;
          align-content: center;
          gap: 5px;
        }
        .stat-label {
          color: var(--secondary-text-color);
          font-size: 12px;
          text-transform: uppercase;
          letter-spacing: 0;
        }
        .stat-value {
          color: var(--primary-text-color);
          font-size: 22px;
          line-height: 1.1;
          font-weight: 500;
        }
        .stat-sub {
          color: var(--secondary-text-color);
          font-size: 12px;
          line-height: 1.3;
          overflow-wrap: anywhere;
        }
        .status {
          min-height: 40px;
          display: flex;
          align-items: center;
          color: var(--secondary-text-color);
          font-size: 14px;
        }
        .clips {
          display: grid;
          grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
          gap: 24px;
        }
        .clip {
          display: grid;
          grid-template-rows: auto 1fr;
          min-height: 0;
          overflow: hidden;
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          background: var(--card-background-color);
          cursor: pointer;
          text-align: left;
          padding: 0;
          font: inherit;
        }
        .clip:hover { border-color: var(--recordings-accent); }
        .clip[playing] {
          border-color: var(--recordings-accent);
        }
        .clip .play-mark { position: absolute; inset: 0; display: grid; place-items: center; pointer-events: none; }
        .play-mark ha-icon { --mdc-icon-size: 42px; color: var(--recordings-accent); filter: drop-shadow(0 1px 4px #000); opacity: 0.95; }
        .clip:hover .play-mark ha-icon { opacity: 1; }
        .thumb {
          position: relative;
          aspect-ratio: 16 / 9;
          background: var(--secondary-background-color);
          overflow: hidden;
        }
        .thumb img {
          width: 100%;
          height: 100%;
          object-fit: cover;
          display: block;
        }
        .missing-thumb {
          width: 100%;
          height: 100%;
          display: grid;
          place-items: center;
          color: var(--secondary-text-color);
          font-size: 13px;
        }
        .badge {
          position: absolute;
          right: 8px;
          bottom: 8px;
          padding: 3px 7px;
          border-radius: 5px;
          background: rgba(0, 0, 0, 0.72);
          color: #fff;
          font-size: 12px;
        }
        .meta {
          display: grid;
          gap: 4px;
          padding: 12px 14px;
        }
        .title {
          font-size: 15px;
          font-weight: 500;
          line-height: 1.25;
          color: var(--primary-text-color);
          overflow-wrap: anywhere;
        }
        .sub {
          font-size: 12px;
          color: var(--secondary-text-color);
        }
        .clip-player {
          width: 100%;
          height: 100%;
          min-height: 0;
          background: #000;
          display: block;
        }
        video {
          width: 100%;
          aspect-ratio: 16 / 9;
          background: #000;
          display: block;
        }
        .no-video {
          width: 100%;
          aspect-ratio: 16 / 9;
          display: grid;
          place-items: center;
          background: #000;
          color: #fff;
        }
        .empty, .error {
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          padding: 18px;
          color: var(--secondary-text-color);
          background: var(--card-background-color);
        }
        .error { color: var(--error-color); }
        @media (min-width: 901px) and (max-height: 650px) {
          .timeline-player { width: min(100%, 460px); }
        }
        @media (max-width: 900px) {
          .page {
            padding: 16px;
          }
          .heading-row {
            align-items: stretch;
            gap: 12px;
          }
          .pause-toggle {
            width: 44px;
            padding: 0;
          }
          .pause-toggle .pause-label {
            display: none;
          }
          .toolbar {
            grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 44px;
            gap: 12px 8px;
          }
          .toolbar .status { grid-column: 1 / -1; grid-row: 2; min-height: 20px; }
          .toolbar #refresh { grid-column: 3; grid-row: 1; }
          .page-heading { margin-bottom: 20px; }
          h1 { font-size: 21px; }
          .timeline-view { grid-template-columns: 1fr; gap: 0; }
          .timeline-player { width: 100%; }
          .timeline-shell { padding: 14px 0 12px; }
          .timeline-heading { gap: 3px; padding: 0 16px 10px; }
          .timeline-heading span { font-size: 22px; }
          .time-ruler-wrap { height: 112px; }
          .recording-lane { inset-block-start: 58px; height: 28px; }
          .recording-segment { height: 22px; }
          .timeline-legend { min-height: 35px; padding-top: 8px; }
          .stats-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr));
          }
          .stat-value {
            font-size: 19px;
          }
          .clips {
            grid-template-columns: 1fr;
          }
          .clip {
            grid-template-rows: auto auto;
            min-height: 0;
            height: auto;
            overflow: visible;
          }
          .meta {
            min-height: 54px;
            padding-bottom: 14px;
          }
          .clip[playing] {
            border-color: var(--recordings-accent);
          }
          video, .no-video {
            min-height: min(58vw, 360px);
          }
        }
      </style>
      <div class="page">
        <div class="heading-row">
          <header class="page-heading"><ha-icon icon="mdi:cctv"></ha-icon><div><h1>Tuya Recordings</h1><p>SD card recordings</p></div></header>
          ${this.renderPauseToggle()}
        </div>
        ${this.renderStats(this.data?.stats)}<div class="toolbar">
          <label>
            Camera
            <select id="camera" ${cameras.length === 0 || this.loading ? "disabled" : ""}>
              ${cameras.map((item) => `<option value="${this.escape(item.dev_id)}" ${item.dev_id === this.selectedCamera ? "selected" : ""}>${this.escape(item.name)}</option>`).join("")}
            </select>
          </label>
          ${this.data?.on_demand ? `<div class="field date-field">
            <span>Date</span>
            <button id="date" class="date-button" type="button" aria-haspopup="dialog" aria-expanded="${this.calendarOpen ? "true" : "false"}" ${this.loading ? "disabled" : ""}>
              <span>${this.escape(this.formatDisplayDate(this.selectedDate))}</span><ha-icon icon="mdi:calendar-month-outline"></ha-icon>
            </button>
            ${this.renderCalendar(camera)}
          </div>` : `<label>
            Date
            <select id="date" ${!camera?.dates?.length ? "disabled" : ""}>
              ${(camera?.dates || []).map((date) => `<option value="${this.escape(date)}" ${date === this.selectedDate ? "selected" : ""}>${this.escape(date)}</option>`).join("")}
            </select>
          </label>`}
          <div class="status">${this.escape(this.statusText(camera, clipCount))}</div>
          <button id="refresh" class="icon-button" type="button" title="Refresh recordings" aria-label="Refresh recordings" ${this.loading ? "disabled" : ""}><ha-icon icon="mdi:refresh"></ha-icon></button>
        </div>
        ${this.error ? `<div class="error">${this.escape(this.error)}</div>` : ""}
        ${!this.error ? this.renderBody(clips) : ""}
      </div>
    `;
    this.bindEvents();
    this.afterRender();
  }

  renderCalendar(camera) {
    if (!this.calendarOpen) return "";
    const selected = this.parseLocalDate(this.selectedDate) || new Date();
    const monthMatch = /^(\d{4})-(\d{2})$/.exec(this.calendarMonth);
    const year = monthMatch ? Number(monthMatch[1]) : selected.getFullYear();
    const month = monthMatch ? Number(monthMatch[2]) - 1 : selected.getMonth();
    const firstWeekday = new Date(year, month, 1).getDay();
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const available = new Set(camera?.dates || []);
    const todayKey = this.localDateString(new Date());
    const weekdays = Array.from({ length: 7 }, (_, index) => new Intl.DateTimeFormat(
      this._hass?.locale?.language || undefined,
      { weekday: "narrow" },
    ).format(new Date(2026, 7, 2 + index)));
    const cells = Array.from({ length: 42 }, (_, index) => {
      const day = index - firstWeekday + 1;
      if (day < 1 || day > daysInMonth) return '<span class="calendar-blank"></span>';
      const key = this.localDateString(new Date(year, month, day));
      const classes = ["calendar-day"];
      if (available.has(key)) classes.push("has-recordings");
      if (key === this.selectedDate) classes.push("selected");
      return `<button class="${classes.join(" ")}" type="button" data-date="${key}" ${key > todayKey ? "disabled" : ""} aria-label="${this.escape(this.formatDisplayDate(key))}">${day}</button>`;
    }).join("");
    const title = new Intl.DateTimeFormat(this._hass?.locale?.language || undefined, {
      month: "long",
      year: "numeric",
    }).format(new Date(year, month, 1));
    return `<div class="calendar-popover" role="dialog" aria-label="Choose recording date">
      <div class="calendar-header">
        <button id="calendar-previous" type="button" title="Previous month" aria-label="Previous month"><ha-icon icon="mdi:chevron-left"></ha-icon></button>
        <strong>${this.escape(title)}</strong>
        <button id="calendar-next" type="button" title="Next month" aria-label="Next month"><ha-icon icon="mdi:chevron-right"></ha-icon></button>
      </div>
      <div class="calendar-grid">${weekdays.map(day => `<span class="calendar-weekday">${this.escape(day)}</span>`).join("")}${cells}</div>
    </div>`;
  }

  renderBody(clips) {
    if (this.loading && !this.data) {
      return `<div class="empty">Loading recordings...</div>`;
    }
    if (!this.data?.cameras?.length) {
      return `<div class="empty">No cameras found.</div>`;
    }
    if (this.timelineMode) {
      return this.renderTimelineBody(this.recordingRanges);
    }
    if (!clips.length) {
      return `<div class="empty">No recordings for this date.</div>`;
    }
    return `
      <div class="clips">
        ${clips.map((clip) => this.renderClip(clip)).join("")}
      </div>
    `;
  }

  renderTimelineBody(ranges) {
    const selected = this.selectedClip
      && this.selectedClip.dev_id === this.selectedCamera
      && this.selectedClip.date === this.selectedDate
      ? this.selectedClip
      : null;
    const preview = selected || ranges[ranges.length - 1] || null;
    const dayStart = new Date(`${this.selectedDate}T00:00:00`).getTime() / 1000;
    const selectedTime = this.timelineInitialTime(ranges, dayStart);
    const player = selected?.signed_playback_url
      ? this.renderPlayer(selected, "timeline-video")
      : preview
        ? `<button id="timeline-play" class="timeline-poster" type="button" aria-label="Play from ${this.escape(this.formatClipTime(preview))}" ${preview.playback_url ? "" : "disabled"}>
            ${preview.signed_thumbnail_url ? `<img src="${this.escape(preview.signed_thumbnail_url)}" alt="">` : '<ha-icon icon="mdi:cctv"></ha-icon>'}
            <span class="play-mark"><ha-icon icon="mdi:play-circle"></ha-icon></span>
          </button>`
        : '<div class="timeline-poster timeline-empty-player" role="img" aria-label="No recordings for this date"><ha-icon icon="mdi:cctv-off"></ha-icon><span>No recordings for this date</span></div>';
    return `<section class="timeline-view" aria-label="SD card timeline">
      <div class="timeline-player">${player}</div>
      <div class="timeline-shell">
        <div class="timeline-heading"><strong>${this.escape(this.formatDisplayDate(this.selectedDate))}</strong><span id="timeline-clock">${this.escape(this.formatTime(selectedTime))}</span></div>
        <div class="time-ruler-wrap">
          <div id="recording-timeline" class="time-ruler" role="slider" tabindex="0" aria-label="Recording timeline" aria-valuemin="0" aria-valuemax="86400">
            <div class="time-ruler-content">
              ${this.renderTimelineHours()}
              <div class="recording-lane">${this.renderRecordingSegments(ranges)}</div>
            </div>
          </div>
          <div class="timeline-playhead" aria-hidden="true"></div>
        </div>
        <div class="timeline-legend">${ranges.length ? '<i></i><span>Recorded video</span><span>Drag to choose a time</span>' : '<span>No recorded video on this day</span>'}</div>
      </div>
    </section>`;
  }

  timelineInitialTime(ranges, dayStart) {
    const selected = this.selectedClip
      && this.selectedClip.dev_id === this.selectedCamera
      && this.selectedClip.date === this.selectedDate
      ? this.selectedClip
      : null;
    if (selected?.play_time) return selected.play_time;
    const latest = ranges.reduce(
      (current, range) => !current || range.end > current.end ? range : current,
      null,
    );
    if (this.selectedDate !== this.localDateString(new Date())) {
      return latest?.start || dayStart + 43200;
    }
    const now = Math.min(Date.now() / 1000, dayStart + 86399);
    // Keep a current day parked at "now", but do not hide every segment when
    // the camera's latest catalog response is hours behind the clock.
    return latest && now - latest.end > 15 * 60 ? latest.start : now;
  }

  renderTimelineHours() {
    return Array.from({ length: 25 }, (_, hour) => {
      const label = new Intl.DateTimeFormat(this._hass?.locale?.language || undefined, {
        hour: "numeric",
        hour12: this._hass?.locale?.time_format !== "24",
      }).format(new Date(2000, 0, 1, hour === 24 ? 0 : hour));
      return `<span class="timeline-hour" style="left:${(hour / 24) * 100}%">${this.escape(hour === 24 ? label.replace(/12(?=\s)/, "12") : label)}</span>`;
    }).join("");
  }

  renderRecordingSegments(ranges) {
    const dayStart = new Date(`${this.selectedDate}T00:00:00`).getTime() / 1000;
    const dayEnd = dayStart + 86400;
    return ranges.map((range) => {
      const start = Math.max(dayStart, Number(range.start));
      const end = Math.min(dayEnd, Number(range.end));
      const left = ((start - dayStart) / 86400) * 100;
      const width = Math.max(0.14, ((end - start) / 86400) * 100);
      return `<button class="recording-segment" type="button" data-start="${range.start}" data-end="${range.end}" style="left:${left}%;width:${width}%" aria-label="Play recording at ${this.escape(this.formatTime(range.start))}"></button>`;
    }).join("");
  }

  renderStats(stats) {
    if (!stats?.cache_only) return "";
    const sync = stats.sync || {};
    const ready = Number(stats.ready_clips) || 0;
    const pending = Math.max(0, Number(stats.pending_clips) || 0);
    const storage = Number(stats.total_bytes) || 0;
    const syncState = this.friendlySyncState(sync, pending);
    return `<section class="cache-summary" aria-label="Local video cache">
      <div><ha-icon icon="mdi:download-box-outline"></ha-icon><strong>${this.escape(this.formatNumber(ready))}</strong><span>saved clips</span></div>
      <div><ha-icon icon="mdi:harddisk"></ha-icon><strong>${this.escape(this.formatBytes(storage))}</strong><span>local storage</span></div>
      <div><ha-icon icon="mdi:sync"></ha-icon><span>${this.escape(syncState)}</span></div>
    </section>`;
  }

  withReadyThumbnails(data) {
    for (const camera of data?.cameras || []) {
      if (camera.playback_mode === "timeline") {
        camera.recording_ranges = camera.recording_ranges || [];
        camera.dates = [...new Set(camera.recording_ranges.map((item) => item.date).filter(Boolean))].sort().reverse();
        continue;
      }
      camera.clips = (camera.clips || []).filter(
        (clip) => clip.thumbnail_cached && clip.thumbnail_url,
      );
      camera.dates = [...new Set(camera.clips.map((clip) => clip.date).filter(Boolean))].sort().reverse();
    }
    return data;
  }

  renderPauseToggle() {
    if (!this.data?.stats?.cache_only) return "";
    const paused = this.cameraActivityPaused;
    const available = this.pauseEntityAvailable;
    const label = paused ? "Resume" : "Pause";
    const title = paused ? "Resume Tuya Recordings camera activity" : "Pause Tuya Recordings camera activity";
    return `<button id="pause-toggle" class="pause-toggle" type="button" title="${this.escape(title)}" aria-label="${this.escape(title)}" aria-pressed="${paused ? "true" : "false"}" ${!available || this.pauseToggling ? "disabled" : ""}>
      <ha-icon icon="${paused ? "mdi:camera-off-outline" : "mdi:camera-outline"}"></ha-icon>
      <span class="pause-label">${this.escape(label)}</span>
    </button>`;
  }

  renderClip(clip) {
    const isPlaying = this.selectedClip && this.selectedClip.dev_id === clip.dev_id && this.selectedClip.start === clip.start && this.selectedClip.end === clip.end;
    return `
      <div class="clip" role="button" aria-label="Play recording at ${this.escape(this.formatClipTime(clip))}" tabindex="0" data-start="${clip.start}" data-end="${clip.end}" ${isPlaying ? "playing" : ""}>
        <div class="thumb">
          ${
            isPlaying
              ? (clip.signed_playback_url ? this.renderPlayer(clip, "clip-video") : '<div class="no-video">Preparing playback...</div>')
              : `${clip.signed_thumbnail_url ? `<img src="${this.escape(clip.signed_thumbnail_url)}" loading="lazy" alt="">` : `<div class="missing-thumb"><ha-icon icon="mdi:cctv"></ha-icon></div>`}<span class="play-mark"><ha-icon icon="mdi:play-circle"></ha-icon></span><span class="badge">${this.escape(this.formatDuration(clip.duration))}</span>`
          }
        </div>
        <div class="meta">
          <div class="title">${this.escape(this.formatClipTime(clip))}</div>
          <div class="sub">${isPlaying ? "Now playing" : this.data?.stats?.cache_only && clip.cached ? "Saved locally" : this.escape(this.cameraName(clip.dev_id))}</div>
        </div>
      </div>
    `;
  }

  bindEvents() {
    this.shadowRoot.getElementById("camera")?.addEventListener("change", async (event) => {
      this.stopPlayback();
      this.selectedCamera = event.target.value;
      this.calendarOpen = false;
      this.calendarMonth = "";
      if (!this.data?.on_demand) this.selectedDate = this.camera?.dates?.[0] || "";
      this.selectedClip = null;
      if (this.data?.on_demand) return this.loadData();
      await this.signVisibleClips();
      this.render();
    });
    const dateControl = this.shadowRoot.getElementById("date");
    if (this.data?.on_demand) {
      dateControl?.addEventListener("click", () => {
        this.calendarOpen = !this.calendarOpen;
        if (this.calendarOpen && !this.calendarMonth) {
          this.calendarMonth = this.selectedDate.slice(0, 7);
        }
        this.render();
      });
      this.shadowRoot.getElementById("calendar-previous")?.addEventListener("click", () => this.shiftCalendarMonth(-1));
      this.shadowRoot.getElementById("calendar-next")?.addEventListener("click", () => this.shiftCalendarMonth(1));
      this.shadowRoot.querySelectorAll(".calendar-day[data-date]").forEach((button) => {
        button.addEventListener("click", async () => {
          this.stopPlayback();
          this.selectedDate = button.dataset.date;
          this.selectedClip = null;
          this.calendarOpen = false;
          this.calendarMonth = this.selectedDate.slice(0, 7);
          await this.loadData();
        });
      });
    } else {
      dateControl?.addEventListener("change", async (event) => {
        this.stopPlayback();
        this.selectedDate = event.target.value;
        this.selectedClip = null;
        await this.signVisibleClips();
        this.render();
      });
    }
    this.shadowRoot.getElementById("refresh")?.addEventListener("click", () => {
      this.stopPlayback();
      this.loadData();
    });
    this.shadowRoot.getElementById("pause-toggle")?.addEventListener("click", () => this.toggleCameraActivityPause());
    this.shadowRoot.getElementById("timeline-play")?.addEventListener("click", async () => {
      const ranges = this.recordingRanges;
      const range = ranges[ranges.length - 1];
      if (range) await this.openTimelineRange(range, range.start);
    });
    this.shadowRoot.querySelectorAll(".clip").forEach((button) => {
      const open = async (event) => {
        if (event?.target?.closest?.("video, .camera-player")) return;
        const start = Number(button.dataset.start);
        const end = Number(button.dataset.end);
        const clip = this.clips.find((item) => item.start === start && item.end === end);
        if (clip) {
          await this.openClip(clip);
        }
      };
      button.addEventListener("click", open);
      button.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        open(event);
      });
    });
  }

  async afterRender() {
    void this.setupTimeline();
    const video = this.shadowRoot.querySelector(".camera-player video");
    if (!video) return;
    const revision = this.playbackRevision;
    try {
      this.debugPlayback("player-import", this.selectedClip);
      const { attachCameraPlayer } = await import('/tuya_recordings_static/camera-player.js?v=20261002-timeline-controller-2');
      if (revision !== this.playbackRevision || !video.isConnected) return;
      this.disposePlayer?.();
      this.disposePlayer = null;
      const autoplay = this.autoplaySelectedClip;
      this.autoplaySelectedClip = false;
      this.debugPlayback("player-attach", this.selectedClip, { autoplay });
      this.disposePlayer = attachCameraPlayer(video, this.selectedClip, path => this.signPath(path), {
        autoplay,
        mode: this.timelineMode ? "timeline" : "clip",
        initialOffset: this.timelineMode ? Math.max(0, Number(this.selectedClip.play_time || this.selectedClip.start) - this.selectedClip.start) : 0,
        onPosition: epoch => this.updateTimelinePlayhead(epoch),
        debug: detail => this.debugPlayback("playback", this.selectedClip, detail),
      });
    } catch (error) {
      this.debugPlayback("player-error", this.selectedClip, { message: error?.message || String(error) });
      console.error("Tuya Recordings player could not load", error);
      if (video.isConnected) {
        video.removeAttribute('src');
        video.load();
        video.parentElement.querySelector('[role="status"]').textContent = `Player could not load: ${error?.message || error || 'unknown error'}`;
      }
    }
  }

  renderPlayer(clip, id) {
    if (!clip.signed_playback_url) return '<div class="no-video">Playback unavailable</div>';
    const isControlledStream = clip.stream_transport === "native" && !clip.cached;
    const source = isControlledStream ? "" : ` src="${this.escape(clip.signed_playback_url)}"`;
    const preload = isControlledStream ? "none" : "metadata";
    const video = `<video id="${id}" class="clip-player" ${clip.signed_thumbnail_url ? `poster="${this.escape(clip.signed_thumbnail_url)}"` : ""} playsinline disablepictureinpicture disableremoteplayback controlsList="nodownload noplaybackrate noremoteplayback" preload="${preload}"${source}></video>`;
    return `<div class="camera-player">${video}<div class="player-status" role="status">Tap play to start</div></div>`;

  }

  statusText(camera, clipCount) {
    if (this.loading && !this.data) return "";
    if (!camera) return "No camera selected";
    const online = camera.online ? "online" : "offline";
    const refresh = this.data?.catalog_refresh_pending ? " • Updating recordings" : "";
    return `${clipCount} recording${clipCount === 1 ? "" : "s"} • Camera ${online}${refresh}`;
  }

  async signVisibleClips() {
    const clips = this.timelineMode ? this.recordingRanges : this.clips;
    await Promise.all(
      clips.map((clip) => this.signClip(clip, { playback: false, thumbnail: true }))
    );
  }

  async signClip(clip, { playback = true, thumbnail = true } = {}) {
    if (playback && clip.playback_url) {
      if (clip.cached) {
        clip.signed_playback_url = await this.signPath(clip.playback_url);
      } else {
        const separator = clip.playback_url.includes("?") ? "&" : "?";
        const transport = clip.stream_transport || "native";
        const mode = this.timelineMode ? "timeline" : "clip";
        clip.signed_playback_url = await this.signPath(`${clip.playback_url}${separator}transport=${encodeURIComponent(transport)}&mode=${mode}`);
      }
    }
    if (thumbnail && clip.thumbnail_url) {
      clip.signed_thumbnail_url = await this.signPath(clip.thumbnail_url);
    }
  }

  async openClip(clip) {
    this.stopPlayback();
    const revision = this.playbackRevision;
    this.error = "";
    this.autoplaySelectedClip = false;
    this.debugPlayback("click", clip);
    try {
      await this.signClip(clip, { playback: true, thumbnail: true });
      if (revision !== this.playbackRevision || !this.isConnected) return;
      this.selectedClip = clip;
      this.selectedCamera = clip.dev_id;
      this.selectedDate = clip.date || this.selectedDate;
      this.autoplaySelectedClip = true;
      this.debugPlayback("signed", clip);
      this.render();
    } catch (error) {
      console.error("Tuya Recordings could not open clip", error);
      this.debugPlayback("sign-error", clip, { message: error?.message || String(error) });
      if (revision !== this.playbackRevision || !this.isConnected) return;
      this.selectedClip = null;
      this.autoplaySelectedClip = false;
      this.error = `Playback setup failed: ${error?.message || error || "unknown error"}`;
      this.render();
    }
  }

  debugPlayback(phase, clip, detail = null) {
    if (!this._hass || !clip) return;
    this._hass.callApi("POST", "tuya_recordings/debug", {
      phase,
      dev_id: clip.dev_id,
      start: clip.start,
      end: clip.end,
      detail,
    }).catch(() => undefined);
  }

  closeViewer() {
    this.stopPlayback();
    this.selectedClip = null;
    this.render();
  }

  syncRouteFromHash() {
    const hash = window.location.hash.replace(/^#/, "");
    const clipParam = new URLSearchParams(hash).get("clip");
    if (!clipParam || !this.data) {
      if (!clipParam) this.selectedClip = null;
      return;
    }
    const [devId, startRaw, endRaw] = clipParam.split(":");
    const clip = this.findClip(devId, Number(startRaw), Number(endRaw));
    if (!clip) return;
    this.selectedClip = clip;
    this.selectedCamera = clip.dev_id;
    this.selectedDate = clip.date || this.selectedDate;
  }

  findClip(devId, start, end) {
    for (const camera of this.data?.cameras || []) {
      const clip = (camera.clips || []).find((item) => item.dev_id === devId && item.start === start && item.end === end);
      if (clip) return clip;
    }
    return null;
  }

  cameraName(devId) {
    return (this.data?.cameras || []).find((camera) => camera.dev_id === devId)?.name || devId;
  }

  isMobileViewport() {
    return window.matchMedia?.("(max-width: 900px), (pointer: coarse)")?.matches || false;
  }

  async signPath(path) {
    const cached = this.signedPaths.get(path);
    if (cached && cached.expires > Date.now()) {
      return cached.path;
    }
    const request = this._hass.connection.sendMessagePromise({
      type: "auth/sign_path",
      path,
      expires: 3600,
    });
    const timeout = new Promise((_, reject) => {
      setTimeout(() => reject(new Error(`Timed out signing ${path}`)), 8000);
    });
    const result = await Promise.race([request, timeout]);
    if (this.signedPaths.size >= 512) this.signedPaths.delete(this.signedPaths.keys().next().value);
    this.signedPaths.set(path, { path: result.path, expires: Date.now() + 3500 * 1000 });
    return result.path;
  }

  async openTimelineRange(range, playTime) {
    if (!range.playback_url) {
      this.error = this.cameraActivityPaused
        ? "Camera activity is paused. Resume it to play SD-card recordings."
        : "This camera is not currently available for SD-card playback.";
      this.render();
      return;
    }
    const clip = { ...range, play_time: Math.max(range.start, Math.min(range.end - 1, Math.floor(playTime))) };
    if (this.disposePlayer?.playClip) {
      this.selectedClip = clip;
      this.error = "";
      this.debugPlayback("click", clip);
      this.disposePlayer.playClip(clip, clip.play_time - clip.start);
      this.timeline?.setTime?.(clip.play_time);
      return;
    }
    await this.openClip(clip);
  }

  async setupTimeline() {
    const container = this.shadowRoot.getElementById("recording-timeline");
    if (!container || !this.timelineMode) return;
    const ranges = this.recordingRanges;
    const content = container.querySelector(".time-ruler-content");
    const dayStart = new Date(`${this.selectedDate}T00:00:00`).getTime() / 1000;
    if (!content || !Number.isFinite(dayStart)) return;
    this.timeline?.destroy?.();
    let scrollFrame = 0;
    let dragging = false;
    let dragMoved = false;
    let suppressClick = false;
    let pointerId = null;
    let dragStartX = 0;
    let dragStartScroll = 0;
    let followPlayback = true;
    const scrollWidth = () => Math.max(1, container.scrollWidth - container.clientWidth);
    const epochAtPlayhead = () => dayStart + (container.scrollLeft / scrollWidth()) * 86400;
    const applyTime = (epoch) => {
      container.scrollLeft = Math.max(0, Math.min(scrollWidth(), ((epoch - dayStart) / 86400) * scrollWidth()));
      const clock = this.shadowRoot.getElementById("timeline-clock");
      if (clock) clock.textContent = this.formatTime(epoch);
      container.setAttribute("aria-valuenow", String(Math.max(0, Math.min(86400, Math.round(epoch - dayStart)))));
    };
    const setTime = (epoch) => {
      if (!dragging && followPlayback) applyTime(epoch);
    };
    const onScroll = () => {
      cancelAnimationFrame(scrollFrame);
      scrollFrame = requestAnimationFrame(() => setTime(epochAtPlayhead()));
    };
    const onKeyDown = (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'PageUp', 'PageDown'].includes(event.key)) return;
      event.preventDefault();
      const direction = event.key === 'ArrowLeft' || event.key === 'PageDown' ? -1 : 1;
      const step = event.key.startsWith('Page') ? 3600 : 600;
      applyTime(epochAtPlayhead() + direction * step);
    };
    const epochAtPointer = (clientX) => {
      const bounds = container.getBoundingClientRect();
      const offsetFromPlayhead = clientX - bounds.left - (bounds.width / 2);
      const scroll = Math.max(0, Math.min(scrollWidth(), container.scrollLeft + offsetFromPlayhead));
      return dayStart + (scroll / scrollWidth()) * 86400;
    };
    const playAt = async (epoch) => {
      const range = this.rangeForTime(epoch, ranges);
      if (range && range.start <= epoch && epoch < range.end) {
        await this.openTimelineRange(range, epoch);
      }
    };
    const onPointerDown = (event) => {
      if (event.button !== undefined && event.button !== 0) return;
      // The user's hand owns the camera while scrubbing. Relinquish the old
      // stream so its time updates cannot pull the ruler away mid-drag.
      this.disposePlayer?.pause?.();
      this.selectedClip = null;
      followPlayback = false;
      dragging = true;
      dragMoved = false;
      pointerId = event.pointerId;
      dragStartX = event.clientX;
      dragStartScroll = container.scrollLeft;
      container.dataset.dragging = "true";
      try {
        container.setPointerCapture?.(event.pointerId);
      } catch (_error) {
        // Synthetic tests and older WebViews can expose the API without an active pointer.
      }
    };
    const onPointerMove = (event) => {
      if (!dragging || event.pointerId !== pointerId) return;
      const delta = dragStartX - event.clientX;
      if (Math.abs(delta) >= 4) dragMoved = true;
      if (!dragMoved) return;
      event.preventDefault();
      container.scrollLeft = Math.max(0, Math.min(scrollWidth(), dragStartScroll + delta));
    };
    const removeGlobalPointerListeners = () => {
      window.removeEventListener("pointermove", onPointerMove, true);
      window.removeEventListener("pointerup", finishPointer, true);
      window.removeEventListener("pointercancel", finishPointer, true);
    };
    const finishPointer = async (event) => {
      if (!dragging || event.pointerId !== pointerId) return;
      const moved = dragMoved;
      dragging = false;
      pointerId = null;
      removeGlobalPointerListeners();
      delete container.dataset.dragging;
      try {
        if (container.hasPointerCapture?.(event.pointerId)) container.releasePointerCapture(event.pointerId);
      } catch (_error) {
        // The browser may release capture before delivering pointercancel.
      }
      if (!moved) return;
      suppressClick = true;
      const epoch = epochAtPlayhead();
      applyTime(epoch);
      await playAt(epoch);
      queueMicrotask(() => { suppressClick = false; });
    };
    const addGlobalPointerListeners = () => {
      removeGlobalPointerListeners();
      window.addEventListener("pointermove", onPointerMove, { capture: true, passive: false });
      window.addEventListener("pointerup", finishPointer, true);
      window.addEventListener("pointercancel", finishPointer, true);
    };
    const onSegmentClick = async (event) => {
      if (suppressClick) {
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      const button = event.target.closest(".recording-segment");
      if (!button) {
        this.disposePlayer?.pause?.();
        this.selectedClip = null;
        followPlayback = false;
        const epoch = epochAtPointer(event.clientX);
        applyTime(epoch);
        await playAt(epoch);
        return;
      }
      event.stopPropagation();
      followPlayback = false;
      const range = ranges.find((item) => item.start === Number(button.dataset.start) && item.end === Number(button.dataset.end));
      if (!range) return;
      const bounds = button.getBoundingClientRect();
      const fraction = Math.max(0, Math.min(1, (event.clientX - bounds.left) / Math.max(1, bounds.width)));
      const epoch = range.start + ((range.end - range.start) * fraction);
      applyTime(epoch);
      await this.openTimelineRange(range, epoch);
    };
    container.addEventListener("scroll", onScroll, { passive: true });
    container.addEventListener("keydown", onKeyDown);
    container.addEventListener("pointerdown", onPointerDown);
    container.addEventListener("pointermove", onPointerMove);
    container.addEventListener("pointerup", finishPointer);
    container.addEventListener("pointercancel", finishPointer);
    container.addEventListener("lostpointercapture", finishPointer);
    container.addEventListener("click", onSegmentClick);
    container.addEventListener("pointerdown", addGlobalPointerListeners);
    this.timeline = {
      setTime,
      followPlayback: (epoch) => {
        if (dragging || !this.selectedClip) return;
        followPlayback = true;
        applyTime(epoch);
      },
      destroy: () => {
        cancelAnimationFrame(scrollFrame);
        container.removeEventListener("scroll", onScroll);
        container.removeEventListener("keydown", onKeyDown);
        container.removeEventListener("pointerdown", onPointerDown);
        container.removeEventListener("pointermove", onPointerMove);
        container.removeEventListener("pointerup", finishPointer);
        container.removeEventListener("pointercancel", finishPointer);
        container.removeEventListener("lostpointercapture", finishPointer);
        container.removeEventListener("click", onSegmentClick);
        container.removeEventListener("pointerdown", addGlobalPointerListeners);
        removeGlobalPointerListeners();
      },
    };
    applyTime(this.timelineInitialTime(ranges, dayStart));
  }

  rangeForTime(epoch, ranges = this.recordingRanges) {
    return ranges.find((range) => range.start <= epoch && epoch < range.end)
      || ranges.find((range) => range.start >= epoch)
      || ranges[ranges.length - 1]
      || null;
  }

  updateTimelinePlayhead(epoch) {
    if (!Number.isFinite(epoch) || !this.selectedClip) return;
    const clock = this.shadowRoot.getElementById("timeline-clock");
    if (clock) clock.textContent = this.formatTime(epoch);
    this.timeline?.followPlayback?.(epoch);
  }

  get pauseEntityId() {
    return "switch.tuya_recordings_pause_camera_activity";
  }

  get pauseEntity() {
    return this._hass?.states?.[this.pauseEntityId];
  }

  get pauseEntityAvailable() {
    const state = this.pauseEntity?.state;
    return state === "on" || state === "off";
  }

  get cameraActivityPaused() {
    return (this.pauseStateOverride || this.pauseEntity?.state) === "on";
  }

  async toggleCameraActivityPause() {
    if (!this._hass || !this.pauseEntityAvailable || this.pauseToggling) return;
    const targetPaused = !this.cameraActivityPaused;
    this.pauseToggling = true;
    this.pauseStateOverride = targetPaused ? "on" : "off";
    this.render();
    try {
      await this._hass.callService("switch", targetPaused ? "turn_on" : "turn_off", {
        entity_id: this.pauseEntityId,
      });
      if (targetPaused) {
        this.stopPlayback();
      }
      await this.loadData();
    } catch (error) {
      this.pauseStateOverride = null;
      this.error = `Could not update camera activity pause: ${error?.message || error || "unknown error"}`;
    } finally {
      this.pauseToggling = false;
      this.render();
    }
  }

  formatClipTime(clip) {
    const start = this.formatTime(clip.start);
    const end = this.formatTime(clip.end);
    return `${start} - ${end}`;
  }

  formatTime(epochSeconds) {
    const locale = this._hass?.locale || {};
    const options = {
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
    };
    if (locale.time_format === "12") {
      options.hour12 = true;
    } else if (locale.time_format === "24") {
      options.hour12 = false;
    }
    return new Date(Number(epochSeconds) * 1000).toLocaleTimeString(locale.language || undefined, {
      ...options,
    });
  }

  formatDuration(seconds) {
    const total = Number(seconds) || 0;
    const minutes = Math.floor(total / 60);
    const rest = total % 60;
    if (!minutes) return `${rest}s`;
    return `${minutes}m ${String(rest).padStart(2, "0")}s`;
  }

  formatDisplayDate(value) {
    if (!value) return "Recordings";
    const date = new Date(`${value}T12:00:00`);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat(this._hass?.locale?.language || undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
      year: "numeric",
    }).format(date);
  }

  parseLocalDate(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
    if (!match) return null;
    const result = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
    return Number.isNaN(result.getTime()) ? null : result;
  }

  shiftCalendarMonth(offset) {
    const selected = this.parseLocalDate(`${this.calendarMonth || this.selectedDate.slice(0, 7)}-01`) || new Date();
    selected.setMonth(selected.getMonth() + offset);
    this.calendarMonth = this.localDateString(selected).slice(0, 7);
    this.render();
  }

  localDateString(value) {
    const year = value.getFullYear();
    const month = String(value.getMonth() + 1).padStart(2, "0");
    const day = String(value.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  formatNumber(value) {
    return new Intl.NumberFormat(this._hass?.locale?.language || undefined).format(Number(value) || 0);
  }

  formatBytes(bytes) {
    const size = Number(bytes) || 0;
    if (size < 1024) return `${size} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let value = size / 1024;
    let unit = units[0];
    for (let index = 1; index < units.length && value >= 1024; index += 1) {
      value /= 1024;
      unit = units[index];
    }
    const digits = value >= 10 ? 1 : 2;
    return `${value.toFixed(digits)} ${unit}`;
  }

  readyDetail(pending) {
    if (pending > 0) return `${this.formatNumber(pending)} still syncing`;
    return "All caught up";
  }

  latestRecordingValue(latest) {
    if (!latest?.start) return "None yet";
    return this.formatTime(latest.start);
  }

  latestRecordingDetail(latest) {
    if (!latest?.start) return "No recordings available";
    const duration = this.formatDuration(latest.duration);
    return `${latest.camera_name || "Camera"} • ${duration}`;
  }

  cameraCountValue(online, total) {
    if (!total) return "0";
    return `${this.formatNumber(online)}/${this.formatNumber(total)}`;
  }

  cameraCountDetail(online, total) {
    if (!total) return "No cameras found";
    if (online === total) return "All cameras online";
    if (!online) return "All cameras offline";
    return `${this.formatNumber(total - online)} offline`;
  }

  friendlySyncState(sync, pending) {
    const state = String(sync?.state || sync?.status || "idle").toLowerCase();
    const failed = Number(sync?.failed) || 0;
    if (failed > 0) return `${this.formatNumber(failed)} clip${failed === 1 ? "" : "s"} need another try`;
    if (state === "running") return "Syncing new videos";
    if (state === "stalled") return "Sync needs attention";
    if (pending > 0) return "Catching up in background";
    return "Recordings are ready";
  }

  escape(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }
}

if (!customElements.get("tuya-recordings-panel")) {
  customElements.define("tuya-recordings-panel", TuyaRecordingsPanel);
}
if (!customElements.get("tuya-recordings-panel-v2")) {
  customElements.define("tuya-recordings-panel-v2", class extends TuyaRecordingsPanel {});
}
