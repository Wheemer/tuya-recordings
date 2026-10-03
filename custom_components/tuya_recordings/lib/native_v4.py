"""Smart Life camera V4 playback-control messages recovered from the APK."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

PLAYBACK_VIEW_CONTROL = "playback.record.view.control"
PLAYBACK_START = "start"
PLAYBACK_QUERY_DAY = "playback.record.query.day"
PLAYBACK_QUERY_DAY_RESPONSE = f"{PLAYBACK_QUERY_DAY}.resp"
BUSINESS_COMMAND_ENABLE = "bizcommand.enable"
CONNECTION_OFFER = "offer"

_MAX_SIGNALING_MESSAGE_BYTES = 0xFFC0


class NativeV4Error(ValueError):
    """A V4 request cannot be represented by the APK's wire model."""


@dataclass(frozen=True, slots=True)
class NativeV4RequestContext:
    """Per-session fields registered by ``ThingP2P4RequestBase``."""

    request_id: int
    timestamp_ms: int
    client_id: str
    session_id: int
    source: str
    destination: str
    username: str
    password: str = field(repr=False)
    camera_channel: int = 0
    function: int = 0
    mode: int = 1
    version: int = 1

    def validate(self) -> NativeV4RequestContext:
        _integer("request_id", self.request_id, 0x7FFFFFFF)
        _integer("timestamp_ms", self.timestamp_ms, 0x7FFFFFFFFFFFFFFF)
        _integer("session_id", self.session_id, 0x7FFFFFFFFFFFFFFF)
        _integer("camera_channel", self.camera_channel, 0x7FFFFFFF)
        _integer("function", self.function, 0x7FFFFFFF)
        _integer("mode", self.mode, 0x7FFFFFFF)
        _integer("version", self.version, 0x7FFFFFFF)
        for name, value in (
            ("client_id", self.client_id),
            ("source", self.source),
            ("destination", self.destination),
            ("username", self.username),
            ("password", self.password),
        ):
            _text(name, value)
        return self


@dataclass(frozen=True, slots=True)
class NativeV4Recording:
    """One ``Record`` item registered by the camera SDK."""

    start_time: int
    end_time: int
    event_type: int = 0
    video_type: int = 0
    encrypt: int = 0
    uuid: str = ""
    encrypt_md5: str = ""
    event_types: tuple[int, ...] = ()

    def validate(self) -> NativeV4Recording:
        _integer("start_time", self.start_time, 0x7FFFFFFFFFFFFFFF)
        _integer("end_time", self.end_time, 0x7FFFFFFFFFFFFFFF)
        if self.end_time <= self.start_time:
            raise NativeV4Error("Recording end_time must be after start_time")
        for name, value in (
            ("event_type", self.event_type),
            ("video_type", self.video_type),
            ("encrypt", self.encrypt),
        ):
            _integer(name, value, 0x7FFFFFFF)
        if not isinstance(self.event_types, tuple):
            raise NativeV4Error("event_types is invalid")
        for event_type in self.event_types:
            _integer("event_types", event_type, 0x7FFFFFFF)
        for name, value in (("uuid", self.uuid), ("encrypt_md5", self.encrypt_md5)):
            if not isinstance(value, str) or "\0" in value:
                raise NativeV4Error(f"{name} is invalid")
        return self

    def as_recording(self) -> dict[str, Any]:
        self.validate()
        recording = {
            "start_time": self.start_time,
            "end_time": self.end_time,
            "event_type": self.event_type,
            "video_type": self.video_type,
            "encrypt": self.encrypt,
            "uuid": self.uuid,
            "encrypt_md5": self.encrypt_md5,
        }
        if self.event_types:
            recording["event_types"] = list(self.event_types)
        return recording


@dataclass(frozen=True, slots=True)
class NativeV4CatalogPage:
    """Validated ``QueryRecordByDayResponse`` payload."""

    response_id: int
    status: int
    error_message: str
    total_pages: int
    total_files: int
    page: int
    items: tuple[NativeV4Recording, ...]


def encode_business_enable_request(
    context: NativeV4RequestContext,
    *,
    channel_id: int = 9,
    channel_type: int = 2,
) -> bytes:
    """Serialize the V4 business-channel enable sent by ``ThingCameraV4``."""
    context.validate()
    _integer("channel_id", channel_id, 0x7FFFFFFF)
    _integer("channel_type", channel_type, 0x7FFFFFFF)
    payload = _request_base(context, BUSINESS_COMMAND_ENABLE)
    payload.update({"ch_id": channel_id, "ch_type": channel_type})
    return _json_bytes(payload)


def encode_connection_offer(
    *,
    client_id: str,
    source: str,
    destination: str,
    p2p_config: dict[str, Any],
    sdp: str,
    ice_ufrag: str,
    ice_password: str,
    candidates: tuple[str, ...] = (),
    tcp_token: dict[str, Any] | None = None,
) -> bytes:
    """Serialize the raw V4 offer returned by ``ThingCreateOffer``."""
    for name, value in (
        ("client_id", client_id),
        ("source", source),
        ("destination", destination),
        ("sdp", sdp),
        ("ice_ufrag", ice_ufrag),
        ("ice_password", ice_password),
    ):
        _text(name, value)
    if not isinstance(p2p_config, dict):
        raise NativeV4Error("p2p_config must be an object")
    if any(not isinstance(item, str) or not item or "\0" in item for item in candidates):
        raise NativeV4Error("Connection-offer candidates are invalid")

    payload: dict[str, Any] = {
        "av": 4,
        "cid": client_id,
        "from": source,
        "to": destination,
        "type": CONNECTION_OFFER,
        "mode": 1,
        "expired": _token_signed_integer(
            p2p_config,
            "expired",
            p2p_config.get("expire", 0),
        ),
        "sdp": sdp,
    }
    username = p2p_config.get("username")
    if isinstance(username, str) and username:
        payload["username"] = username
    for algorithm, secret_key, default in (
        ("crypt_algo", "crypt_key", 1),
        ("sign_algo", "sign_key", 2),
    ):
        secret = p2p_config.get(secret_key)
        if isinstance(secret, str) and secret:
            payload[algorithm] = _token_integer(p2p_config, algorithm, default)
            payload[secret_key] = secret

    raw_ice = p2p_config.get("ice_token")
    servers = raw_ice.get("servers") if isinstance(raw_ice, dict) else None
    if servers is None:
        servers = p2p_config.get("ices") or p2p_config.get("iceServers")
    normalized_servers = _normalize_ice_servers(servers)
    if normalized_servers:
        payload["ice_token"] = {
            "servers": normalized_servers,
            "ufrag": ice_ufrag,
            "password": ice_password,
        }
    selected_tcp = (
        tcp_token
        if tcp_token is not None
        else p2p_config.get("tcp_token") or p2p_config.get("tcpRelay")
    )
    normalized_tcp = _normalize_relay_token(selected_tcp, "tcp")
    if normalized_tcp:
        payload["tcp_token"] = normalized_tcp
    raw_udp = p2p_config.get("udp_token") or p2p_config.get("udpRelay")
    normalized_udp = _normalize_relay_token(raw_udp, "udp")
    if normalized_udp:
        payload["udp_token"] = normalized_udp
    for key, aliases in (
        ("moto_id", ("moto_id", "motoId")),
        ("channel", ("channel",)),
        ("log_token", ("log_token", "logToken", "log")),
    ):
        value = next((p2p_config.get(alias) for alias in aliases if p2p_config.get(alias)), None)
        if isinstance(value, (dict, str)) and value:
            payload[key] = value
    if candidates:
        payload["candidates"] = list(candidates)
    return _json_bytes(payload)


def _normalize_ice_servers(value: Any) -> list[dict[str, Any]]:
    """Reproduce ``sts_ice_server_unmarshal`` followed by its marshaller."""
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        url = item.get("urls") or item.get("url")
        if isinstance(url, list):
            url = next((entry for entry in url if isinstance(entry, str) and entry), "")
        if not isinstance(url, str) or not url or "\0" in url:
            continue
        server: dict[str, Any] = {"url": url[:127], "urls": url[:127]}
        if url.casefold().startswith(("turn:", "turns:")):
            username = item.get("username")
            credential = item.get("credential")
            if isinstance(username, str) and isinstance(credential, str):
                server["username"] = username[:63]
                server["credential"] = credential[:63]
                ttl = item.get("ttl", -1)
                if type(ttl) in {int, float}:
                    server["ttl"] = int(ttl)
        normalized.append(server)
    return normalized[:8]


def _normalize_relay_token(value: Any, scheme: str) -> dict[str, Any] | None:
    """Reproduce the native STS relay-token unmarshal/marshal round trip."""
    if not isinstance(value, dict) or scheme not in {"tcp", "udp"}:
        return None
    raw_urls = value.get("urls")
    if not isinstance(raw_urls, list):
        return None
    urls = [
        url[:128]
        for url in raw_urls
        if isinstance(url, str) and url and scheme in url.casefold()
    ][:8]
    if not urls:
        return None
    username = value.get("username")
    credential = value.get("credential")
    if not isinstance(username, str) or not isinstance(credential, str):
        return None
    key = value.get("key")
    if not isinstance(key, str) or not key:
        key = credential
    numeric_prefix = username.split(":", 1)[0]
    try:
        expire_time = int(numeric_prefix)
    except ValueError:
        expire_time = 0
    security_level = value.get("security_level", 3)
    if type(security_level) is not int:
        security_level = 3
    return {
        "urls": urls,
        "key": key[:16],
        "username": username[:64],
        "credential": credential[:64],
        "security_level": security_level,
        "expire_time": expire_time,
    }


def encode_playback_start_request(
    context: NativeV4RequestContext,
    *,
    stream_id: int,
    start: int,
    end: int,
    play_time: int | None = None,
    speed: int = 1,
) -> bytes:
    """Serialize the exact V4 request model used by ``StartPlayBack``.

    The native SDK registers all fields with its dynamic JSON serializer, adds
    a P2P stream, starts the playback reader, and then sends this JSON through
    ``ThingP2PSendData`` on channel zero. Both media flags are true in the APK.
    """
    context.validate()
    for name, value in (("stream_id", stream_id), ("start", start), ("end", end)):
        _integer(name, value, 0x7FFFFFFFFFFFFFFF)
    position = start if play_time is None else play_time
    _integer("play_time", position, 0x7FFFFFFFFFFFFFFF)
    _integer("speed", speed, 0x7FFFFFFF)
    if end <= start:
        raise NativeV4Error("Playback end must be after start")
    if not start <= position < end:
        raise NativeV4Error("Playback position must be inside clip bounds")

    payload = {
        "v": context.version,
        "t": context.timestamp_ms,
        "cid": context.client_id,
        "sid": context.session_id,
        "from": context.source,
        "to": context.destination,
        "mode": context.mode,
        "type": PLAYBACK_VIEW_CONTROL,
        "f": context.function,
        "reqid": context.request_id,
        "username": context.username,
        "passwd": context.password,
        "channel": context.camera_channel,
        "chan_id": stream_id,
        "start_time": start,
        "end_time": end,
        "play_time": position,
        "speed": speed,
        "audio": True,
        "video": True,
        "year": 0,
        "month": 0,
        "day": 0,
        "fragments": [],
        "operation": PLAYBACK_START,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def encode_recordings_day_request(
    context: NativeV4RequestContext,
    day: date,
) -> bytes:
    """Serialize ``QueryRecordByDayRequest`` exactly as registered by the SDK."""
    context.validate()
    if not isinstance(day, date):
        raise NativeV4Error("Recording catalog day must be a date")
    payload = _request_base(context, PLAYBACK_QUERY_DAY)
    payload.update({"year": day.year, "month": day.month, "day": day.day})
    return _json_bytes(payload)


def decode_recordings_day_response(
    payload: bytes,
    *,
    request_id: int | None = None,
) -> NativeV4CatalogPage:
    """Decode the APK's ``QueryRecordByDayResponse`` registered members."""
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise NativeV4Error("Recording catalog response is not valid JSON") from err
    if not isinstance(value, dict):
        raise NativeV4Error("Recording catalog response must be an object")
    if value.get("type") != PLAYBACK_QUERY_DAY_RESPONSE:
        raise NativeV4Error("Recording catalog response has the wrong type")
    response_id = _required_integer(value, "respid", 0x7FFFFFFF)
    if request_id is not None:
        _integer("request_id", request_id, 0x7FFFFFFF)
        if response_id != request_id:
            raise NativeV4Error("Recording catalog response ID does not match request")
    status = _required_integer(value, "status", 0x7FFFFFFF)
    error_message = value.get("errmsg", "")
    if not isinstance(error_message, str):
        raise NativeV4Error("Recording catalog errmsg is invalid")
    if status != 0:
        raise NativeV4Error(
            f"Recording catalog request failed with status {status}"
            + (f": {error_message}" if error_message else "")
        )
    total_pages = _required_integer(value, "total_page", 0x7FFFFFFF)
    total_files = _required_integer(value, "total_file", 0x7FFFFFFF)
    page = _required_integer(value, "page", 0x7FFFFFFF)
    raw_items = value.get("items")
    if not isinstance(raw_items, list):
        raise NativeV4Error("Recording catalog items must be an array")
    items = tuple(_decode_recording(item) for item in raw_items)
    if total_files < len(items):
        raise NativeV4Error("Recording catalog total_file is smaller than items")
    return NativeV4CatalogPage(
        response_id,
        status,
        error_message,
        total_pages,
        total_files,
        page,
        items,
    )


def _request_base(context: NativeV4RequestContext, request_type: str) -> dict[str, Any]:
    return {
        "v": context.version,
        "t": context.timestamp_ms,
        "cid": context.client_id,
        "sid": context.session_id,
        "from": context.source,
        "to": context.destination,
        "mode": context.mode,
        "type": request_type,
        "f": context.function,
        "reqid": context.request_id,
        "username": context.username,
        "passwd": context.password,
        "channel": context.camera_channel,
    }


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _token_integer(value: dict[str, Any], name: str, default: int) -> int:
    item = value.get(name, default)
    _integer(name, item, 0x7FFFFFFF)
    return item


def _token_signed_integer(value: dict[str, Any], name: str, default: int) -> int:
    item = value.get(name, default)
    _integer(name, item, 0xFFFFFFFF)
    return item if item <= 0x7FFFFFFF else item - 0x100000000


def _required_integer(value: dict[str, Any], name: str, maximum: int) -> int:
    item = value.get(name)
    _integer(name, item, maximum)
    return item


def _optional_integer(value: dict[str, Any], name: str, maximum: int) -> int:
    item = value.get(name, 0)
    _integer(name, item, maximum)
    return item


def _decode_recording(value: Any) -> NativeV4Recording:
    if not isinstance(value, dict):
        raise NativeV4Error("Recording catalog item must be an object")
    raw_event_types = value.get("event_types", [])
    if not isinstance(raw_event_types, list):
        raise NativeV4Error("event_types is invalid")
    return NativeV4Recording(
        start_time=_required_integer(value, "start_time", 0x7FFFFFFFFFFFFFFF),
        end_time=_required_integer(value, "end_time", 0x7FFFFFFFFFFFFFFF),
        event_type=_optional_integer(value, "event_type", 0x7FFFFFFF),
        event_types=tuple(raw_event_types),
        video_type=_optional_integer(value, "video_type", 0x7FFFFFFF),
        encrypt=_optional_integer(value, "encrypt", 0x7FFFFFFF),
        uuid=value.get("uuid", ""),
        encrypt_md5=value.get("encrypt_md5", ""),
    ).validate()


def _integer(name: str, value: int, maximum: int) -> None:
    if type(value) is not int or not 0 <= value <= maximum:
        raise NativeV4Error(f"{name} is outside the native V4 range")


def _text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value or "\0" in value:
        raise NativeV4Error(f"{name} is invalid")
