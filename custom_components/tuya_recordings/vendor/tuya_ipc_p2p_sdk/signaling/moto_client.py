"""The signaling MQTT channel for one session."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import ssl
import time
from collections import Counter
from contextlib import contextmanager
from typing import TYPE_CHECKING
from zlib import crc32

import aiomqtt

from ..const import LOGGER
from ..exceptions import TuyaIpcP2pConnectionError, TuyaIpcP2pError
from ..json_types import JsonObject, JsonValue, dp_integer, optional_int, optional_str
from .envelope import (
    SESSION_PROTOCOL,
    SIG_QUERY_PROTOCOL,
    SUPPORTED_PROTOCOL_VERSIONS,
    decode_payload,
    encode_payload,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ..models import MqttIdentity

_CONNECT_TIMEOUT_SECONDS = 15
_KEEPALIVE_SECONDS = 60
_SOURCE_LENGTH = 4
_DP_REPORT_PROTOCOL = 4
_DP_QUERY_PROTOCOL = 5
_DP_REPORT_TIMEOUT_SECONDS = 30

_BROKER_TLS = ssl.create_default_context()


def _connection_timestamp(value: JsonValue) -> int | None:
    """Read the integral Long timestamp used by the Android interceptor."""
    if isinstance(value, str):
        if not value.isascii() or not value.isdecimal() or len(value) > 19:
            return None
        value = int(value)
    return value if type(value) is int and 0 < value < (1 << 63) else None


class MotoClient:
    """
    Publishes and consumes the offer/answer/candidate exchange of one session.

    Messages use the device-advertised binary envelope version (``2.2`` or
    ``2.3``), on ``smart/mb/out/<devId>`` outbound and
    ``smart/mb/in/<devId>`` inbound.
    """

    def __init__(
        self,
        identity: MqttIdentity,
        uid: str,
        device_id: str,
        session_id: str,
        local_key: str,
        on_answer: Callable[[str], None],
        on_candidate: Callable[[str], None],
        on_disconnect: Callable[[int], None],
        protocol_version: str = "2.2",
        preconnect: bool = True,
        security_level: int = 3,
        moto_id: str = "",
        on_failure: Callable[[str], None] | None = None,
    ) -> None:
        """Bind the client to one device, one session and the callbacks that consume it."""
        if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise ValueError(
                f"Unsupported signaling protocol version: {protocol_version}"
            )
        self._identity = identity
        self._uid = uid
        self._device_id = device_id
        self._session_id = session_id
        self._key = local_key.encode()
        self._moto_id = moto_id
        self._offer_trace_id = ""
        self._on_answer = on_answer
        self._on_candidate = on_candidate
        self._on_disconnect = on_disconnect
        self._on_failure = on_failure
        self._protocol_version = protocol_version
        self._preconnect = bool(preconnect)
        if security_level not in {2, 3, 4}:
            raise ValueError("Unsupported signaling security level")
        self._security_level = security_level
        self._publish_topic = f"smart/mb/out/{device_id}"
        self._subscribe_topic = f"smart/mb/in/{device_id}"
        # Match Smart Life's per-device SandO state: a positive origin in
        # [1000, 1000999] and a counter initialized to 2, then incremented
        # before every publish. The camera therefore sees 3 on the first frame.
        origin = secrets.randbelow(1_000_000) + 1_000
        self._source = origin.to_bytes(_SOURCE_LENGTH, "big")
        self._sequence = 2
        self._client: aiomqtt.Client | None = None
        self._reader: asyncio.Task[None] | None = None
        self._dp_query: tuple[str, asyncio.Future[int]] | None = None
        self._dp_query_owner: asyncio.Task | None = None
        self._received = 0
        self._decode_failures = 0
        self._session_mismatches = 0
        self._inbound: Counter[str] = Counter()
        self._inbound_protocols: Counter[str] = Counter()
        self._published = 0
        self._outbound: Counter[str] = Counter()
        self._broker_status = "not_connected"
        self._subscription = "not_attempted"
        self._reader_failure = "none"
        self.device_online: bool | None = None
        self.low_power_connect: bool | None = None
        self.low_power_wake_confirmed = False
        self._low_power_change_time: int | None = None
        self._low_power_ready = False

    @property
    def diagnostics(self) -> str:
        inbound = ",".join(f"{kind}={count}" for kind, count in sorted(self._inbound.items()))
        outbound = ",".join(f"{kind}={count}" for kind, count in sorted(self._outbound.items()))
        protocols = ",".join(f"{kind}={count}" for kind, count in sorted(self._inbound_protocols.items()))
        return (f"published={self._published}, received={self._received}, decode_failures={self._decode_failures}, "
                f"session_mismatches={self._session_mismatches}, subscription={self._subscription}, "
                f"envelope_pv={self._protocol_version}, session_publish_qos=0, "
                f"broker={self._broker_status}, outbound={outbound or 'none'}, inbound={inbound or 'none'}, "
                f"protocols={protocols or 'none'}, "
                f"device_online={self.device_online}, low_power_connect={self.low_power_connect}, "
                f"low_power_wake_confirmed={self.low_power_wake_confirmed}, "
                f"reader_done={self._reader is not None and self._reader.done()}, failure={self._reader_failure}")

    async def async_connect(self) -> None:
        """Connect the broker, subscribe, and start consuming inbound payloads."""
        client = aiomqtt.Client(
            hostname=self._identity.host,
            port=self._identity.port,
            username=self._identity.username,
            password=self._identity.password,
            identifier=self._identity.client_id,
            keepalive=_KEEPALIVE_SECONDS,
            tls_context=_BROKER_TLS,
            clean_session=True,
        )
        self._broker_status = "connecting"
        try:
            async with asyncio.timeout(_CONNECT_TIMEOUT_SECONDS):
                await client.__aenter__()
        except (TimeoutError, aiomqtt.MqttError, OSError) as exception:
            self._broker_status = "connect_failed"
            raise TuyaIpcP2pConnectionError(
                f"Failed to connect the signaling broker: {type(exception).__name__}"
            ) from exception
        self._client = client
        self._broker_status = "connected"
        self._subscription = "pending"
        try:
            granted = await client.subscribe(
                self._subscribe_topic, qos=1, timeout=_CONNECT_TIMEOUT_SECONDS,
            )
        except aiomqtt.MqttError as error:
            self._subscription = "failed"
            raise TuyaIpcP2pConnectionError(
                "Failed to subscribe to native MQTT camera replies"
            ) from error
        codes = tuple(getattr(item, "value", item) for item in granted)
        if codes not in {(0,), (1,)}:
            self._subscription = "rejected"
            raise TuyaIpcP2pConnectionError(
                "Broker rejected the native MQTT camera reply subscription"
            )
        self._subscription = f"granted_qos_{codes[0]}"
        self._reader = asyncio.create_task(self._async_read())

    async def _async_read(self) -> None:
        """Consume inbound payloads until the connection ends."""
        client = self._client
        if client is None:
            return
        try:
            async for message in client.messages:
                if str(message.topic) != self._subscribe_topic:
                    self._received += 1
                    self._inbound["topic_mismatch"] += 1
                    continue
                payload = message.payload
                if isinstance(payload, bytes):
                    self._consume(payload)
                else:
                    self._received += 1
                    self._inbound["non_binary"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as exception:
            self._reader_failure = type(exception).__name__
            LOGGER.debug("Signaling reader stopped: %s", self._reader_failure)
        else:
            self._reader_failure = "stream_ended"
        if self._dp_query is not None:
            _, reply = self._dp_query
            if not reply.done():
                reply.set_exception(TuyaIpcP2pConnectionError("Device DP report channel stopped"))
        if self._on_failure is not None:
            self._on_failure(self._reader_failure)

    def _consume(self, payload: bytes) -> None:
        """Decode one payload and dispatch it to the session."""
        self._received += 1
        wire_version = payload[:3]
        # Both APKs choose the receive decoder from the frame prefix, not
        # from the inventory PV used for publishing. Authentication is still
        # performed with this device's local key by decode_payload.
        if wire_version not in {b"1.1", b"2.0", b"2.1", b"2.2", b"2.3"}:
            self._inbound["version_mismatch"] += 1
            return
        try:
            protocol, data = decode_payload(self._key, payload)
        except TuyaIpcP2pError as exception:
            self._decode_failures += 1
            LOGGER.debug("Discarded an undecodable signaling payload: %s", type(exception).__name__)
            return
        bucket = str(protocol)
        if bucket not in self._inbound_protocols and len(self._inbound_protocols) >= 16:
            bucket = "other"
        self._inbound_protocols[bucket] += 1
        if protocol == 1:
            self._consume_connection_report(data)
            return
        if protocol == _DP_REPORT_PROTOCOL:
            self._consume_dp_report(data)
            return
        if protocol != SESSION_PROTOCOL:
            self._inbound["signal_query" if protocol == SIG_QUERY_PROTOCOL else "other_protocol"] += 1
            return
        header = data.get("header")
        message = data.get("msg")
        if not isinstance(header, dict) or not isinstance(message, dict):
            self._inbound["invalid_shape"] += 1
            return
        session_id = optional_str(header, "sessionid")
        if (session_id or wire_version != self._protocol_version.encode("ascii")) and session_id != self._session_id:
            self._session_mismatches += 1
            return
        self._dispatch(optional_str(header, "type"), message)

    def _consume_connection_report(self, data: JsonObject) -> None:
        """Match both APKs' protocol-1 low-power interceptor, not a DP report."""
        if data.get("devId") != self._device_id or data.get("cid") not in (None, ""):
            self._inbound["connection_device_mismatch"] += 1
            return
        online = data.get("online")
        if type(online) is not bool:
            self._inbound["connection_invalid_online"] += 1
            return
        self.device_online = online
        self._inbound["device_online" if online else "device_offline"] += 1
        if "lowPowerConnect" in data:
            low_power = data["lowPowerConnect"]
            if type(low_power) is not bool:
                self._inbound["connection_invalid_low_power"] += 1
                return
            self.low_power_connect = low_power
            self._low_power_ready = not low_power
            self.low_power_wake_confirmed = False
            self._inbound["low_power_connected" if low_power else "low_power_exited"] += 1
            stamp = _connection_timestamp(data.get("lastOnlineChangeTime", data.get("lastConnectChangeTime")))
            if stamp is None:
                self._inbound["low_power_timestamp_missing_or_invalid"] += 1
                return
            if not low_power:
                self._low_power_change_time = stamp
                self.low_power_wake_confirmed = online
        else:
            stamp = _connection_timestamp(data.get("lastOnlineChangeTime"))
            if not online:
                self.low_power_wake_confirmed = False
            elif (self._low_power_ready and self._low_power_change_time is not None
                  and stamp is not None and stamp >= self._low_power_change_time):
                self.low_power_wake_confirmed = True
        if self.low_power_wake_confirmed:
            self._inbound["low_power_wake_confirmed"] += 1

    def _consume_dp_report(self, data: JsonObject) -> None:
        if self._dp_query is None:
            self._inbound["dp_report_no_query"] += 1
            return
        dp_id, reply = self._dp_query
        if reply.done():
            self._inbound["dp_report_late"] += 1
            return
        if data.get("devId") not in (None, "", self._device_id):
            self._inbound["dp_report_other_device"] += 1
            return
        if data.get("cid") not in (None, ""):
            self._inbound["dp_report_child"] += 1
            return
        dps = data.get("dps")
        value = dp_integer(dps.get(dp_id)) if isinstance(dps, dict) else None
        if value is not None:
            self._inbound["dp_report_accepted"] += 1
            reply.set_result(value)
        else:
            self._inbound["dp_report_missing_or_non_integer"] += 1

    async def async_query_direct_dp(self, dp_id: str) -> int:
        """Query one schema DP and wait for a report, not the publish acknowledgement."""
        if self._protocol_version == "1.1":
            raise TuyaIpcP2pConnectionError("PV 1.1 does not serialize a null DP query")
        return await self.async_wait_for_dp_report(
            dp_id, lambda: self._async_publish(_DP_QUERY_PROTOCOL, {"dps": {dp_id: None}}),
        )

    @contextmanager
    def dp_report_listener(self, dp_id: str):
        """Register one device-wide query listener, independent of send route."""
        if self._dp_query is not None:
            if self._dp_query[0] == dp_id and self._dp_query_owner is asyncio.current_task():
                yield self._dp_query[1]
                return
            raise TuyaIpcP2pConnectionError("A device DP query is already pending")
        if self._reader_failure != "none":
            raise TuyaIpcP2pConnectionError("Device DP report channel stopped")
        reply = asyncio.get_running_loop().create_future()
        self._dp_query = (dp_id, reply)
        self._dp_query_owner = asyncio.current_task()
        try:
            yield reply
        finally:
            self._dp_query = None
            self._dp_query_owner = None
            if not reply.cancel() and not reply.cancelled():
                reply.exception()

    async def async_wait_for_dp_report(
        self, dp_id: str, send: Callable[[], Awaitable[None]],
    ) -> int:
        """Register the device report listener before dispatching the request."""
        with self.dp_report_listener(dp_id) as reply:
            async with asyncio.timeout(_DP_REPORT_TIMEOUT_SECONDS):
                await send()
                return await reply

    def _dispatch(self, message_type: str | None, message: JsonObject) -> None:
        """Hand one decoded message to the callback that owns it."""
        if message_type == "answer":
            sdp = optional_str(message, "sdp")
            self._inbound["answer" if sdp else "answer_without_sdp"] += 1
            if sdp:
                self._on_answer(sdp)
        elif message_type == "candidate":
            self._inbound["candidate"] += 1
            candidate = optional_str(message, "candidate")
            if candidate:
                self._on_candidate(candidate)
        elif message_type == "disconnect":
            self._inbound["disconnect"] += 1
            self._on_disconnect(optional_int(message, "close_reason") or 0)
        else:
            self._inbound["other_session_message"] += 1

    async def async_send_sig_query(self) -> None:
        """Send the signal query that precedes the offer."""
        await self._async_publish(SIG_QUERY_PROTOCOL, {"reqType": "sigQry"})

    async def async_send_wake(self) -> None:
        """Send one APK low-power wake to the verified direct target."""
        if self._client is None or self._subscription not in {"granted_qos_0", "granted_qos_1"}:
            raise TuyaIpcP2pConnectionError("MQTT is not ready for a camera wake")
        try:
            await self._client.publish(
                f"m/w/{self._device_id}", crc32(self._key).to_bytes(4, "big"),
                qos=0, retain=False, timeout=_CONNECT_TIMEOUT_SECONDS,
            )
        except aiomqtt.MqttError as error:
            raise TuyaIpcP2pConnectionError("Native camera wake send failed") from error

    def configure_offer_metadata(
        self, *, moto_id: str, security_level: int, preconnect: bool
    ) -> None:
        """Apply an HTTP token result without dropping the subscribed reader."""
        if security_level not in {2, 3, 4}:
            raise ValueError("Unsupported signaling security level")
        self._moto_id = moto_id
        self._security_level = security_level
        self._preconnect = preconnect

    async def async_send_offer(
        self,
        sdp: str,
        ice_servers: JsonValue,
        trace_id: str,
        tcp_token: JsonValue,
        log_config: JsonValue,
    ) -> None:
        """Publish the offer that opens the session."""
        self._offer_trace_id = trace_id
        header = self._header("offer", trace_id)
        header.update(
            {
                "is_pre": 0,
                "p2p_skill": 1635,
                "security_level": self._security_level,
            }
        )
        msg: JsonObject = {"sdp": sdp, "preconnect": self._preconnect}
        for key, value in (
            ("token", ice_servers), ("tcp_token", tcp_token), ("log", log_config),
        ):
            if value:
                msg[key] = value
        await self._async_publish(SESSION_PROTOCOL, {"header": header, "msg": msg})

    async def async_send_candidate(self, candidate: str) -> None:
        """Trickle one local ICE candidate."""
        await self._async_publish(
            SESSION_PROTOCOL,
            {"header": self._header("candidate", self._offer_trace_id), "msg": {"candidate": candidate}},
        )

    async def async_send_disconnect(self, close_reason: int = 4) -> None:
        """
        Tear the session down.

        Without this the device holds the session open and refuses the next
        offer until its own timer fires.
        """
        if self._subscription in {"pending", "failed", "rejected"}:
            return
        await self._async_publish(
            SESSION_PROTOCOL,
            {
                "header": self._header("disconnect"),
                "msg": {"close_reason": close_reason, "close_reason_local": 0},
            },
        )

    def _header(self, message_type: str, trace_id: str = "") -> JsonObject:
        """Build the header shared by every published signaling frame."""
        header: JsonObject = {
            "type": message_type,
            "from": self._uid,
            "to": self._device_id,
            "sessionid": self._session_id,
            "moto_id": self._moto_id,
            "path": "mqtt",
        }
        if trace_id:
            header["trace_id"] = trace_id
        return header

    async def _async_publish(self, protocol: int, data: JsonValue) -> None:
        """Encode one payload and publish it."""
        client = self._client
        if client is None:
            raise TuyaIpcP2pConnectionError(
                "Failed to publish: the signaling broker is not connected"
            )
        self._sequence += 1
        payload = encode_payload(
            self._key,
            self._sequence,
            self._source,
            data,
            int(time.time()),
            protocol,
            self._protocol_version,
            device_id=self._device_id,
        )
        try:
            # Both consumer APKs use QoS 0 for native protocol 302.
            await client.publish(
                self._publish_topic, payload, qos=0 if protocol == SESSION_PROTOCOL else 1,
            )
            self._published += 1
            header = data.get("header") if isinstance(data, dict) else None
            kind = optional_str(header, "type") if isinstance(header, dict) else None
            self._outbound[kind if kind in {"offer", "candidate", "disconnect"} else "other"] += 1
        except aiomqtt.MqttError as exception:
            raise TuyaIpcP2pConnectionError(f"Failed to publish: {exception}") from exception

    async def async_close(self) -> None:
        """Stop consuming and disconnect from the broker."""
        reader = self._reader
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
            self._reader = None
        client = self._client
        if client is not None:
            await client.__aexit__(None, None, None)
            self._client = None
