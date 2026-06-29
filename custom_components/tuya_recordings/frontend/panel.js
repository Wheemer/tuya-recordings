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
    this.handleRouteChange = async () => {
      this.syncRouteFromHash();
      if (this.selectedClip) {
        await this.signClip(this.selectedClip);
      }
      this.render();
    };
  }

  set hass(value) {
    this._hass = value;
    if (!this.data && !this.loading) {
      this.loadData();
    }
  }

  connectedCallback() {
    window.addEventListener("hashchange", this.handleRouteChange);
    window.addEventListener("popstate", this.handleRouteChange);
    this.syncRouteFromHash();
    this.render();
  }

  disconnectedCallback() {
    window.removeEventListener("hashchange", this.handleRouteChange);
    window.removeEventListener("popstate", this.handleRouteChange);
  }

  async loadData() {
    if (!this._hass) return;
    this.loading = true;
    this.error = "";
    this.render();
    try {
      const data = await this._hass.callApi("GET", "tuya_recordings/panel");
      this.data = data;
      const cameras = data.cameras || [];
      if (!this.selectedCamera || !cameras.some((camera) => camera.dev_id === this.selectedCamera)) {
        this.selectedCamera = cameras[0]?.dev_id || "";
      }
      const camera = this.camera;
      if (!this.selectedDate || !camera?.dates?.includes(this.selectedDate)) {
        this.selectedDate = camera?.dates?.[0] || "";
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

  render() {
    const cameras = this.data?.cameras || [];
    const camera = this.camera;
    const clips = this.clips;
    const clipCount = clips.length;
    const viewerClip = this.selectedClip && this.findClip(this.selectedClip.dev_id, this.selectedClip.start, this.selectedClip.end);
    const useViewerPage = viewerClip && !this.isMobileViewport();
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          min-height: 100vh;
          color: var(--primary-text-color);
          background: var(--primary-background-color);
          box-sizing: border-box;
          font-family: var(--paper-font-body1_-_font-family, Roboto, Arial, sans-serif);
        }
        * { box-sizing: border-box; }
        .page {
          max-width: 1400px;
          margin: 0 auto;
          padding: 16px;
        }
        .toolbar {
          display: grid;
          grid-template-columns: minmax(180px, 260px) minmax(150px, 220px) 1fr auto;
          gap: 12px;
          align-items: end;
          margin-bottom: 16px;
        }
        label {
          display: grid;
          gap: 6px;
          color: var(--secondary-text-color);
          font-size: 13px;
        }
        select, button {
          height: 40px;
          border: 1px solid var(--divider-color);
          border-radius: 6px;
          background: var(--card-background-color);
          color: var(--primary-text-color);
          font: inherit;
        }
        select { padding: 0 10px; }
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
          grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
          gap: 10px;
        }
        .clip {
          display: grid;
          grid-template-rows: auto 1fr;
          min-height: 188px;
          overflow: hidden;
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          background: var(--card-background-color);
          cursor: pointer;
          text-align: left;
          padding: 0;
          font: inherit;
        }
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
          padding: 10px;
        }
        .title {
          font-size: 14px;
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
          min-height: 220px;
          background: #000;
          display: block;
        }
        .clip-progress {
          display: none;
          height: 5px;
          background: var(--divider-color);
          overflow: hidden;
        }
        .clip[playing] .clip-progress {
          display: block;
        }
        .clip-progress-bar {
          width: 0%;
          height: 100%;
          background: var(--primary-color);
          transition: width 160ms linear;
        }
        .viewer {
          display: grid;
          gap: 12px;
          max-width: 1100px;
          margin: 0 auto;
        }
        .viewer-bar {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
        }
        .viewer-title {
          min-width: 0;
          display: grid;
          gap: 3px;
        }
        .viewer-title .title {
          font-size: 16px;
        }
        .viewer-details {
          display: grid;
          gap: 4px;
          padding: 0 2px;
        }
        .viewer-frame {
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          background: #000;
          overflow: hidden;
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
        @media (max-width: 900px) {
          .page {
            padding: 12px;
          }
          .toolbar {
            grid-template-columns: 1fr;
          }
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
            border-color: var(--primary-color);
          }
          .viewer {
            gap: 14px;
          }
          .viewer-bar {
            align-items: flex-start;
          }
          .viewer-frame {
            overflow: visible;
          }
          .viewer-details {
            padding-bottom: 10px;
          }
          video, .no-video {
            min-height: min(58vw, 360px);
          }
        }
      </style>
      <div class="page">
        ${useViewerPage ? "" : `${this.renderStats(this.data?.stats)}<div class="toolbar">
          <label>
            Camera
            <select id="camera" ${cameras.length === 0 ? "disabled" : ""}>
              ${cameras.map((item) => `<option value="${this.escape(item.dev_id)}" ${item.dev_id === this.selectedCamera ? "selected" : ""}>${this.escape(item.name)}</option>`).join("")}
            </select>
          </label>
          <label>
            Date
            <select id="date" ${!camera?.dates?.length ? "disabled" : ""}>
              ${(camera?.dates || []).map((date) => `<option value="${this.escape(date)}" ${date === this.selectedDate ? "selected" : ""}>${this.escape(date)}</option>`).join("")}
            </select>
          </label>
          <div class="status">${this.escape(this.statusText(camera, clipCount))}</div>
          <button id="refresh" type="button">${this.loading ? "Loading" : "Refresh"}</button>
        </div>`}
        ${this.error ? `<div class="error">${this.escape(this.error)}</div>` : ""}
        ${!this.error ? (useViewerPage ? this.renderViewer(viewerClip) : this.renderBody(clips)) : ""}
      </div>
    `;
    this.bindEvents();
    this.afterRender();
  }

  renderBody(clips) {
    if (this.loading && !this.data) {
      return `<div class="empty">Loading recordings...</div>`;
    }
    if (!this.data?.cameras?.length) {
      return `<div class="empty">No cameras found.</div>`;
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

  renderStats(stats) {
    if (!stats) return "";
    const sync = stats.sync || {};
    const ready = Number(stats.ready_clips) || 0;
    const indexed = Number(stats.indexed_clips) || 0;
    const pending = Math.max(0, Number(stats.pending_clips) || 0);
    const videos = Number(stats.cached_videos) || 0;
    const thumbs = Number(stats.cached_thumbnails) || 0;
    const storage = Number(stats.total_bytes) || 0;
    const videoBytes = Number(stats.video_bytes) || 0;
    const thumbBytes = Number(stats.thumbnail_bytes) || 0;
    const state = this.formatSyncState(sync);
    return `
      <div class="stats-grid">
        <div class="stat-card">
          <div class="stat-label">Cache Ready</div>
          <div class="stat-value">${this.escape(this.formatNumber(ready))}/${this.escape(this.formatNumber(indexed))}</div>
          <div class="stat-sub">${this.escape(this.formatNumber(pending))} pending</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Cached Files</div>
          <div class="stat-value">${this.escape(this.formatNumber(videos))}</div>
          <div class="stat-sub">${this.escape(this.formatNumber(thumbs))} thumbnails</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Storage Used</div>
          <div class="stat-value">${this.escape(this.formatBytes(storage))}</div>
          <div class="stat-sub">${this.escape(this.formatBytes(videoBytes))} video • ${this.escape(this.formatBytes(thumbBytes))} thumbs</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Sync Status</div>
          <div class="stat-value">${this.escape(state)}</div>
          <div class="stat-sub">${this.escape(this.formatSyncDetail(sync))}</div>
        </div>
      </div>
    `;
  }

  renderViewer(clip) {
    return `
      <div class="viewer">
        <div class="viewer-bar">
          <button id="back" type="button">Back</button>
          <div class="viewer-title"></div>
        </div>
        <div class="viewer-frame">
          ${clip.signed_playback_url ? `<video id="viewer-video" controls autoplay playsinline disablepictureinpicture disableremoteplayback controlsList="nodownload noplaybackrate noremoteplayback" preload="auto" src="${this.escape(clip.signed_playback_url)}"></video>` : `<div class="no-video">Not cached yet</div>`}
        </div>
        <div class="viewer-details">
          <div class="title">${this.escape(this.cameraName(clip.dev_id))} • ${this.escape(this.formatClipTime(clip))}</div>
          <div class="sub">${this.escape(this.formatDuration(clip.duration))} • ${clip.cached ? "Cached" : "Not cached"}</div>
        </div>
      </div>
    `;
  }

  renderClip(clip) {
    const isPlaying = this.selectedClip && this.selectedClip.dev_id === clip.dev_id && this.selectedClip.start === clip.start && this.selectedClip.end === clip.end;
    return `
      <div class="clip" role="button" tabindex="0" data-start="${clip.start}" data-end="${clip.end}" ${isPlaying ? "playing" : ""}>
        <div class="thumb">
          ${
            isPlaying && clip.signed_playback_url
              ? `<video id="mobile-video" class="clip-player" controls autoplay playsinline disablepictureinpicture disableremoteplayback controlsList="nodownload noplaybackrate noremoteplayback" preload="auto" src="${this.escape(clip.signed_playback_url)}"></video>`
              : `${clip.signed_thumbnail_url ? `<img src="${this.escape(clip.signed_thumbnail_url)}" loading="lazy" alt="">` : `<div class="missing-thumb">No thumbnail</div>`}<span class="badge">${this.escape(this.formatDuration(clip.duration))}</span>`
          }
        </div>
        <div class="clip-progress"><div id="mobile-progress-bar" class="clip-progress-bar"></div></div>
        <div class="meta">
          <div class="title">${this.escape(this.formatClipTime(clip))}</div>
          <div class="sub">${clip.cached ? "Cached" : "Not cached"}</div>
        </div>
      </div>
    `;
  }

  bindEvents() {
    this.shadowRoot.getElementById("camera")?.addEventListener("change", async (event) => {
      this.selectedCamera = event.target.value;
      this.selectedDate = this.camera?.dates?.[0] || "";
      this.selectedClip = null;
      await this.signVisibleClips();
      this.render();
    });
    this.shadowRoot.getElementById("date")?.addEventListener("change", async (event) => {
      this.selectedDate = event.target.value;
      this.selectedClip = null;
      await this.signVisibleClips();
      this.render();
    });
    this.shadowRoot.getElementById("refresh")?.addEventListener("click", () => this.loadData());
    this.shadowRoot.getElementById("back")?.addEventListener("click", () => this.closeViewer());
    this.shadowRoot.querySelectorAll(".clip").forEach((button) => {
      const open = async (event) => {
        if (event?.target?.closest?.("video")) return;
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

  afterRender() {
    const video = this.shadowRoot.getElementById("viewer-video") || this.shadowRoot.getElementById("mobile-video");
    if (!video) return;
    this.bindMobileProgress(video);
    video.play?.().catch(() => undefined);
  }

  bindMobileProgress(video) {
    if (video.id !== "mobile-video") return;
    const bar = this.shadowRoot.getElementById("mobile-progress-bar");
    if (!bar) return;
    const update = () => {
      const duration = Number(video.duration) || 0;
      const current = Number(video.currentTime) || 0;
      const progress = duration > 0 ? Math.min(100, Math.max(0, (current / duration) * 100)) : 0;
      bar.style.width = `${progress}%`;
    };
    video.addEventListener("timeupdate", update);
    video.addEventListener("progress", update);
    video.addEventListener("loadedmetadata", update);
    video.addEventListener("seeked", update);
    update();
  }

  statusText(camera, clipCount) {
    if (this.loading && !this.data) return "";
    if (!camera) return "No camera selected";
    const online = camera.online ? "online" : "offline";
    const total = (camera.clips || []).filter((clip) => clip.date === this.selectedDate).length;
    return `${clipCount} playable of ${total} clips • ${online}`;
  }

  async signVisibleClips() {
    const clips = this.clips;
    await Promise.all(
      clips.map((clip) => this.signClip(clip))
    );
  }

  async signClip(clip) {
    if (clip.playback_url) {
      clip.signed_playback_url = await this.signPath(clip.playback_url);
    }
    if (clip.thumbnail_url) {
      clip.signed_thumbnail_url = await this.signPath(clip.thumbnail_url);
    }
  }

  async openClip(clip) {
    this.selectedCamera = clip.dev_id;
    this.selectedDate = clip.date || this.selectedDate;
    if (this.isMobileViewport()) {
      if (!clip.signed_playback_url) {
        await this.signClip(clip);
      }
      this.selectedClip = clip;
      this.render();
      return;
    }
    this.selectedClip = clip;
    await this.signClip(clip);
    const params = new URLSearchParams();
    params.set("clip", `${clip.dev_id}:${clip.start}:${clip.end}`);
    window.history.pushState(null, "", `${window.location.pathname}${window.location.search}#${params.toString()}`);
    this.render();
  }

  closeViewer() {
    this.selectedClip = null;
    window.history.pushState(null, "", `${window.location.pathname}${window.location.search}`);
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
    if (this.signedPaths.has(path)) {
      return this.signedPaths.get(path);
    }
    const result = await this._hass.connection.sendMessagePromise({
      type: "auth/sign_path",
      path,
      expires: 3600,
    });
    this.signedPaths.set(path, result.path);
    return result.path;
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

  formatSyncState(sync) {
    const state = String(sync?.state || sync?.status || "idle").replaceAll("_", " ");
    return state.charAt(0).toUpperCase() + state.slice(1);
  }

  formatSyncDetail(sync) {
    if (!sync || Object.keys(sync).length === 0) return "No sync activity yet";
    const downloaded = Number(sync.downloaded) || 0;
    const skipped = Number(sync.skipped) || 0;
    const failed = Number(sync.failed) || 0;
    const total = Number(sync.total) || 0;
    const parts = [];
    if (total) parts.push(`${this.formatNumber(total)} checked`);
    if (downloaded) parts.push(`${this.formatNumber(downloaded)} new`);
    if (skipped) parts.push(`${this.formatNumber(skipped)} skipped`);
    if (failed) parts.push(`${this.formatNumber(failed)} failed`);
    if (sync.current) parts.push(`now ${sync.current}`);
    return parts.length ? parts.join(" • ") : "Waiting for next sync";
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

customElements.define("tuya-recordings-panel", TuyaRecordingsPanel);
