# Tuya Recordings

Home Assistant custom integration for viewing and caching Tuya / Smart Life
camera SD-card recordings.

This is an early public beta. It is useful today for testing Tuya IPC SD-card
recording playback, but Tuya camera firmware and cloud APIs vary by model and
region. Expect to file logs when a camera behaves differently.

Tuya Recordings is built for cameras that already belong to a working Home
Assistant Tuya setup. It treats the official Tuya integration as the camera
inventory because that is where most users already have live video working. It
then reuses LocalTuya cloud credentials and talks to Tuya's IPC recording path to
discover and cache SD-card clips as local MP4 files.

## Requirements

Install and configure these first:

- Official Home Assistant `tuya` integration
- `localtuya` custom integration with Tuya Cloud credentials saved
- A Tuya Developer project linked to the same Tuya / Smart Life account
- Tuya Developer APIs needed for IPC/WebRTC camera access
- `ffmpeg` available on the Home Assistant system

The integration will not set up unless both `tuya` and `localtuya` are present.
The official Tuya integration is used to discover which cameras should be
handled. LocalTuya must have `client_id`, `client_secret`, and `user_id` saved
in its config entry. Tuya Recordings uses those credentials instead of asking
users to enter the same Tuya Developer values again.

Bundled playback helpers are included for Linux `amd64`, `arm64`, and `armv7`.
Other platforms will need a compatible helper binary built from
`tools/pion_offer_probe`.

## Install

### HACS Custom Repository

1. In HACS, open **Custom repositories**.
2. Add this repository URL as an **Integration**.
3. Install **Tuya Recordings**.
4. Restart Home Assistant.
5. Add **Tuya Recordings** from
   **Settings > Devices & services > Add integration**.

### Manual Install

Copy `custom_components/tuya_recordings` into Home Assistant:

```text
/config/custom_components/tuya_recordings
```

Restart Home Assistant, then add **Tuya Recordings** from
**Settings > Devices & services > Add integration**.

## Setup

During setup choose:

- Tuya OpenAPI region
- Private video storage path
- Recording sort order
- Whether to pre-cache SD-card recordings
- Hours to keep synchronized, where `0` means all discovered SD-card recordings

Use a private storage path under `/media`, such as `/media/tuya_recordings`. Do
not use `/config/www`, because cached recordings should not be public web files.

## How It Works

Recordings are not exposed by these cameras as normal local files. Tuya
Recordings uses Tuya IPC/WebRTC signaling to ask the camera for each SD-card
clip, then remuxes the incoming H264 media into an MP4 that Home Assistant can
play.

The normal playback flow is:

1. Discover Tuya IPC cameras from the Tuya account.
2. Query SD-card recording days and clips.
3. Download a clip through Tuya IPC playback.
4. Save the MP4 under the private storage path.
5. Generate a thumbnail from the cached MP4.
6. Serve cached media through the custom panel and Home Assistant media source.

## Playback Modes

Without pre-cache, recordings are listed and downloaded when selected. This is
closest to the Tapo-style on-demand model.

With pre-cache enabled, the integration downloads recordings in the background.
The custom panel only shows clips after they are cached and thumbnailed, so
playback should start immediately.

## Automatic Sync Hints

If matching Home Assistant camera entities already exist, Tuya Recordings uses
them as hints that a new SD-card clip may be available.

For the configured Tuya recording cameras, it watches:

- camera display status sensors
- LocalTuya SD storage sensors
- matching Tuya camera event entities

When one changes, the integration waits briefly for the camera to finish writing
the clip, then queues the normal media and thumbnail sync. A cooldown prevents
motion bursts from hammering the camera or Tuya API.

This is an acceleration layer only. Regular polling remains the fallback.

## LocalTuya Repair Direction

The intended v0.3 setup model is:

- Official `tuya` provides the account/camera graph and is the source of truth
  for which cameras should be handled.
- `localtuya` provides Tuya Cloud credentials and optional local camera entities.
- Tuya Recordings handles SD-card discovery, caching, thumbnails, and playback.

Tuya Recordings now creates repair issues when prerequisites are missing or when
official Tuya camera entities are not represented in LocalTuya. Future repair
helpers should be guarded and explicit: start from the official Tuya camera list,
show the suggested LocalTuya camera config, back up the LocalTuya config entry,
then apply changes only when the integration can safely determine the camera
device ID, local key, protocol version, and LAN IP.

The integration should not silently patch another integration with guessed
camera IPs.

## Storage

Default storage path:

```text
/media/tuya_recordings
```

Changing the storage path does not move old cached files. Move the existing
videos and thumbnails first if you want old clips to remain instant-playback
ready, then update the path in options.

## Services

Available Home Assistant services:

- `tuya_recordings.refresh_recordings`
- `tuya_recordings.sync_media`
- `tuya_recordings.populate_thumbnails`
- `tuya_recordings.clear_cache`

## Troubleshooting

If setup fails, check:

- The official Tuya integration is installed and configured.
- LocalTuya is installed and configured with Tuya Cloud credentials.
- The Tuya Developer project is linked to the same account.
- Required Tuya video/IPC APIs are authorized.
- The selected storage path has enough space.
- The camera is online and has an SD card with recordings.

If clips list but playback is slow, enable pre-cache.

If clips do not appear quickly after motion, confirm matching Tuya or LocalTuya
camera entities exist in Home Assistant. The event-assisted sync is optional and
falls back to polling.

If setup reports missing LocalTuya credentials, open LocalTuya options and make
sure Tuya Cloud credentials are saved for the same Tuya account used by the
official Tuya integration.

## Development

Run validation from the repo root:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$env:PYTHONPATH=(Get-Location).Path
pytest tests -q
```

Build bundled helper binaries when needed:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_helpers.ps1
```

The bundled Linux helpers are built from `tools/pion_offer_probe` and the
patched Pion ICE source in `tools/pion_ice_patch`. See
`THIRD_PARTY_NOTICES.md` for license details.
