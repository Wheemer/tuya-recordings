# Changelog

## Tuya Recordings v0.4.1.2 (2026-10-04)

Corrective Tuya Smart setup and HACS package release.

### Fixes

- Uses the tuyaSmart QR payload accepted by the Tuya Smart app.
- Packages the HACS release ZIP with the integration files at archive root,
  preventing a nested custom_components/tuya_recordings install path.

### Validation

- 485 automated tests pass on Linux with Home Assistant's Python 3.14 runtime.
- The Tuya Smart QR payload was confirmed by the first Tuya Smart account tester.

## Tuya Recordings v0.4.1.1 (2026-10-04)

Tuya Smart account authorization pre-release.

### Highlights

- Adds an **Account app** choice during setup: **Smart Life** or **Tuya Smart**.
- Uses the selected app's APK-derived QR authorization, encrypted gateway
  identity, device-key discovery, and MQTT signaling identity.
- Preserves existing installations by retaining **Smart Life** as their saved
  account-app default.
- Updates setup text, reauthentication errors, and documentation to describe
  the selected account app accurately.

### Validation

- 485 automated tests pass on Linux with Home Assistant's Python 3.14 runtime.
- The current Tuya Smart QR-token request was verified against the Tuya mobile
  gateway. This pre-release needs a real Tuya Smart-account discovery and
  playback check before a stable release.

## Tuya Recordings v0.4.1 (2026-10-03)

Detection-sensor reliability and release validation fix.

### Highlights

- Adds a configurable **Detection reset time** (default: 60 seconds) for the
  existing **Motion detected** and **Person detected** binary sensors. A newer
  matching camera notification restarts the interval; explicit camera clears
  still clear immediately.
- Keeps the existing motion and person entity names unchanged.
- Restores the offline Smart Life native-library inspector required by its test
  and stops `.gitignore` from excluding that tracked source file.

### Validation

- 481 automated tests pass on Linux with Home Assistant's Python 3.14 runtime.

## Tuya Recordings v0.4.0 (2026-10-03)

Native Smart Life SD-card playback redesign.

### Highlights

- Replaces the experimental OpenAPI, WebRTC/Pion, and helper-binary paths with
  an in-process implementation of the Smart Life APK's LAN-first protocol-302
  signaling, bounded MQTT fallback, AES/KCP relay, recording catalog,
  recording-thumbnail, and playback protocols.
- Uses Tuya frame 32 over one serialized, non-reconnecting LAN socket when the
  camera advertises LAN P2P policy and is locally discoverable; MQTT is opened
  only when that bounded LAN discovery/connect attempt is unavailable.
- Adds Smart Life QR authorization directly to the integration. LocalTuya and
  a Tuya Developer project are no longer required.
- Restores notification-driven motion and person binary sensors alongside the
  camera event entity. The event entity is an additional automation/history
  surface, not a replacement for the binary sensors.
- Streams encoded camera video and audio without transcoding, with inline Plyr
  controls for play, progress, volume, mute, and fullscreen.
- Makes uncached browsing match Smart Life's playback model: one camera/day
  wall-clock timeline, orange recording ranges, one active native session, and
  wall-clock seeking without artificial clip-boundary stops.
- Keeps the bounded thumbnail-and-clip library for optional full-video caching,
  with the two playback contracts separated in both the API and frontend.
- Makes Home Assistant Media Browser cached-file-only so browsing media cannot
  start an uncached camera session or receive the native timeline stream.
- Queries Smart Life's capability-gated event timeline and retrieves recording
  JPEGs with the exact event bounds expected by `downloadPlaybackEventImageV2`,
  serialized through one process-wide camera-command queue.
- Keeps `PB_V21` limited to its actual purpose, selecting the ordinary V3
  playback catalog. It is not treated as the thumbnail capability.
- Derives thumbnails for ordinary timeline fragments from a bounded prefix of
  the APK-native H.264 playback stream, stopping immediately after the first
  decodable frame and retaining no temporary video.
- Builds missing thumbnails from an already-cached MP4 before considering any
  camera operation, using atomic validated JPEG publication.
- Keeps local cached-file thumbnail extraction independent from camera/catalog
  backoff without clearing a genuine remote camera cooldown.
- Keeps optional full-video caching while making catalog and thumbnail upkeep
  the default lightweight browsing mode.
- Pauses background camera work while on-demand playback is active and drains
  worker sessions cleanly during cancellation and integration unload.
- Lets disconnected per-camera workers sleep until real work or shutdown instead
  of polling an empty queue indefinitely.
- Adds bounded catalog/thumbnail failure backoff, duplicate-thumbnail
  single-flight protection, and credential-safe diagnostic representations.
- Removes packaged executables, obsolete transport modules, stale browser
  harnesses, and the LocalTuya runtime dependency.
- Aligns the integration manifest with the non-beta `0.4.0` release and adds
  deterministic pytest discovery, Ruff CI, and local brand-asset validation.

### Validation

- 475 automated tests pass on Linux with Home Assistant's Python 3.14 runtime.
- Synthetic Chromium tests verify native H.264 plus PCM audio playback and the
  cached panel at desktop, 390 px, and 320 px widths, plus the uncached
  wall-clock timeline and continuous playback past a catalog boundary, without
  contacting a camera.
- Python compilation, JSON parsing, JavaScript syntax, packaging, secret, and
  obsolete-transport checks pass.

## Tuya Recordings v0.3.0-beta.6

Discovery and API compatibility hardening after additional report triage.

### Highlights

- Improve camera discovery so entries are identified by camera-like category, camera-like
  naming hints, previous cache, and full-device fallback so users with newer
  Tuya camera categories still get results.
- Normalize camera/device IDs from multiple API response keys (`id`, `deviceId`,
  `device_id`, and `devId`) across discovery and playback flows.
- Expand Tuya OpenAPI `get_devices` parsing so wrapped results (`{"result": [...]}`,
  `{"list": [...]}`, or `{"devices": [...]}`) are supported.
- Keep the cached clip sensor thread-safe while preserving paused-state panel and
  media-browser behavior.

### Validation

- Full validation passed through `tools/validate.ps1`.
- Test suite passed: 119 tests.

## Tuya Recordings v0.3.0-beta.4

Compatibility and safety cleanup after the first public install reports.

### Highlights

- Fixes a Home Assistant config-flow crash on 2026.7.x caused by a non-serializable
  custom validator in the media storage path field.
- Keeps media storage path validation, but now reports normal field errors
  instead of breaking the config flow form.
- Tightens **Pause Tuya camera cloud activity** so the panel and Media Browser
  hide uncached clips while paused instead of presenting items that would fail
  when selected.
- Adds regression tests for Home Assistant frontend schema serialization and
  paused cached-only browsing behavior.

### Validation

- Full validation passed through `tools/validate.ps1`.
- Test suite passed: 119 tests.

## Tuya Recordings v0.3.0-beta.3

Safety update for unstable Tuya cameras.

### Highlights

- Adds a **Pause Tuya camera cloud activity** switch.
- When paused, Tuya Recordings serves cached media only and stops manual
  refreshes, scheduled polling, media sync, thumbnail sync, HA camera-triggered
  sync dispatches, uncached clip downloads, and missing-thumbnail refresh paths.
- Adds pause status to diagnostics, the cached clip sensor attributes, and the
  panel stats.
- Fixes a Home Assistant 2026 thread-safety issue where the cached clip sensor
  could call `async_write_ha_state` from a worker thread.

### Validation

- Full validation passed through `tools/validate.ps1`.
- Test suite passed: 112 tests.

## Tuya Recordings v0.3.0-beta.2

Backup-focused beta update for installs that mainly want SD-card visibility
while Frigate or another NVR handles primary detection.

### Highlights

- Adds Thumbnail Sync mode. This keeps SD-card clips visible only after a
  thumbnail is ready, without requiring the full video cache to stay enabled.
- Preserves existing thumbnails when switching away from full media pre-cache.
- Adds a `tuya_recordings.clear_video_cache` service that deletes cached MP4
  video files while keeping the recording index and thumbnails.
- Keeps Media Browser and the Tuya Recordings panel aligned with the backup
  mode: thumbnail-backed clips stay browsable, video playback remains
  on-demand.
- Improves the storage cleanup path so cached videos can be removed by Home
  Assistant Core from the same private media folder the integration uses.

### Validation

- Full validation passed through `tools/validate.ps1`.
- Test suite passed: 106 tests.

## Tuya Recordings v0.3.0-beta.1

First public beta of Tuya Recordings for Home Assistant.

### Highlights

- Adds Tuya / Smart Life SD-card recording discovery through the Tuya
  IPC/OpenAPI path.
- Reuses Tuya Cloud credentials from LocalTuya instead of asking users to enter
  the same Tuya Developer values again.
- Uses the official Home Assistant Tuya integration as the camera inventory and
  source of truth.
- Adds cached MP4 playback through Home Assistant Media Browser.
- Adds a custom Tuya Recordings panel with camera/date browsing, thumbnails,
  cache statistics, storage usage, and sync status.
- Adds optional background pre-cache mode with private video storage and
  generated thumbnails.
- Supports Tapo-style on-demand playback when pre-cache is disabled.
- Adds cache/status sensors, a pre-cache switch, refresh/sync/thumbnail
  services, and repair issues for missing prerequisites.
- Bundles Linux helper binaries for `amd64`, `arm64`, and `armv7` Home
  Assistant installs.

### Requirements

- Official Home Assistant `tuya` integration.
- `localtuya` configured with Tuya Cloud credentials.
- Tuya Developer project linked to the same Tuya / Smart Life account.
- Tuya video/IPC APIs authorized for the project.
- `ffmpeg` available on the Home Assistant system.
- Tuya / Smart Life cameras with SD cards and recordings.

### Notes

- This is a public beta. Tuya camera support can vary by model, firmware,
  account, region, and enabled Tuya Developer APIs.
- Pre-cache mode only shows clips after both video and thumbnail are ready, so
  playback should start quickly.
- On-demand mode lists discovered clips and caches them when selected.
- Cached media should be stored under a private `/media` path, not
  `/config/www`.

### Validation

- Full validation passed through `tools/validate.ps1`.
- Test suite passed: 104 tests.

