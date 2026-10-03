"""Bounded Smart Life app MQTT transport used by native camera signaling."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import ssl
import threading
from typing import Any
from urllib.parse import urlsplit

from .native_gateway import CH_KEY, CLIENT_ID, COMPOSITE_KEY, PACKAGE_NAME

MQTT_PORT = 8883
MQTT_KEEPALIVE = 60
MQTT_CONNECT_TIMEOUT = 10.0
MQTT_SUBSCRIBE_TIMEOUT = 10.0
SUPPORTED_REGIONS = frozenset({"us", "eu", "cn", "in", "we"})
_ALLOWED_BROKER_SUFFIXES = (".lifeaiot.com", ".tuyaus.com")


class NativeAppMqttError(RuntimeError):
    """The Smart Life app MQTT transaction could not be established safely."""


@dataclass(frozen=True, slots=True)
class NativeAppMqttConfig:
    host: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    client_id: str
    port: int = MQTT_PORT

    @classmethod
    def from_saved_session(
        cls,
        saved: Mapping[str, Any],
        region: str = "us",
    ) -> "NativeAppMqttConfig":
        """Reproduce the consumer-app MQTT identity built by the Android SDK."""
        sid = _required(saved, "sid")
        ecode = _required(saved, "ecode")
        uid = _required(saved, "uid")
        device_id = _required(saved, "device_fingerprint")
        partner_identity = _required(saved, "partner_identity")
        host, port = _saved_broker(_required(saved, "mobile_mqtts_url"))
        if region not in SUPPORTED_REGIONS:
            raise NativeAppMqttError("Smart Life MQTT region is invalid")

        app_key_hash = _md5(CLIENT_ID)
        user_tail = _md5(app_key_hash + ecode)[-16:]
        username = (
            f"{partner_identity}_v1_{CLIENT_ID}_{CH_KEY}_mb_{sid}{user_tail}"
        )
        password = _md5(_md5(COMPOSITE_KEY) + ecode)[8:24]
        uid_hash = _md5(uid + "sdkfasodifca")
        client_id = f"{PACKAGE_NAME}_mb_{device_id}_{uid_hash}_DEFAULT"
        return cls(
            host=host,
            username=username,
            password=password,
            client_id=client_id,
            port=port,
        )


class NativeAppMqttClient:
    """Own one TLS MQTT connection with no retry or automatic reconnect."""

    def __init__(
        self,
        config: NativeAppMqttConfig,
        *,
        client_factory: Callable[[str], Any] | None = None,
        connect_timeout: float = MQTT_CONNECT_TIMEOUT,
        subscribe_timeout: float = MQTT_SUBSCRIBE_TIMEOUT,
    ) -> None:
        if not 0 < connect_timeout <= 30 or not 0 < subscribe_timeout <= 30:
            raise NativeAppMqttError("Smart Life MQTT timeout is invalid")
        self._config = config
        self._client_factory = client_factory or _paho_client
        self._connect_timeout = connect_timeout
        self._subscribe_timeout = subscribe_timeout
        self._connected = threading.Event()
        self._subscribed = threading.Event()
        self._failure: str | None = None
        self._client: Any | None = None
        self._started = False

    def start(self, topic: str, callback: Callable[..., None]) -> Any:
        if self._started:
            raise NativeAppMqttError("Smart Life MQTT client is already started")
        if not isinstance(topic, str) or not topic:
            raise NativeAppMqttError("Smart Life MQTT subscription topic is required")
        client = self._client_factory(self._config.client_id)
        self._client = client
        self._started = True
        try:
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_subscribe = self._on_subscribe
            client.username_pw_set(self._config.username, self._config.password)
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
            client.message_callback_add(topic, callback)
            result = client.connect(
                self._config.host,
                self._config.port,
                keepalive=MQTT_KEEPALIVE,
            )
            if _result_code(result) != 0:
                raise NativeAppMqttError("Smart Life MQTT connection was rejected")
            client.loop_start()
            self._wait(self._connected, self._connect_timeout, "connection")
            result = client.subscribe(topic, qos=1)
            if _result_code(result) != 0:
                raise NativeAppMqttError("Smart Life MQTT subscription was rejected")
            self._wait(self._subscribed, self._subscribe_timeout, "subscription")
            return client
        except Exception:
            self.close(topic)
            raise

    def close(self, topic: str | None = None) -> None:
        client, self._client = self._client, None
        self._started = False
        if client is None:
            return
        if topic:
            try:
                client.message_callback_remove(topic)
            except Exception:
                pass
            try:
                client.unsubscribe(topic)
            except Exception:
                pass
        try:
            client.disconnect()
        except Exception:
            pass
        try:
            client.loop_stop()
        except Exception:
            pass

    def _on_connect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
        del client, userdata, flags, properties
        code = _reason_code(reason_code)
        if code == 0:
            self._connected.set()
        else:
            self._failure = f"broker returned {code}"
            self._connected.set()

    def _on_disconnect(self, client: Any, userdata: Any, *args: Any) -> None:
        del client, userdata
        code = _disconnect_code(args)
        if code != 0 and self._failure is None:
            self._failure = f"broker disconnected with {code}"
            self._connected.set()
            self._subscribed.set()

    def _on_subscribe(self, client: Any, userdata: Any, mid: Any, *args: Any) -> None:
        del client, userdata, mid
        if args and _subscription_failed(args[0]):
            self._failure = "broker rejected subscription"
        self._subscribed.set()

    def _wait(self, event: threading.Event, timeout: float, operation: str) -> None:
        if not event.wait(timeout):
            raise NativeAppMqttError(f"Smart Life MQTT {operation} timed out")
        if self._failure is not None:
            raise NativeAppMqttError(f"Smart Life MQTT {operation} failed: {self._failure}")


def _paho_client(client_id: str) -> Any:
    try:
        import paho.mqtt.client as mqtt
    except ImportError as err:
        raise NativeAppMqttError("paho-mqtt is not installed") from err
    return mqtt.Client(
        client_id=client_id,
        clean_session=True,
        protocol=mqtt.MQTTv311,
        transport="tcp",
        reconnect_on_failure=False,
    )


def _required(source: Mapping[str, Any], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 8192:
        raise NativeAppMqttError(f"Smart Life MQTT session is missing {key}")
    return value.strip()


def _md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8"), usedforsecurity=False).hexdigest()


def _saved_broker(value: str) -> tuple[str, int]:
    """Parse the MQTT endpoint returned by the authenticated Smart Life app."""
    parsed = urlsplit(value if "://" in value else f"//{value}")
    if parsed.scheme and parsed.scheme not in {"ssl", "mqtts"}:
        raise NativeAppMqttError("Smart Life MQTT broker scheme is invalid")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or not host.endswith(_ALLOWED_BROKER_SUFFIXES):
        raise NativeAppMqttError("Smart Life MQTT broker host is invalid")
    try:
        port = parsed.port or MQTT_PORT
    except ValueError as err:
        raise NativeAppMqttError("Smart Life MQTT broker port is invalid") from err
    if not 1 <= port <= 65535:
        raise NativeAppMqttError("Smart Life MQTT broker port is invalid")
    return host, port


def _result_code(result: Any) -> int:
    value = result if isinstance(result, int) else getattr(result, "rc", None)
    if value is None and isinstance(result, tuple) and result:
        value = result[0]
    if not isinstance(value, int):
        raise NativeAppMqttError("Smart Life MQTT operation returned no result code")
    return value


def _reason_code(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _disconnect_code(args: tuple[Any, ...]) -> int:
    for value in reversed(args):
        code = _reason_code(value)
        if code >= 0:
            return code
    return 0


def _subscription_failed(value: Any) -> bool:
    values = value if isinstance(value, list | tuple) else (value,)
    return any(_reason_code(item) >= 128 for item in values)
