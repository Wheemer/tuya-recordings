import json
from pathlib import Path

from PIL import Image

from custom_components import tuya_recordings
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.models import (
    AccountSession,
    MqttIdentity,
    P2pSession,
    RelayToken,
    StreamConfig,
    TuyaDevice,
)


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "tuya_recordings"


def test_native_transport_is_vendored_and_not_downloaded_from_pypi():
    manifest = json.loads((INTEGRATION / "manifest.json").read_text(encoding="utf-8"))
    requirements = manifest.get("requirements", [])
    dependencies = manifest.get("dependencies", [])

    assert not any(
        requirement.lower().startswith("tuya-ipc-p2p-sdk")
        for requirement in requirements
    )
    assert (
        INTEGRATION / "vendor" / "tuya_ipc_p2p_sdk" / "transport" / "relay_session.py"
    ).is_file()
    assert (
        INTEGRATION / "vendor" / "tuya_ipc_p2p_sdk" / "LICENSE.txt"
    ).is_file()
    assert "tuya" in dependencies
    assert "ffmpeg" in dependencies
    assert "localtuya" not in dependencies
    assert "tinytuya==1.20.0" in requirements


def test_manifest_order_and_config_entry_only_schema():
    manifest = json.loads(
        (INTEGRATION / "manifest.json").read_text(encoding="utf-8"),
        object_pairs_hook=dict,
    )

    assert list(manifest) == [
        "domain",
        "name",
        *sorted(set(manifest) - {"domain", "name"}),
    ]
    assert callable(tuya_recordings.CONFIG_SCHEMA)


def test_manifest_version_matches_current_changelog_release():
    manifest = json.loads((INTEGRATION / "manifest.json").read_text(encoding="utf-8"))
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert f"## Tuya Recordings v{manifest['version']} (unreleased)" in changelog


def test_home_assistant_local_brand_assets_are_packaged():
    brand = INTEGRATION / "brand"

    with Image.open(brand / "icon.png") as icon:
        assert icon.format == "PNG"
        assert icon.mode == "RGBA"
        assert icon.width == icon.height
    with Image.open(brand / "logo.png") as logo:
        assert logo.format == "PNG"
        assert logo.mode == "RGBA"
        assert logo.width > 0
        assert logo.height > 0


def test_obsolete_pion_helpers_are_not_packaged():
    assert not list(INTEGRATION.glob("pion_offer*"))


def test_obsolete_webrtc_browser_harnesses_are_not_retained():
    obsolete = {
        "viewer_audio.browser.py",
        "viewer_camera.browser.py",
    }

    assert not obsolete.intersection(path.name for path in (ROOT / "tests").glob("*.py"))


def test_removed_transport_implementations_are_not_packaged():
    obsolete = {
        "ipc.py",
        "native_lan_302.py",
        "openapi.py",
        "pion.py",
        "protect.py",
        "protect_auth.py",
        "thumbnail_sample.py",
        "webrtc.py",
    }

    assert not obsolete.intersection(
        path.name for path in (INTEGRATION / "lib").glob("*.py")
    )


def test_native_implementation_does_not_package_executables():
    executable_suffixes = {".exe", ".so", ".dll", ".dylib"}

    assert not [
        path
        for path in INTEGRATION.rglob("*")
        if path.is_file() and path.suffix.lower() in executable_suffixes
    ]


def test_panel_uses_inline_native_player_and_always_renders_pause_control():
    panel = (INTEGRATION / "frontend" / "panel.js").read_text(encoding="utf-8")
    player = (INTEGRATION / "frontend" / "camera-player.js").read_text(
        encoding="utf-8"
    )

    assert 'id="pause-toggle"' in panel
    assert 'const label = paused ? "Resume" : "Pause"' in panel
    assert "await this.loadData();" in panel
    assert 'class="clip-player"' in panel
    assert "attachCameraPlayer(video, this.selectedClip" in panel
    assert "stream_transport === \"native\"" in panel
    assert "new window.Plyr(video" in player
    assert "'mute', 'volume', 'fullscreen'" in player
    assert "new MediaSource()" in player
    assert "audioSampleRate = format.getUint32(0)" in player
    assert "audioChannels = format.getUint8(4)" in player
    assert "audioContext = new AudioContextClass()" in player
    assert 'this.playbackStatus = "Starting playback..."' not in panel
    assert ".timeline-player > * { position: absolute; inset: 0; }" in panel
    assert "contain: layout paint" in panel
    assert "if (streamStarting || video.parentElement.classList.contains('camera-player--switching')) return;" in player


def test_native_player_has_one_seek_dispatch_path():
    player = (INTEGRATION / "frontend" / "camera-player.js").read_text(
        encoding="utf-8"
    )

    assert "Object.defineProperty(player, 'currentTime'" in player
    assert "on(seek, 'change'" not in player


def test_vendored_session_objects_hide_credentials_from_repr():
    secret_values = {
        "sid-secret",
        "ecode-secret",
        "uid-secret",
        "fingerprint-secret",
        "client-secret",
        "username-secret",
        "password-secret",
        "ice-secret",
        "ufrag-secret",
        "trace-secret",
        "local-key-secret",
        "camera-password-secret",
        "moto-secret",
        "device-local-key-secret",
        "relay-secret",
        "relay-user-secret",
        "relay-session-secret",
    }
    account = AccountSession(
        "sid-secret", "ecode-secret", "uid-secret", "fingerprint-secret"
    )
    identity = MqttIdentity(
        "mqtt.example.test", 8883, "client-secret", "username-secret", "password-secret"
    )
    p2p = P2pSession(
        "sid-secret", b"0123456789abcdef", "ufrag-secret", "ice-secret", "trace-secret", "uid-secret"
    )
    relay = RelayToken(
        ["tcp4:relay.example.test:443"],
        "relay-user-secret",
        "relay-secret",
        "relay-session-secret",
        {},
    )
    stream = StreamConfig(
        "camera",
        "local-key-secret",
        "camera-password-secret",
        "moto-secret",
        p2p,
        relay,
    )
    device = TuyaDevice(
        "camera", "Camera", "sp", "device-local-key-secret", "product", True
    )

    rendered = " ".join(map(repr, (account, identity, p2p, relay, stream, device)))
    assert all(value not in rendered for value in secret_values)
