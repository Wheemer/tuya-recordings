"""APK-faithful Tuya camera session contract.

This module describes the Smart Life native camera SDK boundary used by the
in-process authenticated direct-ICE implementation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any

APK_RTC_CONFIG_API = "m.ipc.v4.rtc.config.get"
APK_RTC_SESSION_INIT_API = "thing.m.rtc.session.init"
APK_API_VERSION = "1.0"
APK_CAMERA_BIZ_DM = "ipc"
APK_DEFAULT_USERNAME = "admin"
APK_PLAYBACK_MODE_DEFAULT = 0
APK_VIDEO_OUTPUT_FORMAT_RAWDATA = 0
APK_VIDEO_OUTPUT_FORMAT_YUV = 1
APK_AUDIO_OUTPUT_FORMAT_RAWDATA = 0
APK_AUDIO_OUTPUT_FORMAT_PCM = 1


class NativePlaybackMethod(str, Enum):
    """Native SDK call families seen in the Smart Life APK."""

    RECORD_FRAGMENTS_BY_DAY = "getRecordFragmentsByDay"
    RECORD_FRAGMENTS_BY_DAY_V2 = "getRecordFragmentsByDayV2"
    RECORD_FRAGMENTS_BY_DAY_V3 = "getRecordFragmentsByDayV3"
    START_PLAYBACK = "startPlayBack"
    START_PLAYBACK_V2 = "startPlayBackV2"
    START_PLAYBACK_WITH_PLAY_TIME = "startPlayBackWithPlayTime"
    DOWNLOAD_PLAYBACK_IMAGE_V2 = "downloadPlaybackImageV2"
    SET_ENCRYPTION_INFO = "setEncryptionInfo"
    SET_MUTE = "setMute"


class NativeMediaFormat(str, Enum):
    """APK media callback format families."""

    DECODED_YUV_PCM = "decoded-yuv-pcm"
    RAW_PACKETS = "raw-packets"


class NativeSessionError(ValueError):
    """Camera config is incomplete or incompatible with APK-native playback."""


@dataclass(frozen=True, slots=True)
class NativePlaybackCallSpec:
    """APK-native playback call selected for one recording request.

    The C++ signatures were verified from `libThingCameraSDK.so`; this object
    keeps implementations tied to the Smart Life native callback model
    instead of drifting back to browser/WebRTC/download experiments.
    """

    method: NativePlaybackMethod
    args: tuple[Any, ...]
    video_output_format: int
    audio_output_format: int
    cpp_signatures: tuple[str, ...]
    jni_args: tuple[Any, ...]

    @property
    def uses_raw_packets(self) -> bool:
        return (
            self.video_output_format == APK_VIDEO_OUTPUT_FORMAT_RAWDATA
            and self.audio_output_format == APK_AUDIO_OUTPUT_FORMAT_RAWDATA
        )


@dataclass(frozen=True, slots=True)
class NativeCameraSessionConfig:
    """Connection values passed into the APK native camera SDK.

    `camera_password` is the password returned by the app-session camera config
    API. `derived_password` is the value actually supplied to native connect.
    Secrets are hidden from repr because these objects can show up in HA logs.
    """

    dev_id: str
    local_key: str = field(repr=False)
    camera_password: str = field(repr=False)
    token: str = field(repr=False)
    skill: str
    p2p_type: int
    trace_id: str
    p2p_policy: int = 0
    preconnect: int = 0
    local_id: str = field(default="", repr=False)
    p2p_id: str = ""
    config_json: str = field(default="", repr=False)
    ext_config: dict[str, Any] = field(default_factory=dict, repr=False)
    app_sid: str = field(default="", repr=False)
    app_ecode: str = field(default="", repr=False)
    app_uid: str = field(default="", repr=False)
    app_region: str = "us"
    app_profile_id: str = "smart_life"
    app_device_fingerprint: str = field(default="", repr=False)
    mqtt_protocol_version: str = "2.2"

    @property
    def username(self) -> str:
        return APK_DEFAULT_USERNAME

    @property
    def derived_password(self) -> str:
        return derive_native_password(self.camera_password, self.local_key)

    @property
    def connect_ext_config_json(self) -> str:
        return json.dumps(self.ext_config, separators=(",", ":"), sort_keys=True)

    @property
    def p2p_config(self) -> dict[str, Any]:
        return _parse_json_object("token", self.token)

    @property
    def p2p_session_id(self) -> str:
        session = self.p2p_config.get("session")
        if not isinstance(session, dict):
            return ""
        value = session.get("sessionId") or session.get("session_id")
        return value if isinstance(value, str) else ""

    @property
    def p2p_moto_id(self) -> str:
        for value in (
            self.p2p_config.get("motoId"),
            self.p2p_config.get("moto_id"),
            self.ext_config.get("motoId"),
            self.ext_config.get("moto_id"),
        ):
            if isinstance(value, str) and value:
                return value
        return ""

    @property
    def p2p_auth(self) -> str:
        value = self.p2p_config.get("auth") or self.ext_config.get("auth")
        return value if isinstance(value, str) else ""

    @property
    def p2p_security_level(self) -> int:
        value = self.p2p_config.get("securityLevel")
        return value if type(value) is int and value in {2, 3, 4} else 3

    @property
    def p2p_ice_servers(self) -> list[dict[str, Any]]:
        value = self.p2p_config.get("ices") or self.p2p_config.get("iceServers")
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    @property
    def uses_connection_params(self) -> bool:
        return self.p2p_type == 8

    @property
    def has_app_session(self) -> bool:
        return bool(self.app_sid and self.app_ecode and self.app_uid)

    @property
    def local_storage(self) -> int:
        """Return Smart Life's SD playback capability bitmask."""
        value = _parse_json_object("skill", self.skill).get("localStorage", 0)
        return value if type(value) is int and value >= 0 else 0

    @property
    def event_catalog_supported(self) -> bool:
        """Whether Smart Life queries and displays the separate event timeline."""
        return bool(self.local_storage & (33554432 | 268435456))

    @property
    def playback_mode_supported(self) -> bool:
        """Whether Smart Life uses fragment-list timeline playback."""
        return bool(self.local_storage & 128)

    @property
    def event_catalog_v2(self) -> bool:
        """Whether Smart Life selects the V2 event catalog command."""
        return bool(self.local_storage & 268435456)

    @property
    def playback_catalog_version(self) -> int:
        """Return the ordinary day-catalog version selected by Smart Life."""
        return 2 if self.local_storage & 134217728 else 0

    @property
    def signaling_local_id(self) -> str:
        """Return the identity supplied to the APK P2P initializer."""
        value = self.local_id or self.app_uid
        if not value:
            raise NativeSessionError("Native app signaling identity is unavailable")
        return value

    def validate(self) -> "NativeCameraSessionConfig":
        _required("dev_id", self.dev_id)
        _required("local_key", self.local_key)
        _required("camera_password", self.camera_password)
        _required("token", self.token)
        _required("skill", self.skill)
        _required("trace_id", self.trace_id)
        if self.p2p_type not in {4, 8}:
            raise NativeSessionError(f"Unsupported APK-native P2P type: {self.p2p_type}")
        if self.p2p_policy not in {0, 1}:
            raise NativeSessionError("p2p_policy must be 0 or 1")
        if self.preconnect not in {0, 1}:
            raise NativeSessionError("preconnect must be 0 or 1")
        _parse_json_object("skill", self.skill)
        if self.config_json:
            _parse_json_object("config_json", self.config_json)
        if any((self.app_sid, self.app_ecode, self.app_uid)) and not self.has_app_session:
            raise NativeSessionError("Native app session must include sid, ecode, and uid")
        try:
            if float(self.mqtt_protocol_version) < 1.0:
                raise ValueError
        except (TypeError, ValueError) as err:
            raise NativeSessionError("Native MQTT protocol version is invalid") from err
        return self


@dataclass(frozen=True, slots=True)
class NativeRecordingQuery:
    """APK catalog request for one SD-card day."""

    day: date
    encrypted: bool = False
    version: int | None = None

    def validate(self) -> "NativeRecordingQuery":
        if type(self.day) is not date:
            raise NativeSessionError("Recording query day must be a date")
        if type(self.encrypted) is not bool:
            raise NativeSessionError("Recording query encrypted flag must be boolean")
        if self.version is not None and type(self.version) is not int:
            raise NativeSessionError("Recording query version must be an integer")
        return self

    @property
    def method(self) -> NativePlaybackMethod:
        self.validate()
        return record_query_method(encrypted=self.encrypted, version=self.version)

    @property
    def day_text(self) -> str:
        self.validate()
        return f"{self.day:%Y%m%d}"

    @property
    def args(self) -> tuple[str, ...]:
        return (self.day_text,)


@dataclass(frozen=True, slots=True)
class NativePlaybackRequest:
    start: int
    end: int
    play_time: int | None = None
    fragments_json: str = ""
    encrypted: bool = False
    play_mode_supported: bool = False
    media_format: NativeMediaFormat = NativeMediaFormat.DECODED_YUV_PCM
    catalog_day: date | None = None
    encryption_uuid: str = ""

    def validate(self) -> "NativePlaybackRequest":
        for name, value in (("start", self.start), ("end", self.end)):
            _integer(name, value)
        if self.end <= self.start:
            raise NativeSessionError("Playback end must be after start")
        play_time = self.start if self.play_time is None else self.play_time
        _integer("play_time", play_time)
        if not self.start <= play_time < self.end:
            raise NativeSessionError("Playback position must be inside clip bounds")
        if self.fragments_json:
            fragments = _parse_json_object("fragments_json", self.fragments_json).get("fragments")
            if not isinstance(fragments, list) or not fragments:
                raise NativeSessionError("fragments_json must contain a non-empty fragments list")
            _first_fragment_bounds(self.fragments_json)
        if not isinstance(self.media_format, NativeMediaFormat):
            raise NativeSessionError("media_format must be a NativeMediaFormat")
        if self.catalog_day is not None and type(self.catalog_day) is not date:
            raise NativeSessionError("catalog_day must be a date")
        if not isinstance(self.encryption_uuid, str):
            raise NativeSessionError("encryption_uuid must be a string")
        return self

    @property
    def position(self) -> int:
        return self.start if self.play_time is None else self.play_time

    @property
    def method(self) -> NativePlaybackMethod:
        self.validate()
        if self.play_mode_supported:
            if not self.fragments_json:
                raise NativeSessionError("APK play-mode playback requires fragments_json")
            if self.encrypted:
                return NativePlaybackMethod.START_PLAYBACK_V2
            return NativePlaybackMethod.START_PLAYBACK_WITH_PLAY_TIME
        if self.encrypted:
            return NativePlaybackMethod.START_PLAYBACK_V2
        return NativePlaybackMethod.START_PLAYBACK

    @property
    def args(self) -> tuple[Any, ...]:
        method = self.method
        if method == NativePlaybackMethod.START_PLAYBACK_WITH_PLAY_TIME:
            return (self.position, APK_PLAYBACK_MODE_DEFAULT, self.fragments_json)
        if method == NativePlaybackMethod.START_PLAYBACK_V2 and self.play_mode_supported and self.fragments_json:
            start, end = _first_fragment_bounds(self.fragments_json)
            return (start, end, self.position)
        return (self.start, self.end, self.position)

    @property
    def output_format_args(self) -> tuple[int, int]:
        self.validate()
        if self.media_format == NativeMediaFormat.RAW_PACKETS:
            return APK_VIDEO_OUTPUT_FORMAT_RAWDATA, APK_AUDIO_OUTPUT_FORMAT_RAWDATA
        return APK_VIDEO_OUTPUT_FORMAT_YUV, APK_AUDIO_OUTPUT_FORMAT_PCM

    @property
    def call_spec(self) -> NativePlaybackCallSpec:
        """Return the native playback call contract this request requires."""
        video_format, audio_format = self.output_format_args
        method = self.method
        if method == NativePlaybackMethod.START_PLAYBACK_WITH_PLAY_TIME and self.media_format == NativeMediaFormat.RAW_PACKETS:
            raise NativeSessionError(
                "APK play-mode playback does not expose raw packet output in the Java native API"
            )
        return NativePlaybackCallSpec(
            method=method,
            args=self.args,
            video_output_format=video_format,
            audio_output_format=audio_format,
            cpp_signatures=native_playback_cpp_signatures(method),
            jni_args=native_playback_jni_args(
                method,
                self.args,
                video_output_format=video_format,
                audio_output_format=audio_format,
                encrypted=self.encrypted,
            ),
        )


def derive_native_password(camera_password: str, local_key: str) -> str:
    """Match `MD5Utils.b(password + "||" + localKey)` from Smart Life."""
    _required("camera_password", camera_password)
    _required("local_key", local_key)
    return hashlib.md5(f"{camera_password}||{local_key}".encode("utf-8")).hexdigest()


def native_session_api_request(
    dev_id: str, *, trace_id: str
) -> tuple[str, str, dict[str, Any], dict[str, str]]:
    """Return the APK camera-config request, including URL parameters."""
    _required("dev_id", dev_id)
    _required("trace_id", trace_id)
    return (
        APK_RTC_CONFIG_API,
        APK_API_VERSION,
        {"devId": dev_id},
        {"bizDM": APK_CAMERA_BIZ_DM, "ctId": trace_id},
    )


def native_session_init_api_request(
    dev_id: str, *, trace_id: str
) -> tuple[str, str, dict[str, Any], dict[str, str]]:
    """Return the APK's deprecated camera session-init request."""
    _required("dev_id", dev_id)
    _required("trace_id", trace_id)
    return (
        APK_RTC_SESSION_INIT_API,
        APK_API_VERSION,
        {"devId": dev_id},
        {"bizDM": APK_CAMERA_BIZ_DM, "ctId": trace_id},
    )


def normalize_camera_info(
    dev_id: str,
    camera_info: dict[str, Any],
    *,
    local_key: str,
    trace_id: str,
    preconnect: int = 0,
    local_id: str = "",
    app_session: dict[str, Any] | None = None,
    app_region: str = "us",
    app_profile_id: str = "smart_life",
    app_device_fingerprint: str = "",
    mqtt_protocol_version: str = "2.2",
) -> NativeCameraSessionConfig:
    """Convert the app API `CameraInfoBean` shape into native connect inputs."""
    if not isinstance(camera_info, dict):
        raise NativeSessionError("camera_info must be an object")
    try:
        p2p_type = int(camera_info.get("p2pSpecifiedType") or camera_info.get("p2pType") or 0)
        p2p_policy = int(camera_info.get("p2pPolicy") or 0)
    except (TypeError, ValueError) as err:
        raise NativeSessionError("Unsupported APK-native P2P configuration") from err
    token = _p2p_config_string(camera_info)
    p2p_config = _parse_json_object("p2pConfig", token)
    token_preconnect = p2p_config.get("preconnect")
    if isinstance(token_preconnect, bool) or type(token_preconnect) is int:
        preconnect = int(bool(token_preconnect))
    skill = _string(camera_info.get("skill") or camera_info.get("skillV4"), "skill")
    password = _string(camera_info.get("password"), "camera password")
    p2p_id = str(camera_info.get("id") or camera_info.get("p2pId") or dev_id)
    normalized_camera_info = dict(camera_info)
    normalized_camera_info["p2pConfig"] = p2p_config
    config_json = json.dumps(
        normalized_camera_info, separators=(",", ":"), sort_keys=True
    )
    return NativeCameraSessionConfig(
        dev_id=dev_id,
        local_key=local_key,
        camera_password=password,
        token=token,
        skill=skill,
        p2p_type=p2p_type,
        p2p_policy=p2p_policy,
        trace_id=trace_id,
        preconnect=preconnect,
        local_id=local_id,
        p2p_id=p2p_id,
        config_json=config_json,
        ext_config={"trace_id": trace_id},
        app_sid=_optional_session_text(app_session, "sid"),
        app_ecode=_optional_session_text(app_session, "ecode"),
        app_uid=_optional_session_text(app_session, "uid"),
        app_region=app_region or "us",
        app_profile_id=app_profile_id or "smart_life",
        app_device_fingerprint=app_device_fingerprint,
        mqtt_protocol_version=mqtt_protocol_version,
    ).validate()


def _optional_session_text(app_session: dict[str, Any] | None, key: str) -> str:
    if not isinstance(app_session, dict):
        return ""
    value = app_session.get(key)
    return value.strip() if isinstance(value, str) else ""


def native_encryption_info_json(dev_id: str, secrets: list[dict[str, Any]]) -> str:
    """Build the exact JSON shape passed to `ThingCameraNative.setEncryptionInfo`.

    Smart Life receives cloud secret rows with `uuid` and `secretKey`, then
    passes `{devId, encryptInfos:[{uuid, encrypt: secretKey}]}` into the native
    SDK before encrypted playback/image/download work.
    """
    _required("dev_id", dev_id)
    if not isinstance(secrets, list):
        raise NativeSessionError("encryption secrets must be a list")
    encrypt_infos: list[dict[str, str]] = []
    for item in secrets:
        if not isinstance(item, dict):
            raise NativeSessionError("encryption secret rows must be objects")
        uuid = _string(item.get("uuid"), "encryption uuid")
        secret = _string(item.get("secretKey") or item.get("encrypt"), "encryption secret")
        encrypt_infos.append({"uuid": uuid, "encrypt": secret})
    return json.dumps(
        {"devId": dev_id, "encryptInfos": encrypt_infos},
        separators=(",", ":"),
        sort_keys=True,
    )


def record_query_method(*, encrypted: bool, version: int | None = None) -> NativePlaybackMethod:
    """Select the APK record-fragment call for a day's SD-card catalog."""
    if version == 3:
        return NativePlaybackMethod.RECORD_FRAGMENTS_BY_DAY_V3
    if encrypted:
        return NativePlaybackMethod.RECORD_FRAGMENTS_BY_DAY_V2
    return NativePlaybackMethod.RECORD_FRAGMENTS_BY_DAY


def native_playback_cpp_signatures(method: NativePlaybackMethod) -> tuple[str, ...]:
    """Return verified native SDK signatures for APK playback stream methods."""
    if method == NativePlaybackMethod.START_PLAYBACK_WITH_PLAY_TIME:
        return (
            "ThingCameraSimple::StartPlayBack(int, ThingVideoOutputFormat, "
            "ThingAudioOutputFormat, int, int, char const*, ThingBaseCallBack*)",
            "ThingCameraStation::StartPlayBack(int, ThingVideoOutputFormat, "
            "ThingAudioOutputFormat, int, int, char const*, ThingBaseCallBack*)",
            "ThingCameraV4::StartPlayBack(int, ThingVideoOutputFormat, "
            "ThingAudioOutputFormat, int, int, char const*, ThingBaseCallBack*)",
        )
    if method in {
        NativePlaybackMethod.START_PLAYBACK,
        NativePlaybackMethod.START_PLAYBACK_V2,
    }:
        return (
            "ThingCameraSimple::StartPlayBack(int, ThingVideoOutputFormat, "
            "ThingAudioOutputFormat, int, int, int, int, ThingBaseCallBack*)",
            "ThingCameraStation::StartPlayBack(int, ThingVideoOutputFormat, "
            "ThingAudioOutputFormat, int, int, int, int, ThingBaseCallBack*)",
            "ThingCameraV4::StartPlayBack(int, ThingVideoOutputFormat, "
            "ThingAudioOutputFormat, int, int, int, int, ThingBaseCallBack*)",
        )
    raise NativeSessionError("Unsupported native playback method")


def native_playback_jni_args(
    method: NativePlaybackMethod,
    args: tuple[Any, ...],
    *,
    video_output_format: int,
    audio_output_format: int,
    encrypted: bool,
) -> tuple[Any, ...]:
    """Return the Java native method argument order after the camera handle.

    `ThingCameraNative.startPlayBack` takes:
    start, stop, playTime, encryptedFlag, videoFormat, audioFormat, callback.
    The callback object is represented by the in-process backend, so it is not part
    of this tuple.
    """
    if method in {
        NativePlaybackMethod.START_PLAYBACK,
        NativePlaybackMethod.START_PLAYBACK_V2,
    }:
        start, end, play_time = args
        encrypted_flag = 1 if encrypted or method == NativePlaybackMethod.START_PLAYBACK_V2 else 0
        return (
            start,
            end,
            play_time,
            encrypted_flag,
            video_output_format,
            audio_output_format,
        )
    if method == NativePlaybackMethod.START_PLAYBACK_WITH_PLAY_TIME:
        return args
    raise NativeSessionError("Unsupported native playback method")


def _p2p_config_string(camera_info: dict[str, Any]) -> str:
    raw = camera_info.get("p2pConfig") or camera_info.get("p2p_config")
    if isinstance(raw, str) and raw.strip():
        _parse_json_object("p2pConfig", raw)
        return raw
    if isinstance(raw, dict) and raw:
        return json.dumps(raw, separators=(",", ":"), sort_keys=True)
    token = camera_info.get("token")
    if isinstance(token, str) and token.strip():
        _parse_json_object("p2pConfig", token)
        return token
    raise NativeSessionError("camera_info is missing p2pConfig")


def _parse_json_object(name: str, value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as err:
        raise NativeSessionError(f"{name} must be valid JSON") from err
    if not isinstance(parsed, dict):
        raise NativeSessionError(f"{name} must be a JSON object")
    return parsed


def _first_fragment_bounds(fragments_json: str) -> tuple[int, int]:
    fragments = _parse_json_object("fragments_json", fragments_json).get("fragments")
    if not isinstance(fragments, list) or not fragments or not isinstance(fragments[0], dict):
        raise NativeSessionError("fragments_json must contain a non-empty fragments list")
    fragment = fragments[0]
    start = fragment.get("start")
    end = fragment.get("end")
    _integer("fragment start", start)
    _integer("fragment end", end)
    if end <= start:
        raise NativeSessionError("Fragment end must be after fragment start")
    return start, end


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NativeSessionError(f"Missing {name}")
    return value


def _required(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise NativeSessionError(f"{name} must be a non-empty string")


def _integer(name: str, value: int) -> None:
    if type(value) is not int or not 0 <= value <= 0x7FFFFFFF:
        raise NativeSessionError(f"{name} must be an integer between 0 and 2147483647")
