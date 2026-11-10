# Changelog

## Tuya Recordings v0.4.13-18 (2026-11-10)

### SD Playback

- Follow the Smart Life native SDK's SD connection sequence: authenticate and prepare the video/audio readers without starting a live preview first.
- Remove the preview-start, video-channel wait, and preview-stop prerequisite before SD playback. Start the recording directly, keeping the readers ready for media that arrives before the start acknowledgement.
- Preserve the connected session for seeking and pause/resume. Video and audio remain LAN-only, without transcoding or additional background retries.

### Diagnostics

- Include the active SD task, stream identity, video/audio frame counts, and media-buffer state when playback times out.
- Distinguish media decryption, framing, stale-task, RTP, and H.264 decoding-stage failures instead of silently discarding that evidence.
- Log the requested recording bounds and native start acknowledgement separately from connection readiness.

## Tuya Recordings v0.4.13-17 (2026-11-10)

### Recording Catalog

- Handle an explicit Tuya `DEVICE_OFFLINE` response as camera unavailability: retain existing recordings, respect backoff, and report offline state separately from protocol failures. Unexpected failures still include their tracebacks.
- Let manual refresh cooperatively interrupt an automatic catalog pass and wait for transport cleanup before starting its own request. Scheduled catalog work defers while that manual refresh is running.
- Preserve fresh offline state for skipped cameras in the displayed catalog. Explicit manual queries can still test reachability rather than treating an inventory report as a permanent block.

### Panel And Diagnostics

- Prefer an available camera with recording data on initial panel load instead of blindly selecting the first, possibly empty offline camera. Keep explicit user selections and label cameras reported offline.
- Log recording range counts, summed range durations, and first/last timestamps rather than describing returned ranges as individual video files.
- Preserve the existing native playback path, LAN-only media, wake spacing, and command serialization. No new camera polling or retries are added.

These fixes do not establish complete SD-card discovery or video/audio playback on the affected cameras; those hardware results remain unverified.

## Tuya Recordings v0.4.13-16 (2026-11-09)

### Wake And SD Readiness

- Correct fresh SD connections that queried status while the required wake was suppressed by cooldown. If LAN cannot confirm readiness, wait cancellably for one permitted wake before querying SD status.
- Preserve the fast path for LAN-ready cameras without publishing an additional wake. Keep the device-wide report listener active during the wait and accept a genuine readiness report if one arrives.
- Start the SD-report deadline after the wake-slot wait, and include that bounded wait in the outer connection budget without changing protocol deadlines.
- Preserve wake spacing, missing-report cooldowns, cancellation, and command serialization. No detached retries are added; video and audio remain LAN-only.

This prerelease addresses connection preparation. Complete recording discovery and video/audio playback on the affected cameras remain unverified.

## Tuya Recordings v0.4.13-15 (2026-11-09)

### Recording Catalog

- Apply the Smart Life APK's wake and SD-readiness preparation to automatic day-catalog connections, not just manual refreshes.
- Use fresh account availability reports to defer automatic work for cameras reported offline. These hints expire, and a successful native catalog response supersedes them.
- Keep explicit manual refresh available despite an offline inventory report. Manual browsing and playback retain their existing connection path.

### Diagnostics And Safety

- Distinguish automatic queries skipped because of an offline report from queries skipped because of cache or backoff.
- Correct SD-query diagnostics when an MQTT report arrives during a LAN query, without claiming that LAN delivery completed.
- Preserve command serialization, wake cooldowns, failure backoff, and playback priority. No background offer resends, additional inventory polling, or media relay are added; video and audio remain LAN-only.

## Tuya Recordings v0.4.13-14 (2026-11-09)

### Connection

- Follow the Smart Life native SDK's connection-ready sequence: an authenticated, nominated LAN connection can begin native authorization and SD-card commands without waiting for a separate SDP answer.
- Match the APK's MQTT offer and candidate headers. Keep the relay path marker on the native command-channel copy only.
- Report whether native control started through an SDP answer or authenticated LAN nomination, and show the SD-status query route and dispatch outcome without exposing credentials.

### Wake And Safety

- Send the SD-playback wireless wake with the SDK's QoS 1 delivery setting. Keep generic device wake unchanged.
- Preserve the existing wake deduplication, cooldowns, and SD-status deadline. A broker acknowledgement alone does not count as camera readiness.
- Keep video and audio LAN-only, reject conflicting late session keys, and close signaling connections before native media begins. No background retries are added.

## Tuya Recordings v0.4.13-13 (2026-11-09)

### Connection

- Follow the Smart Life native SDK's bounded signaling resend: replay the cached offer and ICE candidates together, without opening another TCP connection or repeating wake commands.
- Preserve the current offer's trace ID on MQTT and native command-channel candidates, including offers initialized through HTTP.
- Keep the HTTP SD-status query route available when cloud-online metadata is absent. Only ready or full SD cards proceed to playback.

### Playback And Safety

- Match the APK's request and task IDs for preview stop, playback, pause, resume, seek, and stop. Ignore video and audio belonging to an earlier playback task.
- Check camera completion results instead of treating any matching response as success. Do not wait for audio-open or audio-stop acknowledgements that the native SDK does not require.
- Queue video and audio stop commands in native SDK order, and validate a seek before replacing the current playback state.
- Keep video and audio LAN-only. Existing command serialization, deadlines, cooldowns, and scheduled-work limits remain in place; no background retries are added.

## Tuya Recordings v0.4.13-12 (2026-11-08)

### Connection

- Match the Smart Life APK's native TCP decoder by scanning all frame attributes and selecting the last type-7 data attribute, rather than silently discarding a later answer behind an ACK.
- Keep the connection open after well-formed frames without native data so a later authenticated answer can still arrive. Malformed frames and invalid authentication tags remain rejected.
- Apply the APK's ten-second inbound heartbeat deadline without reconnecting. Distinguish local heartbeat timeouts from remote socket closure.
- Add frame-attribute counts, ignored non-data counts, ACK sequence details, and pending-segment diagnostics without exposing credentials or payloads. Video and audio remain LAN-only.

### Refresh Recordings

- Let manual Refresh recordings try again during automatic catalog backoff, with a separate 30-second repeat cooldown. Existing wake-timeout safety limits remain active.
- Stop querying older days for a camera after its first failure in a pass, while allowing other cameras to continue. Start automatic backoff when the failed attempt finishes.
- Prevent scheduled catalog work from inheriting a manual request ID or foreground wake and resend permissions.

## Tuya Recordings v0.4.13-11 (2026-11-08)

### Connection

- Follow the APK's native command routing: send command-channel data over TCP once per sequence, while retaining direct acknowledgements. Attach answer listeners before gathering ICE candidates.
- Preserve both signaling endpoint lists and try alternatives within one bounded dial attempt. Direct connections no longer require an optional TCP relay token.
- Validate the final authenticated TCP handshake and report rejection, closure, and incomplete reads without exposing credentials.
- Enforce LAN-only video and audio, including rejecting public ICE candidates and nominations. Stop unsupported security modes before opening a connection.

### Playback And Safety

- Match the APK's video task and audio request filters so obsolete preview and seek packets do not enter current playback.
- Correct audio/video alignment when their timestamp clocks differ, and advertise only the audio capabilities implemented by the receiver.
- Detect stalled video even when audio continues, and require usable video configuration before accepting startup as successful.
- Cancel and join owned cleanup tasks, close the backend's HTTP session after its workers stop, and require valid month-catalog responses.

## Tuya Recordings v0.4.13-10 (2026-11-08)

### Connection

- Add APK-native command-channel signaling for the reported security-level 3 connection path, including session answers received through the native bootstrap.
- Keep TCP restricted to signaling, close it after the answer, and block TCP recording-control, video, and audio packets. Playback remains LAN-only.
- Use bounded command retransmission backoff and stop obsolete bootstrap retransmits after the answer. Scheduled catalog work does not gain a TCP bootstrap.

### Safety And Diagnostics

- Validate TCP handshake authentication and native command framing, bound receive buffers, and reject replies for another device or session.
- Join bootstrap tasks during cancellation and cleanup, and continue closing the other transports if command-channel cleanup fails.
- Report the command-answer source, TCP attempt count, rejected sessions, and blocked media without logging credentials.

## Tuya Recordings v0.4.13-9 (2026-11-07)

### Connection

- Correct the outgoing offer's preconnect flag using the token value traced in both Smart Life and Tuya Smart APKs, independently of the app's connection optimization.
- Apply refreshed token flags to the existing MQTT connection and discard stale route flags when replacing a token.
- Match the native SDK's numeric conversion for token expiry and connection-route fields, including decimal-form numbers.
- Stop incompatible server-managed sessions before sending an ordinary direct-ICE offer.

### Diagnostics

- Log the token and offer route before sending, with separate app-optimization and outgoing preconnect flags.
- Use the same normalized token values for route selection and diagnostics, without exposing credentials or media payloads.

## Tuya Recordings v0.4.13-8 (2026-11-07)

### Connection

- Receive native connection answers over both MQTT and LAN, following the listener registration in both Smart Life and Tuya Smart APKs. Keep the SD-status LAN socket open for the answer instead of closing it before the offer.
- Separate LAN answer listening from low-power wake eligibility, and allow a usable LAN offer to proceed when its optional MQTT answer subscription is unavailable.
- Prefer refreshed communication protocol metadata and discard obsolete SD-status routes when device details change.
- Block replacement camera sessions when the previous transport has not closed cleanly.
- Add live LAN reply diagnostics, including reader state, session mismatches, and answer source, without logging credentials or media payloads.

### Playback

- Fix pause, resume, and subsequent timeline seeks while retaining the same player and camera session. Correct timeline position after pausing.
- Retrieve all pages of legacy detection catalogs instead of stopping after the first page.
- Wait for stream producers to finish during cancellation and early HTTP errors, and release media-event listeners after each completed operation.
- Finish finite clips according to media progress rather than elapsed wall time, so buffering does not cut playback short.

## Tuya Recordings v0.4.13-7 (2026-11-07)

### Fixes

- Correct LAN 3.4 and 3.5 SD-status request formatting to match the protocol-5 wrapper used by both Smart Life and Tuya Smart APKs.
- Correct native LAN signaling to use IPC_LAN_302 and its raw signaling body rather than the unrelated extended-stream command.
- Check SD status before foreground native LAN signaling, and reuse the completed check if the connection falls back to MQTT.
- Keep SD-status queries active when a repeat wake is throttled or wake publication fails. Stop before sending a playback offer if the query fails or SD status cannot be verified.
- Handle the APK's low-power connection transitions separately from SD-status reports, including timestamp checks before confirming wake.
- Add diagnostics for the actual SD-query LAN version, request format, delivery result, and low-power connection state.

## Tuya Recordings v0.4.13-6 (2026-11-07)

### Fixes

- Keep the SD-status listener active throughout wake, LAN connection, cleanup, and fallback so early camera replies are not lost.
- Follow the wake and SD-status ordering traced in both Smart Life and Tuya Smart APKs for foreground catalog requests and playback, with one shared 30-second SD-status deadline.
- Require a matching LAN command result or SD-status report before accepting delivery. Unrelated packets no longer suppress the normal fallback.
- Restore the APK's final HTTP SD-status query route and use authenticated connectivity updates when choosing MQTT or HTTP.
- Retain remote ICE candidates from signaling and the answer, and send bounded authenticated direct UDP connectivity checks.
- Report the SD-status reply source and skip further catalog-day requests during an SD-status timeout cooldown. Scheduled work gains no wake permission or background retries.

## Tuya Recordings v0.4.13-5 (2026-11-07)

### Fixes

- Restore the eligible direct camera connector's foreground wake step, including interactive playback. Share one wake send with the SD-status step; scheduled jobs do not gain wake permission.
- Decode authenticated inbound MQTT frames using their wire protocol version without changing the outgoing camera protocol version.
- Accept numeric SD-status DP IDs as the Android schema model does.
- Parse numeric-string SD-status reports on MQTT and LAN as the APK's DP helper does.
- Fetch missing product schemas during manual inventory refresh using the product ID and version, following the schema-loading path traced in both Smart Life and Tuya Smart APKs. This allows the wake and SD-status sequence to use the camera's actual SD-status DP before sending the playback offer.
- Match schemas by product ID and version, deduplicate lookups, and limit each request to 20 products. Scheduled work and background retries are unchanged.
- Preserve the manual-refresh request ID through Home Assistant's executor so inventory, wake metadata, and catalog logs can be correlated.
- Add payload-free MQTT protocol counts and DP-report classifications to help distinguish unrelated traffic from a matching SD-status report.

## Tuya Recordings v0.4.13-4 (unreleased)

### Fixes

- Fix the **Refresh recordings** button's catalog pass so eligible cameras can run the APK-backed wake and SD-status sequence before the MQTT offer.
- If a scheduled catalog pass is already running, manual refresh waits for it and then runs its own pass. Scheduled wake and retry behavior is unchanged.
- Improve diagnostics with a manual-refresh request ID, metadata lookup outcomes, missing wake inputs, and full catalog-failure tracebacks. Logs distinguish fresh attempts from retained errors and backoff skips without dumping the recording catalog or raw device responses.

## Tuya Recordings v0.4.13-3 (unreleased)

### Fixes

- Manual refresh now checks camera details when SD playback wake eligibility is unknown, even if the MQTT protocol version is already known.
- Use the matching product schema to query SD card status after waking an eligible camera. A no-card report stops the request before a playback offer is sent.

## Tuya Recordings v0.4.13-2 (unreleased)

### Fixes

- The **Refresh recordings** button now refreshes camera inventory before refreshing the SD card catalog.
- When inventory doesn't report a camera's MQTT protocol version, an explicit refresh checks that camera's device details using the lookup found in Smart Life and Tuya Smart. The integration doesn't guess a version if it's still missing.
- Extend bounded, same-session MQTT offer resends to on-demand playback and manual catalog requests. Scheduled refreshes and background sync are unchanged.

## Tuya Recordings v0.4.13-1 (2026-11-06)

### Fixes

- Follow the Smart Life and Tuya Smart wake and SD-status routes for non-Matter cameras, with bounded offer retries.
- Use refreshed camera MQTT protocol metadata on the next connection without restarting Home Assistant.
- Report wake, offer, and reply progress when a camera fails to connect.

## Tuya Recordings v0.4.12 (2026-11-05)

### Fixes

- Allow on-demand SD catalog and playback access when the cached inventory
  reports a camera offline. The inventory flag does not verify SD access.
- Keep known recording dates visible when a catalog refresh fails, and show
  the failure in the panel.
- After a failed lookup, give that camera a 30-second break before another
  day or month lookup. Other cameras can still be queried.

## Tuya Recordings v0.4.11 (2026-11-05)

### Fixes

- Follows Smart Life and Tuya Smart's native `connect_v3` sequence: generate
  fresh session credentials for each connection, use the app token when valid,
  and initialize through the native HTTP offer when the SDK requires it.
- Restores a single LAN socket attempt for cameras that advertise local
  signaling. Prevents automatic reconnects and blocks MQTT fallback after a
  LAN offer or incomplete LAN cleanup.
- Distinguishes LAN availability, MQTT offer publishing, and IMM answer
  failures in the logs without exposing session credentials. Related: #25.
- Fixes calendar selection during refresh and preserves the last known dates
  after a failed catalog lookup.

### Playback

- Preserves camera timestamps and adds AAC-LC audio for supported native SD
  streams without transcoding.

## Tuya Recordings v0.4.11-1 (2026-11-01)

### Fixes

- Follows the Smart Life and Tuya Smart consumer-app preconnect policy for
  native SD-card playback signaling. The app enables preconnect independently
  of a conflicting `p2pConfig.preconnect` token value, improving compatibility
  with cameras that did not return an IMM signaling answer.
- Keeps the APK-native transport selection intact: cameras using local P2P
  policy use LAN signaling when locally online; other cameras use MQTT.

## Tuya Recordings v0.4.10 (2026-11-01)

### Fixes

- Uses the APK's LAN-first P2P signaling path for cameras that advertise
  local P2P policy, with a bounded local discovery and no signaling retry loop.
- Keeps MQTT signaling as the fallback only when a camera is not locally
  reachable; a failed local offer does not trigger a second MQTT offer.
- Uses each camera's advertised P2P preconnect and security settings instead
  of fixed MQTT signaling values.
- Adds test coverage for local signaling selection, fallback boundaries, and
  TinyTuya's required local transport dependency.

- Uses the camera's ordinary SD-card day catalog as the timeline source, matching
  the Smart Life and Tuya Smart playback implementations.
- Preserves native `aiDetectList` detection intervals, codes, and icons through
  the catalog pipeline so timeline markers use camera-supplied timing rather
  than Home Assistant state history.
- Removes the unused Home Assistant Recorder dependency from the integration.

## Tuya Recordings v0.4.8 (2026-10-20)

### Improvements

- Discovers cameras from the Smart Life or Tuya Smart account authorized during
  setup. New installations no longer require the official Home Assistant Tuya
  integration.
- Retains the legacy Home Assistant Tuya registry path for existing entries
  until their next QR reauthorization records the native account inventory.

## Tuya Recordings v0.4.7 (2026-10-20)

### Fixes

- Anchors person-detection markers to explicit camera event times that fall
  within the camera's SD-card recording catalog. Home Assistant receipt times
  and Tuya DP receipt times no longer create misleading timeline markers.
- Preserves camera event-catalog metadata in the timeline response.
- Reuses a camera session when playback takes over from a cancelled catalog
  refresh, while keeping ordinary interactive playback connection attempts
  promptly cancellable.

## Tuya Recordings v0.4.6 (2026-10-20)

### Fixes

- Uses Home Assistant's device-registry lookup API when resolving cameras from
  the official Tuya integration, avoiding a deprecated registry access pattern.
- Moves normal playback and frontend request traces to debug logging so routine
  use does not generate warning-level log noise.

## Tuya Recordings v0.4.5 (2026-10-20)

Corrective HACS package release.

### Fixes

- Ignores zero-length audio and video media frames after native playback
  decryption, while retaining errors for invalid decryption and unknown frame
  types. Valid subsequent media continues playing normally.

- Restores the flat HACS ZIP layout, with manifest.json, __init__.py, and
  frontend files at the archive root.
- Adds release-archive validation so nested custom_components/tuya_recordings
  paths cannot be published.

## Tuya Recordings v0.4.3 (2026-10-20)

Corrective camera discovery and native capability release.

### Fixes

- Discovers cameras from Home Assistant's device-registry entry values, fixing
  installations where Tuya Recordings reported no cameras despite cameras being
  available through the official Tuya integration.
- Keeps event-timeline capability separate from native recording-image handling.
  Normal cached thumbnails continue to be generated locally from successfully
  cached MP4 files without opening an additional camera session.

## Tuya Recordings v0.4.2 (2026-10-16)

Native playback and timeline reliability release.

### Highlights

- Makes uncached SD-card playback use one serialized camera session with
  browser-safe video and audio remuxing, without video transcoding.
- Reworks timeline seeking so a completed pointer interaction seeks to the
  selected wall-clock position and resumes playback from that position.
- Keeps the player surface stable while a new native playback stream starts,
  and records detailed browser-side playback diagnostics for troubleshooting.
- Adds Smart Life and Tuya Smart account-app selection to the QR setup flow.
- Makes motion and person history available through a camera event entity while
  retaining the existing Motion detected and Person detected binary sensors.
- Validates setup options and bounds camera background work through a
  serialized, one-camera-at-a-time dispatcher.

### Fixes

- Declares Home Assistant's recorder dependency used by the integration.
- Runs every browser playback fixture and frontend syntax check in GitHub
  Actions with the same repository import path as the Python suite.

## Tuya Recordings v0.4.1.4 (2026-10-05)

Timeline playback seek correction.

### Fixes

- Resets the browser media-source timeline offset when seeking, so the next SD-card segment starts at the requested position instead of remaining on Waiting for video.

## Tuya Recordings v0.4.1.3 (2026-10-05)

Corrective catalog and playback scheduling release.

### Fixes

- Changes uncached catalog refreshes from every 2 minutes to every 15 minutes by default.
- Adds a configurable **Background catalog interval** (15 minutes to 24 hours).
- Lets an interactive playback request cancel an in-flight background catalog command before it opens its own camera session.
- Handles malformed RTP flag bits emitted by affected Tuya cameras so video does not freeze between keyframes.
- Releases the timeline playback lock when a browser disconnects or cancels playback.
- Waits for the first H.264 SPS before sending a stream to ffmpeg.
- Uses `ManagedMediaSource` when iOS does not expose `MediaSource`.
- Allows the `m1.tuyaeu.com` broker used by EU Tuya Smart accounts.

## Tuya Recordings v0.4.1.2 (2026-10-04)

Corrective Tuya Smart setup and HACS package release.

### Fixes

- Uses the tuyaSmart QR payload accepted by the Tuya Smart app.
- Packages the HACS release ZIP with the integration files at archive root,
  preventing a nested custom_components/tuya_recordings install path.

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
