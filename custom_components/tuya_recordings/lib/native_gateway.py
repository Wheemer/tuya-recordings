"""Smart Life mobile gateway caller for APK-native camera bootstrap.

This is the missing app-session side of the native path. It implements the
signed, encrypted `et=3` mobile gateway request shape used by Smart Life SDK
calls such as `m.ipc.v4.rtc.config.get`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import requests
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .native_auth import NativeAppSession

PACKAGE_NAME = "com.tuya.smartlife"
CERT_SHA256 = (
    "0F:C3:61:99:9C:C0:C3:5B:A8:AC:A5:7D:AA:55:93:A2"
    ":0C:F5:57:27:70:2E:A8:5A:D7:B3:22:89:49:F8:88:FE"
)
DERIVED_KEY = "jfg5rs5kkmrj5mxahugvucrsvw43t48x"
APP_SECRET = "r3me7ghmxjevrvnpemwmhw3fxtacphyg"  # noqa: S105
COMPOSITE_KEY = f"{PACKAGE_NAME}_{CERT_SHA256}_{DERIVED_KEY}_{APP_SECRET}"

CLIENT_ID = "ekmnwp9f5pnh3trdtpgy"
CH_KEY = "ec9709a4"
APP_VERSION = "7.11.0"
SDK_VERSION = "7.11.0"
LANGUAGE = "en_US"
TTID = "smartlife"
_NONCE_LENGTH = 12
_TAG_LENGTH = 16
_BODY_KEY_LENGTH = 16
_MD5_HEX_LENGTH = 32
_REQUEST_TIMEOUT_SECONDS = 20
_SIGN_WHITELIST = frozenset(
    {
        "a",
        "v",
        "lat",
        "lon",
        "lang",
        "deviceId",
        "appVersion",
        "ttid",
        "isH5",
        "h5Token",
        "os",
        "clientId",
        "postData",
        "time",
        "requestId",
        "et",
        "n4h5",
        "sid",
        "chKey",
        "sp",
    }
)


class NativeGatewayError(RuntimeError):
    """Smart Life mobile gateway request failed."""


class NativeGatewayAuthError(NativeGatewayError):
    """Smart Life app session is invalid or expired."""


class NativeGatewayProtocolError(NativeGatewayError):
    """Smart Life mobile gateway response was malformed."""


def generate_device_fingerprint() -> str:
    """Create one stable app-device identity for a new config entry."""
    return secrets.token_urlsafe(33)


@dataclass(slots=True)
class NativeAppGatewayClient:
    """Synchronous Smart Life mobile gateway client.

    Home Assistant calls this from executor jobs, so it deliberately uses the
    existing synchronous `requests` stack rather than owning an event loop.
    """

    region: str = "us"
    device_fingerprint: str = field(default_factory=generate_device_fingerprint)
    session: requests.Session = field(default_factory=requests.Session, repr=False)
    composite_key: str = field(default=COMPOSITE_KEY, repr=False)

    def call(
        self,
        api: str,
        version: str,
        post: dict[str, Any],
        app_session: NativeAppSession | None = None,
        extra_params: dict[str, str] | None = None,
    ) -> Any:
        """Issue one signed encrypted mobile-gateway call."""
        api = _wire_api_name(api)
        request_id = str(uuid4())
        key = (
            _body_key(request_id, app_session.ecode, self.composite_key)
            if app_session is not None
            else _pre_login_body_key(request_id, self.composite_key)
        )
        encrypted = _encrypt_post_data(key, _dump_json(post))
        params = self._build_params(api, version, request_id, encrypted, app_session, extra_params)
        return self._unwrap(api, key, self._post(api, params))

    def call_with_saved_session(
        self,
        api: str,
        version: str,
        post: dict[str, Any],
        saved: Any,
        extra_params: dict[str, str] | None = None,
    ) -> Any:
        """Call an API using a serialized NativeAppSession mapping."""
        return self.call(
            api,
            version,
            post,
            _saved_session(saved),
            extra_params,
        )

    def device_local_keys(self, saved: Any) -> dict[str, str]:
        """Return current device local keys using the APK account APIs."""
        local_keys, _protocol_versions = self.device_connection_data(saved)
        return local_keys

    def device_connection_data(
        self, saved: Any
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Return local keys and MQTT protocol versions from Smart Life.

        Only an explicit device ``pv`` is accepted. ``moduleMap.wifi.pv`` is
        module/OTA metadata and is not the communication-mode value copied
        into ``DeviceBean.pv`` by Smart Life.
        """
        app_session = _saved_session(saved)
        homes = self.call("tuya.m.location.list", "2.1", {}, app_session)
        if not isinstance(homes, list):
            raise NativeGatewayProtocolError("Smart Life home list was malformed")
        found: dict[str, str] = {}
        protocol_versions: dict[str, str] = {}
        seen: set[int] = set()
        for home in homes:
            if not isinstance(home, dict) or type(home.get("gid")) is not int:
                continue
            gid = home["gid"]
            if gid in seen:
                continue
            seen.add(gid)
            devices = self.call(
                "tuya.m.my.group.device.list",
                "1.0",
                {},
                app_session,
                {"gid": str(gid)},
            )
            if not isinstance(devices, list):
                raise NativeGatewayProtocolError(
                    "Smart Life device list was malformed"
                )
            for device in devices:
                if not isinstance(device, dict):
                    continue
                dev_id = device.get("devId")
                local_key = device.get("localKey")
                if (
                    isinstance(dev_id, str)
                    and dev_id.strip()
                    and isinstance(local_key, str)
                    and local_key.strip()
                ):
                    normalized_id = dev_id.strip()
                    found[normalized_id] = local_key.strip()
                    protocol_version = _device_mqtt_protocol_version(device)
                    if protocol_version is not None:
                        protocol_versions[normalized_id] = protocol_version
        return found, protocol_versions

    def _build_params(
        self,
        api: str,
        version: str,
        request_id: str,
        encrypted: str,
        app_session: NativeAppSession | None,
        extra_params: dict[str, str] | None,
    ) -> dict[str, str]:
        params = {
            "a": api,
            "v": version,
            "clientId": CLIENT_ID,
            "time": str(int(time.time())),
            "requestId": request_id,
            "lang": LANGUAGE,
            "deviceId": self.device_fingerprint,
            "appVersion": APP_VERSION,
            "ttid": TTID,
            "os": "Android",
            "sdkVersion": SDK_VERSION,
            "chKey": CH_KEY,
            "et": "3",
            "postData": encrypted,
        }
        if app_session is not None:
            params["sid"] = app_session.sid
        if extra_params:
            params.update(extra_params)
        signed = dict(params)
        signed["postData"] = _post_data_sign_field(encrypted)
        params["sign"] = _sign(_build_sign_string(signed), self.composite_key)
        return params

    def _post(self, api: str, params: dict[str, str]) -> str:
        try:
            response = self.session.post(
                _gateway_url(self.region),
                data=params,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": f"TY/{APP_VERSION}",
                },
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.text
        except requests.RequestException as err:
            raise NativeGatewayError(f"Failed to call {api}: {type(err).__name__}") from err

    def _unwrap(self, api: str, key: str, raw: str) -> Any:
        envelope = _parse_json_object(raw)
        result = envelope.get("result")
        if isinstance(result, str) and result:
            inner = _parse_json_object(_decrypt_post_data(key, result).decode("utf-8"))
            envelope = {**envelope, **inner}
            result = inner.get("result")
        error_code = _optional_str(envelope, "errorCode")
        if error_code or envelope.get("success") is False:
            message = _optional_str(envelope, "errorMsg") or "request rejected"
            error = NativeGatewayAuthError if error_code in {"USER_SESSION_INVALID", "USER_SESSION_EXPIRED"} else NativeGatewayError
            raise error(f"{api}: {error_code or 'error'} {message}")
        if result is None:
            raise NativeGatewayProtocolError(f"Failed to call {api}: empty result")
        return result


def _device_mqtt_protocol_version(device: dict[str, Any]) -> str | None:
    """Extract an explicit APK DeviceBean MQTT protocol version."""
    version = device.get("pv")
    if not isinstance(version, str | int | float):
        return None
    normalized = str(version).strip()
    try:
        if not normalized or float(normalized) < 1.0:
            return None
    except ValueError:
        return None
    return normalized


def _saved_session(value: Any) -> NativeAppSession:
    if isinstance(value, NativeAppSession):
        return value
    if not isinstance(value, dict):
        raise NativeGatewayAuthError("Smart Life app-session authorization is not configured")
    try:
        return NativeAppSession(
            sid=_required_text(value, "sid"),
            ecode=_required_text(value, "ecode"),
            uid=_required_text(value, "uid"),
            partner_identity=_required_text(value, "partner_identity"),
            mobile_mqtts_url=_required_text(value, "mobile_mqtts_url"),
            device_fingerprint=_required_text(value, "device_fingerprint"),
        )
    except ValueError as err:
        raise NativeGatewayAuthError("Smart Life app-session authorization is incomplete") from err


def _wire_api_name(api: str) -> str:
    if api.startswith("thing."):
        return "smartlife." + api[len("thing.") :]
    return api


def _gateway_url(region: str) -> str:
    if not isinstance(region, str) or not region.strip():
        region = "us"
    return f"https://a1-{region}.lifeaiot.com/api.json"


def _dump_json(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _parse_json_object(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as err:
        raise NativeGatewayProtocolError("Gateway response was not JSON") from err
    if not isinstance(decoded, dict):
        raise NativeGatewayProtocolError("Gateway response was not an object")
    return decoded


def _encrypt_post_data(key: str, post_json: bytes) -> str:
    nonce = secrets.token_bytes(_NONCE_LENGTH)
    sealed = AESGCM(key.encode("utf-8")).encrypt(nonce, post_json, None)
    return base64.b64encode(nonce + sealed).decode("ascii")


def _decrypt_post_data(key: str, encrypted: str) -> bytes:
    try:
        raw = base64.b64decode(encrypted, validate=True)
    except ValueError as err:
        raise NativeGatewayProtocolError("Gateway encrypted body was not base64") from err
    if len(raw) < _NONCE_LENGTH + _TAG_LENGTH:
        raise NativeGatewayProtocolError("Gateway encrypted body was too short")
    try:
        return AESGCM(key.encode("utf-8")).decrypt(raw[:_NONCE_LENGTH], raw[_NONCE_LENGTH:], None)
    except InvalidTag as err:
        raise NativeGatewayProtocolError("Gateway encrypted body failed authentication") from err


def _build_sign_string(params: dict[str, str]) -> str:
    return "||".join(
        f"{key}={params[key]}" for key in sorted(params) if params[key] and key in _SIGN_WHITELIST
    )


def _sign(sign_string: str, key: str) -> str:
    return hmac.new(key.encode("utf-8"), sign_string.encode("utf-8"), hashlib.sha256).hexdigest()


def _post_data_sign_field(encrypted_post_data: str) -> str:
    return _swap_sign_string(_md5_hex(encrypted_post_data))


def _swap_sign_string(value: str) -> str:
    if len(value) != _MD5_HEX_LENGTH:
        return value
    return value[8:16] + value[0:8] + value[24:32] + value[16:24]


def _body_key(request_id: str, ecode: str, key: str) -> str:
    return _hmac_sha256_hex(request_id, f"{key}_{ecode}")[:_BODY_KEY_LENGTH]


def _pre_login_body_key(request_id: str, key: str) -> str:
    return _hmac_sha256_hex(request_id, key)[:_BODY_KEY_LENGTH]


def _md5_hex(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.md5(raw, usedforsecurity=False).hexdigest()


def _hmac_sha256_hex(key: str | bytes, message: str | bytes) -> str:
    raw_key = key.encode("utf-8") if isinstance(key, str) else key
    raw_message = message.encode("utf-8") if isinstance(message, str) else message
    return hmac.new(raw_key, raw_message, hashlib.sha256).hexdigest()


def _optional_str(source: dict[str, Any], key: str) -> str:
    value = source.get(key)
    return value if isinstance(value, str) else ""


def _required_text(source: dict[str, Any], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(key)
    return value
