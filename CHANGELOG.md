# Changelog

## 0.3.0-beta.1

First public beta.

- Adds Tuya SD-card recording discovery through the Tuya IPC/OpenAPI path.
- Reuses credentials from LocalTuya instead of asking users to enter Tuya Cloud keys again.
- Uses the official Home Assistant Tuya integration as the camera inventory.
- Adds cached MP4 playback through Home Assistant Media Browser and a custom Tuya Recordings panel.
- Adds optional background pre-cache mode with private video storage and generated thumbnails.
- Adds cache/status sensors, refresh/sync/thumbnail services, and repair issues for missing prerequisites.
- Bundles Linux helper binaries for amd64, arm64, and armv7 Home Assistant installs.
