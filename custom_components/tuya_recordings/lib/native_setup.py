"""Build the pure-Python APK-native backend from Home Assistant runtimes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..const import (
    CONF_DEVICE_LOCAL_KEYS,
    CONF_DEVICE_PROTOCOL_VERSIONS,
    CONF_NATIVE_APP_SESSION,
    CONF_REGION,
)
from .native_backend import (
    ApkNativeRecordingBackend,
    NativeBackendNotConfigured,
    NativeCameraSessionProvider,
    RecordingBackend,
)
from .native_gateway import NativeAppGatewayClient
from .native_media_crypto import NativeMediaSecretStore
from .native_provider import NativeAppCameraConfigProvider
from .native_relay_runtime import NativeRelayRuntimePool

CONF_NATIVE_APP_CALL = "_native_app_call"


class NativeBackendSetupError(RuntimeError):
    """The APK-native backend cannot be built from the current configuration."""


def build_recordings_backend(
    entry_data: Mapping[str, Any],
) -> RecordingBackend:
    """Return the in-process native IMM/Simple backend or a fail-closed backend."""

    try:
        app_call = native_app_call(entry_data)
        app_session = native_app_session(entry_data)
        local_keys = resolved_local_keys(entry_data)
        protocol_versions = resolved_protocol_versions(entry_data)
        if not local_keys:
            raise NativeBackendSetupError("No Tuya camera local keys are available")
    except NativeBackendSetupError as err:
        return NativeBackendNotConfigured(str(err))

    provider = NativeAppCameraConfigProvider(
        call_api=app_call,
        local_key_lookup=lambda dev_id: lookup_local_key(local_keys, dev_id),
        protocol_version_lookup=lambda dev_id: protocol_versions.get(dev_id, "2.2"),
        app_session=app_session,
        app_region=str(entry_data.get(CONF_REGION) or "us"),
        app_device_fingerprint=str((entry_data.get(CONF_NATIVE_APP_SESSION) or {}).get("device_fingerprint") or ""),
    )
    media_secrets = NativeMediaSecretStore(app_call)
    runtime_pool = NativeRelayRuntimePool(
        app_session,
        media_secrets=media_secrets,
    )
    return ApkNativeRecordingBackend(
        NativeCameraSessionProvider(
            session_config=provider.session_config,
            open_session=runtime_pool.session,
            close=runtime_pool.close,
        )
    )


def native_app_call(
    entry_data: Mapping[str, Any],
) -> Callable[[str, str, dict[str, Any], dict[str, str]], Any]:
    """Return the Smart Life app-session gateway caller.

    Tests may inject `_native_app_call`. A serialized app session from the
    native Smart Life QR flow is enough for the built-in mobile gateway caller.
    """
    call = entry_data.get(CONF_NATIVE_APP_CALL)
    if callable(call):
        return call
    session = native_app_session(entry_data)
    if session:
        gateway = NativeAppGatewayClient(
            region=str(entry_data.get(CONF_REGION) or "us"),
            device_fingerprint=session["device_fingerprint"],
        )
        return lambda api, version, body, extra_params: gateway.call_with_saved_session(
            api, version, body, session, extra_params
        )
    raise NativeBackendSetupError("Smart Life app-session authorization is not configured")


def native_app_session(entry_data: Mapping[str, Any]) -> dict[str, Any]:
    """Return the complete APK app session required by gateway and MQTT."""
    session = entry_data.get(CONF_NATIVE_APP_SESSION)
    required = (
        "sid",
        "ecode",
        "uid",
        "partner_identity",
        "mobile_mqtts_url",
        "device_fingerprint",
    )
    if not isinstance(session, Mapping) or any(
        not isinstance(session.get(key), str) or not session[key].strip()
        for key in required
    ):
        raise NativeBackendSetupError(
            "Smart Life app-session authorization is incomplete; reauthenticate Tuya Recordings"
        )
    return dict(session)


def lookup_local_key(local_keys: Mapping[str, str], dev_id: str) -> str:
    try:
        local_key = local_keys[dev_id]
    except KeyError as err:
        raise NativeBackendSetupError(f"Missing local key for {dev_id}") from err
    if not isinstance(local_key, str) or not local_key.strip():
        raise NativeBackendSetupError(f"Missing local key for {dev_id}")
    return local_key


def resolved_protocol_versions(entry_data: Mapping[str, Any]) -> dict[str, str]:
    """Return explicit MQTT framing versions captured from DeviceBean data.

    LAN protocol versions and ``moduleMap.wifi.pv`` are not DeviceBean MQTT
    framing values and must never be substituted here.
    """
    source = entry_data.get(CONF_DEVICE_PROTOCOL_VERSIONS)
    if not isinstance(source, Mapping):
        return {}
    found: dict[str, str] = {}
    for dev_id, version in source.items():
        if not isinstance(dev_id, str) or not dev_id.strip():
            continue
        if not isinstance(version, str | int | float):
            continue
        normalized = str(version).strip()
        try:
            if normalized and float(normalized) >= 1.0:
                found[dev_id.strip()] = normalized
        except ValueError:
            continue
    return found


def resolved_local_keys(entry_data: Mapping[str, Any]) -> dict[str, str]:
    """Return keys captured from the current Smart Life QR account session."""
    found: dict[str, str] = {}
    current = entry_data.get(CONF_DEVICE_LOCAL_KEYS)
    if not isinstance(current, Mapping):
        return found
    for dev_id, local_key in current.items():
        _add_local_key(found, dev_id, local_key)
    return found


def _add_local_key(found: dict[str, str], dev_id: Any, local_key: Any) -> None:
    if isinstance(dev_id, str) and dev_id.strip() and isinstance(local_key, str) and local_key.strip():
        found[dev_id.strip()] = local_key.strip()
