"""Tuya OpenAPI helpers for IPC recording bootstrap."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import requests


class TuyaOpenApiError(RuntimeError):
    """Error returned by Tuya OpenAPI."""

    def __init__(self, path: str, payload: dict[str, Any] | None = None) -> None:
        self.path = path
        self.payload = payload or {}
        code = self.payload.get("code") or "unknown"
        message = self.payload.get("msg") or "Tuya OpenAPI request failed"
        super().__init__(f"{path}: {code} {message}")


class TuyaOpenApiAuthError(TuyaOpenApiError):
    """Tuya OpenAPI credentials are invalid or expired."""


class TuyaOpenApiClient:
    """Small sync Tuya OpenAPI client using the official HMAC-SHA256 signing flow."""

    def __init__(self, *, region: str, client_id: str, client_secret: str, user_id: str) -> None:
        self.region = region
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_id = user_id
        self.base_url = _base_url(region)
        self.session = requests.Session()
        self._access_token = ""
        self._token_expires_at = 0.0
        self._hub_config: dict[str, Any] | None = None
        self._hub_config_expires_at = 0.0

    def get_devices(self) -> list[dict[str, Any]]:
        result = self.get(f"/v1.0/users/{self.user_id}/devices")
        return result if isinstance(result, list) else []

    def get_webrtc_config(self, device_id: str) -> dict[str, Any]:
        result = self.get(f"/v1.0/users/{self.user_id}/devices/{device_id}/webrtc-configs")
        if not isinstance(result, dict):
            raise TuyaOpenApiError("webrtc-configs", {"code": "invalid_result", "msg": "Invalid WebRTC config result"})
        return _normalize_webrtc_config(result)

    def get_open_iot_hub_config(self) -> dict[str, Any]:
        if self._hub_config is not None and time.time() < self._hub_config_expires_at - 60:
            return dict(self._hub_config)
        result = self.post(
            "/v2.0/open-iot-hub/access/config",
            {
                "uid": self.user_id,
                "unique_id": "tuya-recordings",
                "link_type": "mqtt",
                "topics": "ipc",
            },
        )
        if not isinstance(result, dict):
            raise TuyaOpenApiError("open-iot-hub", {"code": "invalid_result", "msg": "Invalid MQTT config result"})
        normalized = _normalize_mqtt_config(result)
        self._hub_config = normalized
        self._hub_config_expires_at = time.time() + int(result.get("expire_time") or 3600)
        return dict(normalized)

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self._request("POST", path, body)

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if path.startswith("/v1.0/token"):
            token = ""
        else:
            token = self._token()
        body_text = json.dumps(body, separators=(",", ":")) if body is not None else ""
        timestamp = str(int(time.time() * 1000))
        content_hash = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
        sign_payload = (
            f"{self.client_id}{token}{timestamp}"
            f"{method}\n{content_hash}\n\n/{path.lstrip('/')}"
        )
        headers = {
            "client_id": self.client_id,
            "sign": _sign(sign_payload, self.client_secret),
            "t": timestamp,
            "sign_method": "HMAC-SHA256",
        }
        if token:
            headers["access_token"] = token
        if body_text:
            headers["Content-Type"] = "application/json"

        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            data=body_text if body_text else None,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("success"):
            error = TuyaOpenApiAuthError if isinstance(payload, dict) and payload.get("code") in {"1010", "1011", "1012", "1013", "1014"} else TuyaOpenApiError
            raise error(path, payload if isinstance(payload, dict) else None)
        return payload.get("result")

    def _token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token
        result = self._request("GET", "/v1.0/token?grant_type=1")
        if not isinstance(result, dict) or not result.get("access_token"):
            raise TuyaOpenApiAuthError("/v1.0/token", {"code": "invalid_token", "msg": "Invalid token result"})
        self._access_token = str(result["access_token"])
        self._token_expires_at = time.time() + int(result.get("expire_time") or 3600)
        return self._access_token


def _base_url(region: str) -> str:
    if region == "ea":
        return "https://openapi-ueaz.tuyaus.com"
    if region == "we":
        return "https://openapi-weaz.tuyaeu.com"
    if region == "sg":
        return "https://openapi-sg.iotbing.com"
    return f"https://openapi.tuya{region}.com"


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode("latin-1"), payload.encode("latin-1"), hashlib.sha256).hexdigest().upper()


def _normalize_webrtc_config(result: dict[str, Any]) -> dict[str, Any]:
    p2p_config = dict(result.get("p2p_config") or result.get("p2pConfig") or {})
    moto_id = result.get("moto_id") or result.get("motoId") or p2p_config.get("moto_id") or p2p_config.get("motoId")
    return {
        **result,
        "auth": result.get("auth") or p2p_config.get("auth"),
        "motoId": moto_id,
        "moto_id": moto_id,
        "p2pType": result.get("p2p_type") or result.get("p2pType"),
        "p2pConfig": p2p_config,
        "protocolVersion": result.get("protocol_version") or result.get("protocolVersion") or "2.3",
        "supportWebrtcRecord": result.get("support_webrtc_record") if "support_webrtc_record" in result else result.get("supportWebrtcRecord"),
        "supportsWebrtc": result.get("supports_webrtc") if "supports_webrtc" in result else result.get("supportsWebrtc"),
    }


def _normalize_mqtt_config(result: dict[str, Any]) -> dict[str, Any]:
    source_topic = result.get("source_topic") or result.get("sourceTopic") or {}
    source_ipc = source_topic.get("ipc") if isinstance(source_topic, dict) else ""
    msid = str(source_ipc).rsplit("/av/u/", 1)[-1] if "/av/u/" in str(source_ipc) else result.get("username", "")
    return {
        **result,
        "msid": msid,
        "password": result.get("password"),
        "client_id": result.get("client_id") or result.get("clientId"),
        "username": result.get("username"),
        "url": result.get("url"),
        "sink_topic": result.get("sink_topic") or result.get("sinkTopic"),
        "source_topic": source_topic,
    }
