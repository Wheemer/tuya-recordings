"""The signaling MQTT channel for one session."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import ssl
import time
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

# The brokers present a certificate that does not match the regional hostname,
# so a verifying context never completes the handshake. The payloads carried on
# top are themselves encrypted under the device local key, which is what
# actually keeps the signaling private.
_UNVERIFIED_TLS = ssl.create_default_context()
_UNVERIFIED_TLS.check_hostname = False
_UNVERIFIED_TLS.verify_mode = ssl.CERT_NONE


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
        self._on_answer = on_answer
        self._on_candidate = on_candidate
        self._on_disconnect = on_disconnect
        self._protocol_version = protocol_version
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

    async def async_connect(self) -> None:
        """Connect the broker, subscribe, and start consuming inbound payloads."""
        client = aiomqtt.Client(
            hostname=self._identity.host,
            port=self._identity.port,
            username=self._identity.username,
            password=self._identity.password,
            identifier=self._identity.client_id,
            keepalive=_KEEPALIVE_SECONDS,
            tls_context=_UNVERIFIED_TLS,
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
            LOGGER.debug("Signaling reader stopped: %s", exception)

    def _consume(self, payload: bytes) -> None:
        """Decode one payload and dispatch it to the session."""
        try:
            data = decode_payload(self._key, payload)
        except TuyaIpcP2pError as exception:
            LOGGER.debug("Discarded an undecodable signaling payload: %s", exception)
            return
        header = data.get("header")
        message = data.get("msg")
        if not isinstance(header, dict) or not isinstance(message, dict):
            return
        session_id = optional_str(header, "sessionid")
        if session_id and session_id != self._session_id:
            return
        self._dispatch(optional_str(header, "type"), message)

    def _dispatch(self, message_type: str | None, message: JsonObject) -> None:
        """Hand one decoded message to the callback that owns it."""
        if message_type == "answer":
            sdp = optional_str(message, "sdp")
            if sdp:
                self._on_answer(sdp)
        elif message_type == "candidate":
            candidate = optional_str(message, "candidate")
            if candidate:
                self._on_candidate(candidate)
        elif message_type == "disconnect":
            self._on_disconnect(optional_int(message, "close_reason") or 0)

    async def async_send_sig_query(self) -> None:
        """Send the signal query that precedes the offer."""
        await self._async_publish(SIG_QUERY_PROTOCOL, {"reqType": "sigQry"})

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
                "security_level": 3,
            }
        )
        await self._async_publish(
            SESSION_PROTOCOL,
            {
                "header": header,
                "msg": {
                    "sdp": sdp,
                    "preconnect": True,
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
            "moto_id": "",
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
        except aiomqtt.MqttError as exception:
            raise TuyaIpcP2pConnectionError(f"Failed to publish: {exception}") from exception

    async def async_close(self) -> None:
        """Stop consuming and disconnect from the broker."""
        reader = self._reader
        self._reader = None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        client = self._client
        self._client = None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.__aexit__(None, None, None)
