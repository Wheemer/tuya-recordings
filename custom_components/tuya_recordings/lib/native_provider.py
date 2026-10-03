"""APK app-session provider for native Tuya camera connection config."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import threading
import time
from typing import Any

from .native_session import (
    APK_RTC_CONFIG_API,
    NativeCameraSessionConfig,
    NativeSessionError,
    native_session_api_request,
    normalize_camera_info,
)
from .native_gateway import NativeGatewayAuthError


class NativeCameraConfigError(RuntimeError):
    """The app-session camera config cannot be converted into native inputs."""


class NativeCameraConfigAuthError(NativeCameraConfigError):
    """The saved Smart Life app session is invalid or expired."""


@dataclass(slots=True)
class NativeAppCameraConfigProvider:
    """Fetch and normalize the APK `CameraInfoBean` connection payload.

    `call_api` is intentionally injected. It must be the Smart Life app-session
    gateway call, not the Tuya IoT OpenAPI client. `local_key_lookup` must
    return the device local key captured by the Smart Life QR setup flow.
    """

    call_api: Callable[[str, str, dict[str, Any], dict[str, str]], Any] = field(repr=False)
    local_key_lookup: Callable[[str], str] = field(repr=False)
    protocol_version_lookup: Callable[[str], str] = field(
        default=lambda dev_id: "2.2", repr=False
    )
    trace_factory: Callable[[str], str] = field(
        default=lambda dev_id: _apk_trace_id(dev_id), repr=False
    )
    app_session: dict[str, Any] | None = field(default=None, repr=False)
    app_region: str = "us"
    app_device_fingerprint: str = ""
    # The APK's connectV3 trace belongs to one short-lived camera transport.
    # Runtime workers disconnect after eight idle seconds, so this cache must
    # expire first; otherwise a replacement worker reuses an already-consumed
    # trace and the camera never answers its IMM offer.
    cache_ttl: float = 5.0
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    _cache: dict[str, tuple[float, NativeCameraSessionConfig]] = field(
        default_factory=dict, init=False, repr=False
    )
    _cache_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def session_config(self, dev_id: str) -> NativeCameraSessionConfig:
        if not isinstance(dev_id, str) or not dev_id.strip():
            raise NativeCameraConfigError("Missing Tuya device id")
        if not 0 <= self.cache_ttl <= 300:
            raise NativeCameraConfigError("Camera config cache TTL is invalid")
        now = self.clock()
        with self._cache_lock:
            cached = self._cache.get(dev_id)
            if cached is not None and now < cached[0]:
                return cached[1]
        local_key = self._local_key(dev_id)
        # ThingP2PManager creates this trace before requesting camera config,
        # sends it as ApiParams.ctId, then passes it as connectV3.trace_id.
        trace_id = self.trace_factory(dev_id)
        api, version, body, extra_params = native_session_api_request(
            dev_id, trace_id=trace_id
        )
        try:
            result = self.call_api(api, version, body, extra_params)
            camera_info = extract_camera_info(result)
            config = normalize_camera_info(
                dev_id,
                camera_info,
                local_key=local_key,
                trace_id=trace_id,
                app_session=self.app_session,
                app_region=self.app_region,
                app_device_fingerprint=self.app_device_fingerprint,
                mqtt_protocol_version=self.protocol_version_lookup(dev_id),
            )
        except NativeGatewayAuthError as err:
            raise NativeCameraConfigAuthError(
                "Smart Life app-session authorization expired"
            ) from err
        except NativeSessionError as err:
            raise NativeCameraConfigError(
                "APK camera config is not compatible with native playback"
            ) from err
        except NativeCameraConfigError:
            raise
        except Exception as err:
            raise NativeCameraConfigError(
                f"APK camera config request failed for {dev_id}: {type(err).__name__}"
            ) from err
        with self._cache_lock:
            self._cache[dev_id] = (now + self.cache_ttl, config)
        return config

    def _local_key(self, dev_id: str) -> str:
        try:
            local_key = self.local_key_lookup(dev_id)
        except Exception as err:
            raise NativeCameraConfigError(f"Could not find local key for {dev_id}") from err
        if not isinstance(local_key, str) or not local_key.strip():
            raise NativeCameraConfigError(f"Could not find local key for {dev_id}")
        return local_key


def _apk_trace_id(dev_id: str) -> str:
    """Match ToolKit.b(devId) from the Smart Life P2P SDK."""
    return f"ipc_p2p_android_{dev_id}_{int(time.time() * 1000)}"


def extract_camera_info(result: Any) -> dict[str, Any]:
    """Find the CameraInfoBean-like dict inside common app-gateway wrappers."""
    if not isinstance(result, dict):
        raise NativeCameraConfigError("APK camera config result is not an object")
    for candidate in _camera_info_candidates(result):
        if _looks_like_camera_info(candidate):
            return candidate
    raise NativeCameraConfigError("APK camera config result did not contain camera info")


def _camera_info_candidates(value: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = [value]
    for key in ("cameraInfo", "camera_info", "config", "data", "result"):
        nested = value.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
            for nested_key in ("cameraInfo", "camera_info", "config", "data", "result"):
                deeper = nested.get(nested_key)
                if isinstance(deeper, dict):
                    candidates.append(deeper)
    return candidates


def _looks_like_camera_info(value: dict[str, Any]) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("password")
        and (value.get("p2pConfig") or value.get("p2p_config") or value.get("token"))
        and (value.get("skill") or value.get("skillV4"))
        and (value.get("p2pSpecifiedType") or value.get("p2pType"))
    )


def app_camera_api_names() -> tuple[str]:
    """Expose the exact APK camera config API used by playback."""
    return (APK_RTC_CONFIG_API,)
