# Third-Party Notices

Tuya Recordings bundles small Linux helper binaries used to establish Tuya IPC
WebRTC sessions and remux SD-card playback into cached MP4 files.

## Pion

The helper is built with the Pion WebRTC stack.

- Project: https://github.com/pion/webrtc
- License: MIT

The source used to build the helper is in `tools/pion_offer_probe`.

## Patched Pion ICE Source

`tools/pion_ice_patch` contains the patched Pion ICE source used by the helper
build. It is included so the bundled helper can be rebuilt and audited from
source.

- Upstream project: https://github.com/pion/ice
- License: MIT

See `tools/pion_ice_patch/LICENSE` and
`tools/pion_ice_patch/LICENSES/MIT.txt` for the upstream license text.
