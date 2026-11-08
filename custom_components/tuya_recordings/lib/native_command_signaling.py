"""APK native command signaling; TCP is never connected to the media router."""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Awaitable, Callable

from ..vendor.tuya_ipc_p2p_sdk.crypto import decrypt_record, encrypt_record
from ..vendor.tuya_ipc_p2p_sdk.exceptions import TuyaIpcP2pError
from ..vendor.tuya_ipc_p2p_sdk.models.relay_token import RelayToken
from ..vendor.tuya_ipc_p2p_sdk.signaling.handshake_signer import HandshakeSigner
from ..vendor.tuya_ipc_p2p_sdk.transport.kcp_conversation import KcpConversation
from ..vendor.tuya_ipc_p2p_sdk.transport.kcp_segment import (
    CMD_PUSH, KcpSegment, build_segment, parse_segments,
)
from ..vendor.tuya_ipc_p2p_sdk.transport.relay_connection import RelayConnection

COMMAND_CONVERSATION = 0x010000F3
_CHUNK = 1300
_MAX_RECORD = 4092
_MAX_ENCRYPTED_CHUNK = 1328


class NativeCommandSignaling:
    """One bounded, session-bound signaling conversation, never a media session."""

    def __init__(
        self, *, token: RelayToken | None, device_id: str, uid: str, session_id: str,
        key: bytes, transmit_direct: Callable[[bytes], None],
        on_signal: Callable[[dict], None],
    ) -> None:
        self._key = key
        self._device_id = device_id
        self._uid = uid
        self._session_id = session_id
        self._direct = transmit_direct
        self._on_signal = on_signal
        self._connection = None
        if token is not None:
            signer = HandshakeSigner(
                token.credential, token.expire_timestamp, device_id, token.session_id, uid,
            )
            self._connection = RelayConnection(token, signer, device_id, uid, key)
            self._connection.set_segment_handler(self._input_tcp)
            self._connection.set_close_handler(self._on_tcp_closed)
        self._channel = KcpConversation(
            COMMAND_CONVERSATION, self._write,
            max_message_bytes=_MAX_ENCRYPTED_CHUNK, backoff=True,
        )
        self._channel.set_message_handler(self._receive)
        self._buffer = bytearray()
        self._queued: list[bytes] = []
        self._tasks: list[asyncio.Task] = []
        self._tcp_ready = False
        self._direct_ready = False
        self._closed = False
        self._bootstrap_finished = False
        self._state = "created"
        self._signals = 0
        self._invalid = 0
        self._blocked = 0
        self._mismatches = 0
        self._failure = "none"
        self._input_source = "none"
        self._signal_source = "none"
        self._tcp_attempts = 0
        self._started = False
        self._tcp_last_push: int | None = None
        self._segment_counts: dict[tuple[str, int], int] = {}
        self._tcp_push_duplicates = 0
        self._tcp_pushes = 0
        self._lan_packets = 0

    def queue(self, payload: bytes) -> None:
        """Queue a single framed signaling message, with bounded plaintext."""
        if self._closed or self._bootstrap_finished:
            return
        if not 0 < len(payload) <= _MAX_RECORD - 4 or len(self._queued) >= 16:
            raise ValueError("Native command signaling queue limit")
        self._queued.append(payload)
        self._flush()

    def _flush(self) -> None:
        if self._closed or not self._tcp_ready:
            return
        queued, self._queued = self._queued, []
        for payload in queued:
            frame = struct.pack(">HH", 1, len(payload)) + payload
            frame += bytes((-len(payload)) % 4)
            for offset in range(0, len(frame), _CHUNK):
                self._channel.send(encrypt_record(self._key, frame[offset:offset + _CHUNK]))

    async def start(self, wait_direct: Callable[[], Awaitable[None]], *, tcp: bool) -> None:
        """Bootstrap transports concurrently within the owner's answer deadline."""
        if self._started or self._closed:
            raise RuntimeError("Native command bootstrap cannot be restarted")
        self._started = True
        async def direct() -> None:
            try:
                await wait_direct()
                if not self._closed:
                    self._direct_ready = True
                    self._state = "direct_ready"
                    self._channel.flush_pending()
                    self._flush()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._failure = type(error).__name__

        async def connect() -> None:
            try:
                self._tcp_attempts += 1
                await self._connection.async_connect()
                if not self._closed:
                    self._tcp_ready = True
                    self._state = "tcp_signaling_ready"
                    self._channel.flush_pending()
                    self._flush()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._failure = type(error).__name__
                await self._connection.async_close()

        self._state = "connecting"
        self._tasks = [asyncio.create_task(direct())]
        if tcp and self._connection is not None:
            self._tasks.append(asyncio.create_task(connect()))
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            raise

    def _write(self, raw: bytes) -> None:
        if self._closed:
            return
        for segment in parse_segments(raw):
            # APK a76a4: command PUSH never uses ICE, and reliable TCP gets
            # each PUSH sequence once; ACK/window traffic can use both paths.
            packet = build_segment(
                segment.conversation, segment.command, segment.fragment,
                segment.window, segment.timestamp, segment.sequence,
                segment.unacknowledged, segment.data,
            )
            if self._tcp_ready:
                if segment.command != CMD_PUSH or self._tcp_last_push is None or (
                    0 < ((segment.sequence - self._tcp_last_push) & 0xFFFFFFFF) < 0x80000000
                ):
                    self._connection.write_segment(packet)
                    if segment.command == CMD_PUSH:
                        self._tcp_last_push = segment.sequence
                        self._tcp_pushes += 1
                else:
                    self._tcp_push_duplicates += 1
            if self._direct_ready and segment.command != CMD_PUSH:
                self._direct(packet)
                self._lan_packets += 1

    def input_direct(self, segment: KcpSegment) -> None:
        """Accept only this conversation from the authenticated LAN router."""
        if not self._closed and segment.conversation == COMMAND_CONVERSATION:
            self._direct_ready = True
            self._input_source = "LAN"
            self._input(segment)

    def _input_tcp(self, raw: bytes) -> None:
        if self._closed or self._bootstrap_finished:
            return
        segments = parse_segments(raw)
        if not segments:
            self._invalid += 1
        for segment in segments:
            if segment.conversation != COMMAND_CONVERSATION:
                self._blocked += 1
                continue
            self._input_source = "TCP"
            self._input(segment)

    def _input(self, segment: KcpSegment) -> None:
        count_key = (self._input_source, segment.command)
        self._segment_counts[count_key] = self._segment_counts.get(count_key, 0) + 1
        try:
            if len(segment.data) > _MAX_ENCRYPTED_CHUNK:
                raise ValueError("Native command chunk limit")
            self._channel.input(segment)
        except Exception as error:
            self._invalid += 1
            self._failure = type(error).__name__
            self._channel.close()

    def _receive(self, record: bytes) -> None:
        try:
            plain = decrypt_record(self._key, record)
            while plain:
                free = _MAX_RECORD - len(self._buffer)
                if free < 1:
                    raise ValueError("Native command buffer limit")
                self._buffer.extend(plain[:free])
                plain = plain[free:]
                while len(self._buffer) >= 4:
                    kind, length = struct.unpack_from(">HH", self._buffer)
                    size = 4 + length + (-length % 4)
                    if not length or size > _MAX_RECORD:
                        raise ValueError("Invalid native command length")
                    if len(self._buffer) < size:
                        break
                    payload = bytes(self._buffer[4:4 + length])
                    del self._buffer[:size]
                    if kind == 1:
                        self._dispatch(json.loads(payload))
        except (ValueError, UnicodeError, TypeError, TuyaIpcP2pError) as error:
            self._invalid += 1
            self._failure = type(error).__name__
            self._buffer.clear()
            self._channel.close()

    def _dispatch(self, value: object) -> None:
        if not isinstance(value, dict):
            raise ValueError("Native signaling must be an object")
        header, message = value.get("header"), value.get("msg")
        if not isinstance(header, dict) or not isinstance(message, dict):
            raise ValueError("Invalid native signaling shape")
        if (header.get("sessionid") != self._session_id
                or header.get("from") != self._device_id
                or header.get("to") not in (None, self._uid)):
            self._mismatches += 1
            return
        if header.get("type") not in {"answer", "candidate", "disconnect"}:
            self._invalid += 1
            return
        kind = header["type"]
        if ((kind == "answer" and not isinstance(message.get("sdp"), str))
                or (kind == "candidate" and not isinstance(message.get("candidate"), str))
                or (kind == "disconnect" and type(message.get("close_reason")) is not int)):
            self._invalid += 1
            return
        self._signals += 1
        self._signal_source = self._input_source
        self._on_signal(value)

    def _on_tcp_closed(self, error: Exception | None) -> None:
        self._tcp_ready = False
        if not self._closed and not self._bootstrap_finished:
            self._state = "tcp_closed_direct_ready" if self._direct_ready else "tcp_closed"
        if error is not None and not self._closed:
            self._failure = type(error).__name__

    async def finish_bootstrap(self) -> None:
        """Close TCP after the answer; leave only direct command signaling."""
        self._bootstrap_finished = True
        self._tcp_ready = False
        self._queued.clear()
        self._channel.discard_pending()
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._connection is not None:
            await self._connection.async_close()

    async def async_close(self) -> None:
        self._closed = True
        self._queued.clear()
        self._buffer.clear()
        try:
            await self.finish_bootstrap()
        finally:
            await self._channel.async_close()

    @property
    def accepting_signals(self) -> bool:
        return not self._closed and not self._channel.closed and (
            not self._bootstrap_finished or self._direct_ready
        )

    @property
    def diagnostics(self) -> str:
        packets = ",".join(
            f"{source}:0x{command:02x}={count}"
            for (source, command), count in sorted(self._segment_counts.items())
        ) or "none"
        return (
            f"state={self._state}, tcp_signaling={self._tcp_ready}, tcp_media=blocked, "
            f"signals={self._signals}, invalid={self._invalid}, "
            f"source={self._signal_source}, tcp_attempts={self._tcp_attempts}, "
            f"session_mismatches={self._mismatches}, blocked_media={self._blocked}, "
            f"failure={self._failure}, packets={packets}, "
            f"tcp_duplicate_pushes_suppressed={self._tcp_push_duplicates}, "
            f"tcp_pushes={self._tcp_pushes}, lan_packets={self._lan_packets}, "
            f"queued_signals={len(self._queued)}, "
            f"tcp_details=({self._connection.diagnostics if self._connection is not None else 'not_configured'})"
        )
