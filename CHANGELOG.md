# Changelog

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
