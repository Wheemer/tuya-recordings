"""The signaling MQTT channel for one session."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import ssl
import time
from collections import Counter
from typing import TYPE_CHECKING

import aiomqtt

from ..const import LOGGER
from ..exceptions import TuyaIpcP2pConnectionError, TuyaIpcP2pError
from ..json_types import JsonObject, JsonValue, optional_int, optional_str
from .envelope import SESSION_PROTOCOL, SIG_QUERY_PROTOCOL, decode_payload, encode_payload

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..models import MqttIdentity

_CONNECT_TIMEOUT_SECONDS = 15
_KEEPALIVE_SECONDS = 60
_SOURCE_LENGTH = 4

_BROKER_TLS = ssl.create_default_context()


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
        if protocol_version not in {"2.2", "2.3"}:
            raise ValueError(
                f"Unsupported signaling protocol version: {protocol_version}"
            )
        self._identity = identity
        self._uid = uid
        self._device_id = device_id
        self._session_id = session_id
        self._key = local_key.encode()
        self._moto_id = moto_id
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
        self._received = 0
        self._decode_failures = 0
        self._session_mismatches = 0
        self._inbound: Counter[str] = Counter()
        self._published = 0
        self._reader_failure = "none"

    @property
    def diagnostics(self) -> str:
        inbound = ",".join(f"{kind}={count}" for kind, count in sorted(self._inbound.items()))
        return (f"published={self._published}, received={self._received}, decode_failures={self._decode_failures}, "
                f"session_mismatches={self._session_mismatches}, "
                f"inbound={inbound or 'none'}, "
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
        try:
            async with asyncio.timeout(_CONNECT_TIMEOUT_SECONDS):
                await client.__aenter__()
        except (TimeoutError, aiomqtt.MqttError, OSError) as exception:
            raise TuyaIpcP2pConnectionError(
                f"Failed to connect the signaling broker: {exception}"
            ) from exception
        self._client = client
        await client.subscribe(self._subscribe_topic, qos=1)
        self._reader = asyncio.create_task(self._async_read())

    async def _async_read(self) -> None:
        """Consume inbound payloads until the connection ends."""
        client = self._client
        if client is None:
            return
        try:
            async for message in client.messages:
                payload = message.payload
                if isinstance(payload, bytes):
                    self._consume(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exception:
            self._reader_failure = type(exception).__name__
            LOGGER.debug("Signaling reader stopped: %s", self._reader_failure)
        else:
            self._reader_failure = "stream_ended"
        if self._on_failure is not None:
            self._on_failure(self._reader_failure)

    def _consume(self, payload: bytes) -> None:
        """Decode one payload and dispatch it to the session."""
        self._received += 1
        try:
            protocol, data = decode_payload(self._key, payload)
        except TuyaIpcP2pError as exception:
            self._decode_failures += 1
            LOGGER.debug("Discarded an undecodable signaling payload: %s", exception)
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
        if session_id and session_id != self._session_id:
            self._session_mismatches += 1
            return
        self._dispatch(optional_str(header, "type"), message)

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

    def configure_offer_metadata(self, *, moto_id: str, security_level: int) -> None:
        """Apply an HTTP token result without dropping the subscribed reader."""
        if security_level not in {2, 3, 4}:
            raise ValueError("Unsupported signaling security level")
        self._moto_id = moto_id
        self._security_level = security_level

    async def async_send_offer(
        self,
        sdp: str,
        ice_servers: JsonValue,
        trace_id: str,
        tcp_token: JsonValue,
        log_config: JsonValue,
    ) -> None:
        """Publish the offer that opens the session."""
        header = self._header("offer", trace_id)
        header.update(
            {
                "is_pre": 0,
                "p2p_skill": 1635,
                "security_level": self._security_level,
            }
        )
        await self._async_publish(
            SESSION_PROTOCOL,
            {
                "header": header,
                "msg": {
                    "sdp": sdp,
                    "preconnect": self._preconnect,
                    "token": ice_servers,
                    "tcp_token": tcp_token,
                    "log": log_config,
                },
            },
        )

    async def async_send_candidate(self, candidate: str) -> None:
        """Trickle one local ICE candidate."""
        await self._async_publish(
            SESSION_PROTOCOL,
            {"header": self._header("candidate"), "msg": {"candidate": candidate}},
        )

    async def async_send_disconnect(self, close_reason: int = 4) -> None:
        """
        Tear the session down.

        Without this the device holds the session open and refuses the next
        offer until its own timer fires.
        """
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
        )
        try:
            await client.publish(self._publish_topic, payload, qos=1)
            self._published += 1
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
