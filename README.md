<div align="center">

# Tuya Recordings

### Browse SD-card recordings from Tuya / Smart Life cameras in Home Assistant

<img src="https://raw.githubusercontent.com/Wheemer/tuya-recordings/main/custom_components/tuya_recordings/brand/forum-logo.png" alt="Tuya Recordings" width="520">

[![HACS Custom](https://img.shields.io/badge/HACS-CUSTOM-41BDF5?style=for-the-badge&logo=home-assistant&logoColor=white&labelColor=555555)](https://github.com/hacs/integration)
[![Home Assistant Custom Integration](https://img.shields.io/badge/HOME%20ASSISTANT-CUSTOM%20INTEGRATION-41BDF5?style=for-the-badge&logo=home-assistant&logoColor=white&labelColor=555555)](https://www.home-assistant.io/)
[![Latest release](https://img.shields.io/github/v/release/Wheemer/tuya-recordings?style=for-the-badge&logo=github&logoColor=white&label=RELEASE&labelColor=555555&color=22C55E)](https://github.com/Wheemer/tuya-recordings/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/Wheemer/tuya-recordings/total?style=for-the-badge&logo=github&logoColor=white&label=DOWNLOADS&labelColor=555555&color=8A2BE2)](https://github.com/Wheemer/tuya-recordings/releases)

<p>
  <strong>Smart Life SD-card playback, audio, optional private caching, and camera detection entities.</strong><br>
  Built to complement the official Home Assistant Tuya integration.
</p>

</div>

Tuya Recordings adds SD-card recording playback to Home Assistant for cameras
already set up in the official Home Assistant **Tuya** integration. It follows
the Smart Life mobile app's native recording path instead of relying on Tuya
Cloud video storage or a separate camera bridge.

By default, choose a camera and day, then use one wall-clock timeline to play
the SD-card recording at the selected time. Video and audio are remuxed for the
browser without re-encoding, and the integration does not retain a full local
copy.

Optional local caching is available for people who want a private MP4 library
and fast repeat playback. It is off by default.

<div style="border: 1px solid rgba(65, 189, 245, 0.45); border-radius: 8px; padding: 16px 18px; margin: 18px 0;">
  <strong style="color: #41bdf5;">Requires:</strong> The official Home Assistant Tuya integration, a camera with an SD card, the Smart Life app for one QR approval, and Home Assistant's <code>ffmpeg</code> integration.
</div>

> [!IMPORTANT]
> This integration is for SD-card recordings. It does not replace the official
> Tuya camera entity, camera setup, live view, firmware management, or Tuya
> Cloud video storage.

## Features

- SD-card day and recording discovery through the Smart Life native playback
  protocol.
- Video and audio playback from one serialized camera session.
- A compact custom panel with camera picker, date picker, wall-clock timeline,
  recording ranges, keyboard seeking, and fullscreen controls.
- Optional local MP4 caching under a private `/media` folder.
- Cached recordings in Home Assistant Media Browser.
- One **Camera events** entity per supported camera, preserving the original
  Tuya notification code and metadata in Home Assistant history.
- Separate **Motion detected** and **Person detected** binary sensors for each
  camera, classified from the same Tuya IPC notifications.
- A bounded, one-camera-at-a-time work queue for catalog refresh, thumbnail
  work, and optional caching.
- Smart Life QR authorization during setup. No Tuya Developer project or
  LocalTuya camera entry is required.
- No helper process or bundled executable.

## Requirements

- Home Assistant with the official **Tuya** integration configured for the
  same Smart Life / Tuya account.
- A Tuya / Smart Life camera with an SD card and recordings.
- The Smart Life mobile app, used once during setup to approve the QR code.
- Home Assistant's `ffmpeg` integration. It remuxes the camera's existing
  encoded streams for browser playback; it does not transcode them.

LocalTuya may coexist for other local controls, but it is not a Tuya Recordings
requirement. Tuya Developer OpenAPI services and Tuya Cloud video storage are
also not required.

## Installation

### HACS

[![Open your Home Assistant instance and add this repository to HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Wheemer&repository=tuya-recordings&category=integration)

1. Open **HACS** in Home Assistant.
2. Add `https://github.com/Wheemer/tuya-recordings` as a custom repository in
   the **Integration** category.
3. Download **Tuya Recordings** and restart Home Assistant when HACS asks.
4. Go to **Settings > Devices & services > Add integration** and select
   **Tuya Recordings**.

### Manual

Copy this repository's `custom_components/tuya_recordings` directory to:

```text
/config/custom_components/tuya_recordings
```

Restart Home Assistant, then add **Tuya Recordings** from **Settings > Devices
& services > Add integration**.

## Setup

The setup flow asks for:

- **Smart Life account region**
- **Private video storage path**, normally `/media/tuya_recordings`
- **Recording order**
- **Pre-cache recordings**
- **Sync window in hours** when pre-caching is enabled

It then shows one QR code. Scan it with the Smart Life app, approve the login,
and submit the Home Assistant step. The QR code is intentionally not polled or
refreshed in the background. If it expires, restart setup to make a new code.

Use a private `/media` location for cached files. Do not use `/config/www`.

## Playback And Caching

### Default: SD-card timeline playback

With **Pre-cache recordings** off, the panel reads the recording catalog and
plays the selected time directly from the camera's SD card. There is one active
camera session at a time: selecting another point stops the prior session
before the next one begins. The background catalog pass is metadata-only; it
does not download or retain video files.

### Optional: local MP4 cache

With **Pre-cache recordings** on, Tuya Recordings downloads selected SD-card
recordings into the configured private storage path. Cached clips appear in the
panel and Home Assistant Media Browser, and their thumbnails are generated
locally from the cached files. Set the sync window to `0` only when the storage
location has enough free space for every discovered recording.

Changing the storage path does not move existing files. Move the existing
`videos` and `thumbs` folders before confirming the new path if you want to
retain prior cached clips.

## Camera Detection Entities

For each supported Tuya camera notification source, Tuya Recordings creates:

- **Camera events**: a point-in-time event entity. It records `detected` or
  `cleared`, the original `tuya_event_code`, and camera-supplied metadata.
- **Motion detected**: turns on for supported IPC motion notifications.
- **Person detected**: turns on for supported IPC person notifications.

The binary sensors are notification-driven. They only change when the camera
reports a matching detection or an explicit clear; they do not guess an `off`
state with a timer. A person notification can correctly turn on both sensors,
because a person is also motion.

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

Camera work is serialized per integration and interactive playback takes
priority over background work. The integration does not fan out parallel
recording sessions across cameras.

When local caching is enabled, **Pause camera activity** stops camera-facing
refresh, caching, thumbnail work, and uncached playback while continuing to
serve complete files already saved locally. Turn it on while a camera is
recovering or when you want Tuya Recordings completely quiet.

## Troubleshooting

**No cameras or setup cannot continue**

- Confirm that the official Home Assistant Tuya integration is configured for
  the same account and exposes the camera.
- Confirm that the camera has an SD card with recordings in Smart Life.

**Smart Life authorization failed or expired**

- Reconfigure Tuya Recordings and complete a fresh QR authorization.

**Playback is unavailable**

- Confirm the same time is playable in the Smart Life app.
- Confirm Home Assistant's `ffmpeg` integration is available.
- Check that **Pause camera activity** is off when using cached mode.

**Cached clips do not appear**

- Enable **Pre-cache recordings** and confirm the selected storage path has
  sufficient free space.
- Use `tuya_recordings.sync_media` to request a cache pass.

## Changelog

Release history is maintained in [CHANGELOG.md](CHANGELOG.md).
