<div align="center">

<img src="https://raw.githubusercontent.com/Wheemer/tuya-recordings/main/custom_components/tuya_recordings/brand/forum-logo.png" alt="Tuya Recordings" width="520">

[![HACS Custom](https://img.shields.io/badge/HACS-CUSTOM-41BDF5?style=for-the-badge&logo=home-assistant&logoColor=white&labelColor=555555)](https://github.com/hacs/integration)
[![Home Assistant Custom Integration](https://img.shields.io/badge/HOME%20ASSISTANT-CUSTOM%20INTEGRATION-41BDF5?style=for-the-badge&logo=home-assistant&logoColor=white&labelColor=555555)](https://www.home-assistant.io/)
[![Latest release](https://img.shields.io/github/v/release/Wheemer/tuya-recordings?include_prereleases&style=for-the-badge&logo=github&logoColor=white&label=RELEASE&labelColor=555555&color=22C55E)](https://github.com/Wheemer/tuya-recordings/releases)
[![Downloads](https://img.shields.io/github/downloads/Wheemer/tuya-recordings/total?style=for-the-badge&logo=github&logoColor=white&label=DOWNLOADS&labelColor=555555&color=8A2BE2)](https://github.com/Wheemer/tuya-recordings/releases)

</div>

## What It Does

Tuya Recordings adds SD-card recording playback to Home Assistant for compatible
Smart Life and Tuya Smart cameras. Playback follows the selected account app's
native camera protocol, rather than using Tuya Cloud video storage or a
separate camera bridge. Tuya Recordings discovers compatible cameras during
its own QR-authorized account setup and runs SD playback through that same
native session.

By default, choose a camera and date, then use a wall-clock timeline to play
the recording at the selected time directly from the camera's SD card. Video
and audio are remuxed for the browser without video re-encoding, and no local
video copy is retained.

Optional local caching creates a private MP4 library for fast repeat playback
and Home Assistant Media Browser. It is off by default.

> [!IMPORTANT]
> This integration is for SD-card recordings. It does not replace the official
> Tuya camera entity, camera setup, live view, firmware management, or Tuya
> Cloud video storage.

## Features

- SD-card day and recording discovery through the Smart Life or Tuya Smart
  native playback protocol.
- Video and audio playback through one serialized camera session.
- A custom panel with camera picker, date picker, wall-clock timeline,
  recording ranges, seeking, and fullscreen controls.
- Person markers that appear only when an explicit camera person event falls
  inside that camera's matching SD-card recording range. The panel does not
  place markers from delayed Home Assistant receipt times.
- Optional local MP4 caching under a private `/media` folder.
- Cached recordings in Home Assistant Media Browser, with thumbnails rendered
  locally from the cached MP4 files.
- One **Camera events** entity per supported camera, preserving the original
  Tuya notification code and metadata in Home Assistant history.
- Separate **Motion detected** and **Person detected** binary sensors for each
  camera, classified from the same Tuya IPC notifications.
- A bounded, one-camera-at-a-time work queue for catalog refresh, playback,
  local thumbnail work, and optional caching.
- Smart Life or Tuya Smart QR authorization during setup. No Tuya Developer
  project, LocalTuya camera entry, separate bridge, sidecar, or bundled
  executable is required.

## Requirements

- Home Assistant.
- A Tuya / Smart Life camera with an SD card and recordings.
- The app that contains the cameras, used once during setup to approve the QR code.
- Home Assistant's `ffmpeg` integration. It remuxes the camera's existing
  encoded streams for browser playback; it does not transcode them.

The official Home Assistant **Tuya** integration and LocalTuya may coexist for
other controls, but neither is a Tuya Recordings requirement. Tuya Developer
OpenAPI services and Tuya Cloud video storage are also not required.

## Installation

### HACS

[![Open your Home Assistant instance and add this repository to HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Wheemer&repository=tuya-recordings&category=integration)

1. Open **HACS** in Home Assistant.
2. Add `https://github.com/Wheemer/tuya-recordings` as a custom repository in
   the **Integration** category.
3. Download **Tuya Recordings** and restart Home Assistant when HACS asks.
4. Go to **Settings > Devices & services > Add integration** and select
   **Tuya Recordings**.

### Pre-releases

When a prerelease is available, open **Settings > Devices & services > Entities**, find the disabled diagnostic **Pre-release** switch for the HACS Tuya Recordings repository, enable it, then turn it on. HACS will then offer the prerelease.

### Manual

Copy this repository's `custom_components/tuya_recordings` directory to:

```text
/config/custom_components/tuya_recordings
```

Restart Home Assistant, then add **Tuya Recordings** from **Settings > Devices
& services > Add integration**.

## Setup

The setup flow asks for:

- **Account region**
- **Account app**: **Smart Life** or **Tuya Smart**. Choose the app and account
  that actually owns the cameras.
- **Private video storage path**, normally `/media/tuya_recordings`
- **Recording order**
- **Pre-cache recordings**
- **Sync window in hours** when pre-caching is enabled
- **Detection reset time** (default: 60 seconds)

The flow then displays one QR code. Scan it with the selected account app,
approve the login, and submit the Home Assistant step. The code is not polled
or refreshed in the background. If it expires, restart setup to create a new
one.

Use a private `/media` location for cached files. Do not use `/config/www`.

### Options

After setup, **Configure** on the Tuya Recordings integration provides:

- **Days to check**: `0` scans up to 31 days and stops after 7 consecutive
  empty days. It does not enumerate the entire SD card. A higher value limits
  the scan to that number of days, up to 31.
- **Background catalog interval**: 15 minutes to 24 hours for timeline mode.
- **Pre-cache recordings** and **Sync window in hours** for local MP4 caching.
  A window of `0` means every discovered recording.
- **Private video storage path** and **Recording order**.
- **Detection reset time** for the motion and person binary sensors.

Changing the storage path takes effect immediately and does not move existing
files. Move the old `videos` and `thumbs` folders before confirming a new path
if those cached clips should remain available.

## Playback And Caching

### Default: SD-card timeline playback

With **Pre-cache recordings** off, the panel requests the camera's recording
catalog and plays the selected time directly from the SD card. It does not
download or retain video or thumbnails locally.

There is one active camera session at a time. Selecting a new time stops the
prior playback before the next one begins. Catalog refresh is serialized with
playback, and a playback request takes priority over a background catalog
refresh. The default catalog interval is 15 minutes.

Person markers are conservative by design: a marker is shown only when the
camera's explicit person-event timestamp falls within a matching native
SD-card recording range. A missing marker is preferable to a marker placed at
the wrong moment.

### Optional: local MP4 cache

Background sync is off by default and must be explicitly enabled. For
battery-powered cameras, leave it off unless you accept the battery cost
of repeated wake-ups and background downloads. Cameras on SD-status
cooldown are deferred until a later normal sync pass; cooldown does not
count as a failed recording download.

With **Pre-cache recordings** on, Tuya Recordings downloads the selected
SD-card recordings into the configured private storage path. Cached clips
appear in the panel and Home Assistant Media Browser. Thumbnails are rendered
locally from the cached MP4 files; no separate camera thumbnail request is
made.

Set the sync window to `0` only when the storage location has enough free
space for every discovered recording.

## Camera Detection Entities

For each supported Tuya camera notification source, Tuya Recordings creates:

These notification entities need a matching camera notification source from
the official Tuya integration. QR-authorized SD playback and discovery work
independently; without an official notification source these entities are not
created. Sources loaded after Tuya Recordings are discovered automatically.

- **Camera events**: a point-in-time event entity. It records `detected` or
  `cleared`, the original `tuya_event_code`, and camera-supplied metadata.
- **Motion detected**: turns on for supported IPC motion notifications.
- **Person detected**: turns on for supported IPC person notifications.

The binary sensors are notification-driven and do not poll the camera. A
matching detection turns the sensor on and restarts its configurable detection
reset timer. An explicit camera clear turns it off immediately; otherwise it
turns off after the configured reset time. A person notification can correctly
turn on both sensors, because a person is also motion.

Use the event entity when an automation needs the original camera event and
metadata. Use the binary sensors for Home Assistant history, dashboard state,
and ordinary motion/person automations.

## Services

| Service | Purpose |
| --- | --- |
| `tuya_recordings.refresh_recordings` | Refresh the SD-card recording catalog. |
| `tuya_recordings.sync_media` | Download recordings when pre-cache is enabled. |
| `tuya_recordings.populate_thumbnails` | Populate missing thumbnails for cached recordings. |
| `tuya_recordings.clear_cache` | Clear the recording index. |
| `tuya_recordings.clear_video_cache` | Delete cached video files while keeping the index and thumbnails. |

## Safety And Camera Activity

Camera work is serialized across Tuya Recordings; it never opens parallel
recording sessions for the same camera. Interactive playback takes priority
over background catalog and cache work.

**Pause camera activity** stops every camera-facing action: catalog refresh,
uncached SD playback, caching, and thumbnail work. The panel can still show
its existing metadata, and locally cached MP4 files remain playable. Turn it
on while a camera is recovering or whenever Tuya Recordings should be quiet.

## Troubleshooting

**No cameras or setup cannot continue**

- Confirm that the selected Smart Life or Tuya Smart account owns the camera.
- Confirm that the camera has an SD card with recordings in the selected
  Smart Life or Tuya Smart app.

**Camera authorization failed or expired**

- Reconfigure Tuya Recordings and complete a fresh QR authorization with the
  app selected in setup.

**Timeline playback is unavailable**

- Confirm the same time is playable in the selected account app.
- Confirm Home Assistant's `ffmpeg` integration is available.
- Confirm **Pause camera activity** is off. This switch intentionally blocks
  camera-backed catalog and timeline work.

**Person marker is missing or not expected**

- The marker requires both an explicit camera person-event time and a matching
  SD-card recording range. The integration deliberately does not guess from a
  delayed Home Assistant notification time.

**Cached clips do not appear**

- Enable **Pre-cache recordings** and confirm the selected storage path has
  sufficient free space.
- Use `tuya_recordings.sync_media` to request a cache pass.

## Changelog

Release history is maintained in [CHANGELOG.md](CHANGELOG.md).
