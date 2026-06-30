<div align="center">

# Tuya Recordings

### SD-card recording browser and cache for Tuya / Smart Life cameras in Home Assistant

  <img src="custom_components/tuya_recordings/brand/forum-logo.png" alt="Tuya Recordings logo" width="520">

[![HACS Custom](https://img.shields.io/badge/HACS-CUSTOM-41BDF5?style=for-the-badge&logo=home-assistant&logoColor=white&labelColor=555555)](https://github.com/hacs/integration)
[![Home Assistant Custom Integration](https://img.shields.io/badge/HOME%20ASSISTANT-CUSTOM%20INTEGRATION-41BDF5?style=for-the-badge&logo=home-assistant&logoColor=white&labelColor=555555)](https://www.home-assistant.io/)
[![Latest release](https://img.shields.io/github/v/release/Wheemer/tuya-recordings?include_prereleases&style=for-the-badge&logo=github&logoColor=white&label=RELEASE&labelColor=555555&color=22C55E)](https://github.com/Wheemer/tuya-recordings/releases)
[![Downloads](https://img.shields.io/github/downloads/Wheemer/tuya-recordings/total?style=for-the-badge&logo=github&logoColor=white&label=DOWNLOADS&labelColor=555555&color=8A2BE2)](https://github.com/Wheemer/tuya-recordings/releases)

</div>

## Overview

Tuya Recordings is a Home Assistant custom integration for viewing and caching
Tuya / Smart Life camera SD-card recordings.

It is built for cameras that already belong to a working Home Assistant Tuya
setup. The official Home Assistant `tuya` integration remains the camera
inventory because that is where most users already have live video working.
Tuya Recordings then reuses Tuya Cloud credentials saved by `localtuya` and
talks to Tuya's IPC recording path to discover, cache, thumbnail, and play
SD-card clips from Home Assistant.

This is an early public beta. Tuya camera firmware and cloud APIs vary by model
and region, so bug reports should include the camera model, integration version,
diagnostics, relevant Home Assistant logs, and whether the recording is playable
in the Tuya or Smart Life app.

## Features

- Tuya / Smart Life SD-card recording discovery through Tuya IPC/OpenAPI.
- Cached MP4 playback through Home Assistant Media Browser.
- Custom Tuya Recordings panel with camera/date browsing, thumbnails, cache
  statistics, storage usage, and sync status.
- Optional background pre-cache mode for instant playback of cached clips.
- Optional thumbnail sync mode for backup browsing without keeping full videos.
- Tapo-style on-demand playback when pre-cache is disabled.
- Generated thumbnails from cached MP4 recordings or short thumbnail samples.
- Private storage path under `/media`, avoiding public `/config/www` files.
- Home Assistant services for refresh, media sync, thumbnail population, and
  cache clearing.
- Status sensors plus pre-cache and thumbnail sync switches.
- Repair issues when official Tuya or LocalTuya prerequisites are missing.
- Event-assisted sync hints from matching Tuya/LocalTuya entities, with normal
  polling as the fallback.
- Bundled Linux helper binaries for `amd64`, `arm64`, and `armv7`.

## Requirements

- A functional Home Assistant installation.
- HACS, or manual access to `/config/custom_components`.
- Official Home Assistant `tuya` integration configured for the same Tuya /
  Smart Life account.
- `localtuya` configured with Tuya Cloud credentials saved in its config entry.
- A Tuya Developer project linked to the same account.
- Tuya Developer API services needed for Tuya account/device lookup and
  IPC/WebRTC camera access.
- `ffmpeg` available on the Home Assistant system.
- Tuya / Smart Life cameras with SD cards and local recordings.

The integration will not set up unless both `tuya` and `localtuya` are present.
LocalTuya must have `client_id`, `client_secret`, and `user_id` saved. Tuya
Recordings uses those credentials instead of asking users to enter the same Tuya
Developer values again.

Bundled playback helpers are included for Linux `amd64`, `arm64`, and `armv7`.
Other platforms need a compatible helper binary built from
`tools/pion_offer_probe`.

### Tuya Developer API Service

If LocalTuya already works and can find your devices, Tuya Recordings should
only need one extra Tuya Cloud API service:

| Add this Tuya service | Why Tuya Recordings needs it |
| --- | --- |
| `IoT Video Live Stream` | Provides the WebRTC IPC configuration and video resource pack used by Tuya's camera playback signaling path. |

That is the main Tuya Developer service to add/check for this integration.

If you are setting up LocalTuya from scratch, follow LocalTuya's own Tuya Cloud
setup first. LocalTuya commonly needs these baseline services before Tuya
Recordings is involved:

| LocalTuya baseline service | Why LocalTuya needs it |
| --- | --- |
| `Industry Basic Service` | Basic Tuya device/project access used by LocalTuya. |
| `Smart Home Basic Service` | Lists the devices linked to the Smart Life / Tuya account. |
| `Device Status Notification` | Tuya device status/event support used by LocalTuya setups. |

`Video Cloud Storage` is not required for normal Tuya Recordings use. This
integration caches SD-card recordings from the camera playback path; it does not
play Tuya's paid cloud-storage recordings.

Depending on the Tuya project age and region, Tuya may also show already
authorized default services such as `Device Status Notification`, `Data
Dashboard Service`, `Industry Basic Service`, or `[Deprecate]Smart Home Scene
Linkage`. Those are common on Tuya / LocalTuya projects, but they are not the
camera recording playback path.

The important endpoint coverage is:

- `GET /v1.0/users/{uid}/devices/{device_id}/webrtc-configs`
- `POST /v2.0/open-iot-hub/access/config`

If setup or playback reports `permission deny`, `No permissions`, or a Tuya API
permission error, first confirm that `IoT Video Live Stream` is subscribed and
authorized on the same Tuya project LocalTuya is using. Then make sure the
project is linked to the same Smart Life / Tuya app account and the correct data
center. If the `open-iot-hub` endpoint is denied, also confirm the project's
normal LocalTuya/Tuya cloud services and IoT Core subscription are still active.

____________________________________________________________

## Installation via HACS

[![Open your Home Assistant instance and add this repository to HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Wheemer&repository=tuya-recordings&category=integration)

1. **Open HACS in Home Assistant**

   HACS is the easiest way to install and update custom integrations.

2. **Add this repository as a custom integration repository**

   Repository URL:

   ```text
   https://github.com/Wheemer/tuya-recordings
   ```

   Category:

   ```text
   Integration
   ```

3. **Download Tuya Recordings**

   Find **Tuya Recordings** in HACS, download it, and follow the HACS restart
   prompt.

4. **Add the integration**

   In Home Assistant, go to:

   ```text
   Settings > Devices & services > Add integration > Tuya Recordings
   ```

____________________________________________________________

## Manual Installation

Copy the integration folder into Home Assistant:

```text
/config/custom_components/tuya_recordings
```

Restart Home Assistant, then add **Tuya Recordings** from:

```text
Settings > Devices & services > Add integration
```

____________________________________________________________

## Configuration

During setup, choose:

- **Tuya OpenAPI region**: The region used by your Tuya Developer project.
- **Private video storage path**: Use a private path under `/media`, such as
  `/media/tuya_recordings`.
- **Recording order**: Newest-first or oldest-first browsing.
- **Pre-cache recordings**: Download recordings in the background.
- **Preload thumbnails**: Create thumbnail previews in the background without
  keeping full video files.
- **Sync window in hours**: Use `0` to sync every discovered SD-card recording.

Do not use `/config/www`. Cached recordings should not be public web files.

### Playback Modes

**On-demand mode**

Recordings are listed and downloaded only when selected. This is closest to the
Tapo-style model.

**Thumbnail backup mode**

Leave **Pre-cache recordings** off and turn **Preload thumbnails** on. Tuya
Recordings keeps the SD-card index and thumbnail previews, while full video
clips are downloaded only when opened. This is the best mode when Frigate is the
primary recorder and Tuya Recordings is only a backup SD-card browser.

**Pre-cache mode**

Recordings are downloaded in the background. The custom panel and Media Browser
show only clips that are cached and thumbnailed, so playback should start
quickly.

### Storage Changes

Default storage path:

```text
/media/tuya_recordings
```

Changing the storage path does not move existing cached files. Move the existing
`videos` and `thumbs` folders first if you want old clips to remain available
from the new path.

____________________________________________________________

## How It Works

Tuya cameras do not expose SD-card recordings as simple local files. Tuya
Recordings uses Tuya IPC/WebRTC signaling to ask the camera for each clip, then
remuxes the incoming H264 media into an MP4 that Home Assistant can play.

Normal flow:

1. Discover Tuya IPC cameras from the Tuya account.
2. Query SD-card recording days and clips.
3. Download a clip through Tuya IPC playback when needed.
4. Save the MP4 under the private storage path when full pre-cache or playback
   is requested.
5. Generate thumbnails from cached MP4 files or from short temporary H264
   samples.
6. Serve cached or on-demand media through the custom panel and Home Assistant
   Media Browser.

### Automatic Sync Hints

When matching Home Assistant camera entities already exist, Tuya Recordings uses
them as hints that a new SD-card clip may be available.

For configured Tuya recording cameras, it watches:

- camera display status sensors
- LocalTuya SD storage sensors
- matching Tuya camera event entities

When one changes, the integration waits briefly for the camera to finish writing
the clip, then queues media and thumbnail sync. A cooldown prevents motion bursts
from hammering the camera or Tuya API. Regular polling remains the fallback.

____________________________________________________________

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
- The selected storage path has enough free space.
- The camera is online and has an SD card with recordings.

If clips list but playback is slow, enable pre-cache.

If clips do not appear quickly after motion, confirm matching Tuya or LocalTuya
camera entities exist in Home Assistant. Event-assisted sync is optional and
falls back to polling.

If setup reports missing LocalTuya credentials, open LocalTuya options and make
sure Tuya Cloud credentials are saved for the same Tuya account used by the
official Tuya integration.

____________________________________________________________

## We Need Your Help

Tuya camera behavior varies across models, regions, firmware, and account
features. Good beta feedback makes the integration better for everyone.

Helpful reports include:

1. Camera model and firmware version.
2. Tuya Recordings version.
3. Home Assistant version and install type.
4. Tuya OpenAPI region.
5. Whether the same clip plays in the Tuya or Smart Life app.
6. Diagnostics and relevant Home Assistant log lines.

Use GitHub Issues for bug reports and compatibility notes:

https://github.com/Wheemer/tuya-recordings/issues

## Complete Reference

### Integration boundaries

- Official `tuya` provides the account/camera graph and is the source of truth
  for which cameras should be handled.
- `localtuya` provides Tuya Cloud credentials and optional local camera entities.
- Tuya Recordings handles SD-card discovery, caching, thumbnails, and playback.
- Pairing, removing, sharing, firmware updates, SD-card formatting, cloud
  subscription management, and account management remain in the Tuya / Smart
  Life app.

### LocalTuya notes

Tuya Recordings can create repair issues when prerequisites are missing or when
official Tuya camera entities are not represented in LocalTuya. These issues are
there to help explain why faster local recording-sync hints may not be available
for every camera.
