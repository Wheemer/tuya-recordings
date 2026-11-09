"""Tuya consumer-app mobile gateway caller for APK-native camera bootstrap.

This is the missing app-session side of the native path. It implements the
signed, encrypted `et=3` mobile gateway request shape used by Tuya consumer app SDKs
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

KNOWN_CAMERA_CATEGORIES = {"sp", "dghsxj"}
KNOWN_CAMERA_CATEGORY_HINTS = ("camera", "ipc", "cam", "doorbell")
CAMERA_NAME_HINTS = ("camera", "doorbell", "lobby", "ipc", "ip camera")


def is_camera_inventory_record(category: str, name: str, product_id: str) -> bool:
    category = category.strip().lower()
    return (
        category in KNOWN_CAMERA_CATEGORIES
        or any(hint in category for hint in KNOWN_CAMERA_CATEGORY_HINTS)
        or any(hint in value.lower() for value in (name, product_id) for hint in CAMERA_NAME_HINTS)
    )

@dataclass(frozen=True, slots=True)
class NativeAppProfile:
    """APK-derived mobile gateway identity for one Tuya consumer app."""

    id: str
    package_name: str
    certificate_sha256: str
    security_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    client_id: str
    ch_key: str
    app_version: str
    sdk_version: str
    ttid: str
    qr_scheme: str
    qr_create_api: str = "thing.m.user.qr.token.create"
    qr_finish_api: str = "thing.m.user.qr.token.user.get"

    @property
    def composite_key(self) -> str:
        return "_".join(
            (self.package_name, self.certificate_sha256, self.security_key, self.app_secret)
        )


SMART_LIFE_PROFILE = NativeAppProfile(
    id="smart_life",
    package_name="com.tuya.smartlife",
    certificate_sha256=(
        "0F:C3:61:99:9C:C0:C3:5B:A8:AC:A5:7D:AA:55:93:A2"
        ":0C:F5:57:27:70:2E:A8:5A:D7:B3:22:89:49:F8:88:FE"
    ),
    security_key="jfg5rs5kkmrj5mxahugvucrsvw43t48x",
    app_secret="r3me7ghmxjevrvnpemwmhw3fxtacphyg",  # noqa: S105
    client_id="ekmnwp9f5pnh3trdtpgy",
    ch_key="ec9709a4",
    app_version="7.11.6",
    sdk_version="7.11.0",
    ttid="smartlife",
    qr_scheme="tuyaSmart",
)

TUYA_SMART_PROFILE = NativeAppProfile(
    id="tuya_smart",
    package_name="com.tuya.smart",
    certificate_sha256=(
        "93:21:9F:C2:73:E2:20:0F:4A:DE:E5:F7:19:1D:C6:56"
        ":BA:2A:2D:7B:2F:F5:D2:4C:D5:5C:4B:61:55:00:1E:40"
    ),
    security_key="f3hd7pet4p83kemjdf5wqsa5tavrv579",
    app_secret="5gdtanjtf38vyxkqh87cjwfcqjhvjjqa",  # noqa: S105
    client_id="3cxxt3au9x33ytvq3h9j",
    ch_key="3f7060ea",
    app_version="7.11.0",
    sdk_version="7.11.0",
    ttid="tuyaSmart",
    qr_scheme="tuyaSmart",
)

NATIVE_APP_PROFILES = {
    SMART_LIFE_PROFILE.id: SMART_LIFE_PROFILE,
    TUYA_SMART_PROFILE.id: TUYA_SMART_PROFILE,
}
DEFAULT_APP_PROFILE_ID = SMART_LIFE_PROFILE.id
COMPOSITE_KEY = SMART_LIFE_PROFILE.composite_key
PACKAGE_NAME = SMART_LIFE_PROFILE.package_name
CLIENT_ID = SMART_LIFE_PROFILE.client_id
CH_KEY = SMART_LIFE_PROFILE.ch_key
APP_VERSION = SMART_LIFE_PROFILE.app_version
SDK_VERSION = SMART_LIFE_PROFILE.sdk_version
TTID = SMART_LIFE_PROFILE.ttid
LANGUAGE = "en_US"


def native_app_profile(profile_id: str | None) -> NativeAppProfile:
    """Return an APK-derived profile, defaulting old entries to Smart Life."""
    try:
        return NATIVE_APP_PROFILES[profile_id or DEFAULT_APP_PROFILE_ID]
    except KeyError as err:
        raise NativeGatewayError("Unknown Tuya mobile app profile") from err
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
    """Tuya consumer-app mobile gateway request failed."""


class NativeGatewayAuthError(NativeGatewayError):
    """Tuya mobile app session is invalid or expired."""


class NativeGatewayProtocolError(NativeGatewayError):
    """Tuya consumer-app mobile gateway response was malformed."""


def generate_device_fingerprint() -> str:
    """Create one stable app-device identity for a new config entry."""
    return secrets.token_urlsafe(33)


@dataclass(slots=True)
class NativeAppGatewayClient:
    """Synchronous Tuya consumer-app mobile gateway client.

    Home Assistant calls this from executor jobs, so it deliberately uses the
    existing synchronous `requests` stack rather than owning an event loop.
    """

    region: str = "us"
    device_fingerprint: str = field(default_factory=generate_device_fingerprint)
    app_profile_id: str = DEFAULT_APP_PROFILE_ID
    session: requests.Session = field(default_factory=requests.Session, repr=False)

    @property
    def app_profile(self) -> NativeAppProfile:
        return native_app_profile(self.app_profile_id)

    @property
    def composite_key(self) -> str:
        return self.app_profile.composite_key

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
        local_keys, _protocol_versions, _devices = self.device_connection_inventory_data(saved)
        return local_keys

    def device_connection_data(
        self, saved: Any
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Return current device local keys and MQTT protocol versions."""
        local_keys, protocol_versions, _devices = self.device_connection_inventory_data(saved)
        return local_keys, protocol_versions

    def device_connection_inventory_data(
        self, saved: Any, *, refresh_protocols: bool = False,
    ) -> tuple[dict[str, str], dict[str, str], list[dict[str, Any]]]:
        """Capture the APK-native account inventory at setup or reauthorization."""
        app_session = _saved_session(saved)
        homes = self.call("tuya.m.location.list", "2.1", {}, app_session)
        if not isinstance(homes, list):
            raise NativeGatewayProtocolError("Tuya mobile app home list was malformed")
        found: dict[str, str] = {}
        protocol_versions: dict[str, str] = {}
        inventory: list[dict[str, Any]] = []
        home_rows: dict[int, list[dict[str, Any]]] = {}
        camera_details: dict[str, tuple[int, dict[str, Any]]] = {}
        seen: set[int] = set()
        for home in homes:
            if not isinstance(home, dict) or type(home.get("gid")) is not int:
                continue
            gid = home["gid"]
            if gid in seen:
                continue
            seen.add(gid)
            devices = self.call(
                "tuya.m.my.group.device.list", "1.0", {}, app_session, {"gid": str(gid)}
            )
            if not isinstance(devices, list):
                raise NativeGatewayProtocolError("Tuya mobile app device list was malformed")
            for device in devices:
                if not isinstance(device, dict):
                    continue
                dev_id = device.get("devId")
                local_key = device.get("localKey")
                if not (
                    isinstance(dev_id, str) and dev_id.strip()
                    and isinstance(local_key, str) and local_key.strip()
                ):
                    continue
                dev_id = dev_id.strip()
                found[dev_id] = local_key.strip()
                protocol_version = _device_mqtt_protocol_version(device)
                if protocol_version is not None:
                    protocol_versions[dev_id] = protocol_version
                product_id = str(device.get("productId") or device.get("product_id") or "")
                wake = _product_wake_capability(device.get("productRefBean"))
                row = {
                    "devId": dev_id,
                    "name": str(device.get("name") or device.get("deviceName") or dev_id),
                    "category": str(device.get("category") or ""),
                    "productId": product_id,
                    "online": device.get("online"),
                    "sdWakeEvidence": _sd_wake_evidence(device, dev_id, "group"),
                }
                if wake is not None:
                    row["wakeCapability"] = wake
                communication = device.get("communication")
                if (lan_capable := _has_lan_communication(device)) is not None:
                    row["lanSignalingCapable"] = lan_capable
                if isinstance(communication, dict):
                    row["wakeDirect"] = communication.get("communicationNode") in (None, "", dev_id)
                elif isinstance(device.get("communicationId"), str):
                    row["wakeDirect"] = device["communicationId"] == dev_id
                if wake in (None, "none") and _supports_direct_wireless_wake(device, dev_id):
                    row["wakeCapability"] = "wireless"
                if (eligible := _sd_low_power_wake(device, dev_id)) is not None:
                    row["sdLowPowerWake"] = eligible
                if eligible and (dp := _sd_status_mqtt_dp(device)):
                    row["sdStatusMqttDp"] = dp
                elif eligible and (dp := _sd_status_http_dp(device)):
                    row["sdStatusHttpDp"] = dp
                if eligible and (dp := _sd_status_lan_dp(device)):
                    row["sdStatusLanDp"] = dp
                    if route := _sd_status_lan_error_route(device):
                        row["sdStatusLanErrorRoute"] = route
                inventory.append(row)
                if is_camera_inventory_record(row["category"], row["name"], product_id):
                    if product_id:
                        home_rows.setdefault(gid, []).append(row)
                    if refresh_protocols:
                        camera_details.setdefault(dev_id, (gid, row))
        product_ids = {row["productId"] for row in inventory if row["productId"]}
        references = self._product_wake_capabilities(product_ids, app_session)
        for row in inventory:
            if (wake := references.get(row["productId"])) is not None and (
                wake != "none" or row.get("wakeCapability") in (None, "none")
            ):
                row["wakeCapability"] = wake
        home_products: dict[int, dict[str, dict[str, Any]]] = {}
        for gid, rows in home_rows.items():
            unresolved = [
                row for row in rows
                if row.get("wakeCapability") in (None, "none") or "wakeDirect" not in row
                or refresh_protocols
                or row["devId"] not in protocol_versions
                or "sdLowPowerWake" not in row
                or (row.get("sdLowPowerWake") is True
                    and not {"sdStatusMqttDp", "sdStatusHttpDp", "sdStatusLanDp"} & row.keys())
            ]
            if unresolved:
                home_products[gid] = self._enrich_camera_wake_from_home(
                    gid, unresolved, app_session, protocol_versions,
                )
        schema_details: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for dev_id, (gid, row) in camera_details.items():
            if (dev_id in protocol_versions and "sdLowPowerWake" in row
                    and (row["sdLowPowerWake"] is not True
                         or {"sdStatusMqttDp", "sdStatusHttpDp", "sdStatusLanDp"} & row.keys())):
                continue
            row["sdWakeDetailLookup"] = "requested"
            try:
                detail = self.call(
                    "thing.m.device.get", "4.1", {"devId": dev_id}, app_session, {"gid": str(gid)}
                )
            except NativeGatewayAuthError:
                raise
            except NativeGatewayError:
                row["sdWakeDetailLookup"] = "failed"
                continue
            if not isinstance(detail, dict) or detail.get("devId") != dev_id:
                row["sdWakeDetailLookup"] = "invalid_device"
                continue
            product_id = row["productId"]
            detail_product = detail.get("productId")
            if product_id and detail_product and detail_product != product_id:
                row["sdWakeDetailLookup"] = "product_mismatch"
                continue
            if pv := _device_mqtt_protocol_version(detail, allow_legacy=dev_id not in protocol_versions):
                protocol_versions[dev_id] = pv
            product = home_products.get(gid, {}).get(product_id)
            if product and _optional_str(product, "productVer") != _optional_str(detail, "productVer"):
                product = None
            row["sdWakeDetailLookup"] = "matched"
            _apply_sd_wake_metadata(
                row, {**detail, "productBean": product} if product else detail, dev_id, source="detail",
            )
            if not _product_schema(product or {}):
                schema_details.append((row, detail))
        if schema_details:
            self._resolve_product_schemas(schema_details, app_session)
        return found, protocol_versions, inventory

    def _resolve_product_schemas(
        self, devices: list[tuple[dict[str, Any], dict[str, Any]]],
        app_session: NativeAppSession,
    ) -> None:
        """Load missing product schemas using the APK's versioned product lookup."""
        requests: dict[tuple[str, str | None], dict[str, str]] = {}
        for row, detail in devices:
            pid = _optional_str(detail, "productId")
            ver = _optional_str(detail, "productVer") or None
            if pid:
                requests.setdefault((pid, ver), {"pid": pid, **({"ver": ver} if ver else {})})
                row["sdSchemaLookup"] = "requested"
        keys = list(requests)
        for offset in range(0, len(keys), 20):
            batch = keys[offset:offset + 20]
            try:
                products = self.call(
                    "thing.m.device.ref.info.list", "6.1",
                    {"productIds": _dump_json([requests[key] for key in batch]).decode("utf-8"),
                     "zigbeeGroup": True},
                    app_session,
                )
            except NativeGatewayAuthError:
                raise
            except NativeGatewayError:
                products = None
            for row, detail in devices:
                key = (_optional_str(detail, "productId"), _optional_str(detail, "productVer") or None)
                if key not in batch:
                    continue
                row["sdSchemaLookup"] = "unavailable"
                if not isinstance(products, list):
                    continue
                matches = [
                    product for product in products
                    if isinstance(product, dict) and product.get("id") == key[0]
                    and (_optional_str(product, "productVer") or None) == key[1]
                ]
                if len(matches) != 1 or not _product_schema(matches[0]):
                    continue
                row["sdSchemaLookup"] = "matched"
                _apply_sd_wake_metadata(
                    row, {**detail, "productBean": matches[0]}, row["devId"], source="detail",
                )

    def _enrich_camera_wake_from_home(
        self, gid: int, rows: list[dict[str, Any]], app_session: NativeAppSession,
        protocol_versions: dict[str, str],
    ) -> dict[str, dict[str, Any]]:
        """Join the app's separately cached device, product and reference records."""
        try:
            result = self.call(
                "m.life.app.smart.local.device.list", "1.1",
                {"homeId": gid, "groupType": "homeGroup"}, app_session,
            )
        except NativeGatewayError:
            for row in rows:
                row["sdWakeHomeLookup"] = "failed"
            return {}
        if not isinstance(result, dict):
            for row in rows:
                row["sdWakeHomeLookup"] = "malformed_response"
            return {}

        def index(field: str, key: str) -> dict[str, dict[str, Any]]:
            records = result.get(field)
            if not isinstance(records, list):
                return {}
            return {
                identifier: record for record in records
                if isinstance(record, dict) and (identifier := _optional_str(record, key))
            }

        devices = index("deviceList", "devId")
        products = index("productList", "id")
        references = index("productExtList", "productId")
        for row in rows:
            dev_id, product_id = row["devId"], row["productId"]
            device = devices.get(dev_id)
            if device is None:
                row["sdWakeHomeLookup"] = "device_absent"
                continue
            if device.get("productId") != product_id:
                row["sdWakeHomeLookup"] = "product_mismatch"
                continue
            row["sdWakeHomeLookup"] = "matched"
            if pv := _device_mqtt_protocol_version(device):
                protocol_versions[dev_id] = pv
            product = products.get(product_id)
            if product and _optional_str(product, "productVer") != _optional_str(device, "productVer"):
                product = None
            resolved = {**device, "productBean": product}
            _apply_sd_wake_metadata(row, resolved, dev_id)
            wake = _product_wake_capability(references.get(product_id))
            if wake not in (None, "none"):
                row["wakeCapability"] = wake
            elif row.get("wakeCapability") in (None, "none"):
                if _supports_direct_wireless_wake(resolved, dev_id):
                    row["wakeCapability"] = "wireless"
        return products

    def _product_wake_capabilities(
        self, product_ids: set[str], app_session: NativeAppSession,
    ) -> dict[str, str]:
        if not product_ids:
            return {}
        try:
            references = self.call(
                "thing.m.device.product.ref.list", "1.0",
                {"productIds": _dump_json(sorted(product_ids)).decode("utf-8")},
                app_session,
            )
        except NativeGatewayError:
            # Optional diagnostics must not block inventory or authorization.
            return {}
        if not isinstance(references, list):
            return {}
        return {
            pid: wake for ref in references if isinstance(ref, dict)
            if (pid := _optional_str(ref, "productId")) in product_ids
            and (wake := _product_wake_capability(ref)) is not None
        }

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
            "clientId": self.app_profile.client_id,
            "time": str(int(time.time())),
            "requestId": request_id,
            "lang": LANGUAGE,
            "deviceId": self.device_fingerprint,
            "appVersion": self.app_profile.app_version,
            "ttid": self.app_profile.ttid,
            "os": "Android",
            "sdkVersion": self.app_profile.sdk_version,
            "chKey": self.app_profile.ch_key,
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
                    "User-Agent": f"Thing-UA=APP/Android/{self.app_profile.app_version}/SDK/{self.app_profile.sdk_version}",
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


def _supports_direct_wireless_wake(device: dict[str, Any], dev_id: str) -> bool:
    """Recognize the APK provider-2 fallback from a resolved product schema."""
    product = device.get("productBean")
    return (
        isinstance(product, dict) and _is_provider_two(device, dev_id) is True
        and any(entry.get("code") == "wireless_awake" for entry in _product_schema(product))
    )


def _sd_wake_evidence(device: dict[str, Any], dev_id: str, source: str) -> dict[str, Any]:
    """Describe missing APK predicate inputs without retaining DP values or keys."""
    communication = device.get("communication")
    target = communication.get("communicationNode") if isinstance(communication, dict) else device.get("communicationId")
    provider = _is_provider_two(device, dev_id)
    info = device.get("dataPointInfo")
    dps = info.get("dps") if isinstance(info, dict) else device.get("dps")
    product = device.get("productBean")
    return {
        "source": source,
        "route": "missing" if not isinstance(target, str) or not target else "direct" if target == dev_id else "hub",
        "provider_two": provider,
        "dp149": ("present" if "149" in dps else "absent") if isinstance(dps, dict) else "missing",
        "sd_status_schema": bool(_sd_status_schema_id(product)) if isinstance(product, dict) else False,
    }


def _apply_sd_wake_metadata(
    row: dict[str, Any], device: dict[str, Any], dev_id: str, *, source: str = "home",
) -> None:
    row["sdWakeEvidence"] = _sd_wake_evidence(device, dev_id, source)
    if (lan_capable := _has_lan_communication(device)) is not None:
        row["lanSignalingCapable"] = lan_capable
    communication = device.get("communication")
    target = communication.get("communicationNode") if isinstance(communication, dict) else device.get("communicationId")
    if isinstance(target, str) and target:
        row["wakeDirect"] = target == dev_id
    eligible = _sd_low_power_wake(device, dev_id)
    if eligible is None:
        return
    row["sdLowPowerWake"] = eligible
    row["sdStatusMqttCapable"] = bool(_sd_status_mqtt_dp({**device, "cloudOnline": True})) if eligible else False
    for key in ("sdStatusMqttDp", "sdStatusHttpDp", "sdStatusLanDp", "sdStatusLanErrorRoute"):
        row.pop(key, None)
    if not eligible:
        return
    if dp := _sd_status_mqtt_dp(device):
        row.pop("sdStatusHttpDp", None)
        row["sdStatusMqttDp"] = dp
    elif dp := _sd_status_http_dp(device):
        row.pop("sdStatusMqttDp", None)
        row["sdStatusHttpDp"] = dp
    if dp := _sd_status_lan_dp(device):
        row["sdStatusLanDp"] = dp
        row.pop("sdStatusLanErrorRoute", None)
        if route := _sd_status_lan_error_route(device):
            row["sdStatusLanErrorRoute"] = route


def _has_lan_communication(device: dict[str, Any]) -> bool | None:
    """Read DeviceBean.hasLanCommunication independently of SD wake support."""
    communication = device.get("communication")
    modes = communication.get("communicationModes") if isinstance(communication, dict) else None
    if not isinstance(modes, list) or not modes or any(
        not isinstance(mode, dict) or type(mode.get("type")) is not int for mode in modes
    ):
        return None
    return any(mode["type"] == 0 for mode in modes)


def _is_provider_two(device: dict[str, Any], dev_id: str) -> bool | None:
    communication = device.get("communication")
    target = (
        communication.get("communicationNode")
        if isinstance(communication, dict) else device.get("communicationId")
    )
    if not target:
        return None
    if target != dev_id or device.get("virtual"):
        return False
    product = device.get("productBean")
    skills = device.get("skills")
    if skills is None:
        meta = device.get("meta")
        if isinstance(meta, dict) and "isSupportDirectlyDevice" in meta:
            return True
        return product.get("category") != "sp" if isinstance(product, dict) else None
    p2p_type = skills.get("p2pType") if isinstance(skills, dict) else None
    return (
        type(p2p_type) in (int, float) and (p2p_type < 1 or p2p_type >= 2)
        and device.get("uiName") != "singleHalfFisheyeCamera"
    )


def _sd_low_power_wake(device: dict[str, Any], dev_id: str) -> bool | None:
    """Match the SD presenter's built-in provider-2 and DP-149 predicate."""
    provider = _is_provider_two(device, dev_id)
    if provider is not True:
        return provider
    info = device.get("dataPointInfo")
    dps = info.get("dps") if isinstance(info, dict) else device.get("dps")
    return "149" in dps if isinstance(dps, dict) else None


def _product_schema(product: dict[str, Any]) -> list[dict[str, Any]]:
    schema_info = product.get("schemaInfo")
    schema = schema_info.get("schema") if isinstance(schema_info, dict) else None
    if not isinstance(schema, str):
        return []
    try:
        entries = json.loads(schema)
    except ValueError:
        return []
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def _sd_status_mqtt_dp(device: dict[str, Any]) -> str:
    """Select modern MQTT when no higher-priority route is advertised."""
    communication = device.get("communication")
    product = device.get("productBean")
    if not isinstance(communication, dict) or not isinstance(product, dict):
        return ""
    modes = _sd_status_modes(communication)
    if (8 in modes or 1 not in modes or device.get("cloudOnline") is not True
            or _device_mqtt_protocol_version(device) == "1.1"):
        return ""
    return _sd_status_schema_id(product) if _modern_dp_payload(device) else ""


def _modern_dp_payload(device: dict[str, Any]) -> bool:
    cadv = device.get("gatewayVerCAD") or device.get("cadv")
    if not isinstance(cadv, str):
        return False
    try:
        return tuple(int(part) for part in cadv.split(".")) >= (1, 0, 2)
    except ValueError:
        return False


def _sd_status_lan_error_route(device: dict[str, Any]) -> str:
    """The LAN error callback enters sendCommand, not initial route selection."""
    pv = _device_mqtt_protocol_version(device)
    if pv == "1.0":
        return "http"
    if pv in {"2.0", "2.1", "2.2", "2.3"} and _modern_dp_payload(device):
        return "mqtt"
    return ""


def _sd_status_http_dp(device: dict[str, Any]) -> str:
    """Recognize a DP device with no advertised LAN, Matter or MQTT route."""
    communication = device.get("communication")
    product = device.get("productBean")
    if not isinstance(communication, dict) or not isinstance(product, dict):
        return ""
    modes = _sd_status_modes(communication)
    if not modes or 8 in modes or (1 in modes and device.get("cloudOnline") is True):
        return ""
    return _sd_status_schema_id(product)


def _sd_status_lan_dp(device: dict[str, Any]) -> str:
    communication, product = device.get("communication"), device.get("productBean")
    if isinstance(communication, dict) and isinstance(product, dict):
        if 0 in _sd_status_modes(communication):
            return _sd_status_schema_id(product)
    return ""


def _sd_status_modes(communication: dict[str, Any]) -> set[int]:
    modes = communication.get("communicationModes")
    if type(communication.get("dataModel")) is not int or communication["dataModel"] != 1:
        return set()
    if not isinstance(modes, list) or not modes or any(
        not isinstance(mode, dict) or type(mode.get("type")) is not int or mode["type"] not in (0, 1, 2, 8)
        for mode in modes
    ):
        return set()
    return {mode["type"] for mode in modes}


def _sd_status_schema_id(product: dict[str, Any]) -> str:
    matches = [entry.get("id") for entry in _product_schema(product) if entry.get("code") == "sd_status"]
    dp = matches[0] if len(matches) == 1 else None
    if type(dp) is int and dp > 0:
        dp = str(dp)
    return dp if isinstance(dp, str) and dp.isascii() and dp.isdecimal() else ""


def _product_wake_capability(reference: Any) -> str | None:
    metadata = reference.get("configMetas") if isinstance(reference, dict) else None
    if not isinstance(metadata, dict):
        return None
    low_power = "low_power_wakeup" in metadata
    remote = "remote_wakeup" in metadata
    if low_power and remote:
        return "low_power_and_remote"
    return "low_power" if low_power else "remote" if remote else "none"


def _device_mqtt_protocol_version(device: dict[str, Any], *, allow_legacy: bool = True) -> str | None:
    """Extract an explicit APK DeviceBean MQTT protocol version."""
    version = None
    communication = device.get("communication")
    modes = communication.get("communicationModes") if isinstance(communication, dict) else None
    if isinstance(modes, list):
        for mode in modes:
            if not isinstance(mode, dict):
                continue
            kind = mode.get("type")
            # Both APKs populate DeviceBean.pv in this order-sensitive loop.
            if type(kind) is int and (kind == 1 or (kind in {2, 3} and not version)):
                version = mode.get("pv")
    if (version is None or version == "") and allow_legacy:
        version = device.get("pv")
    if not isinstance(version, str | int | float):
        return None
    normalized = str(version).strip()
    try:
        if not normalized or not 1.0 <= float(normalized) < float("inf"):
            return None
    except ValueError:
        return None
    return normalized


def _saved_session(value: Any) -> NativeAppSession:
    if isinstance(value, NativeAppSession):
        return value
    if not isinstance(value, dict):
        raise NativeGatewayAuthError("Tuya mobile app-session authorization is not configured")
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
        raise NativeGatewayAuthError("Tuya mobile app-session authorization is incomplete") from err


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
