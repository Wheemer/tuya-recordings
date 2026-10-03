"""A multi-conversation KCP media session over one relay connection."""

from __future__ import annotations

import asyncio
import secrets
from typing import TYPE_CHECKING

from ..const import LOGGER
from ..crypto import aes_cbc_encrypt_raw, pad_pkcs7
from ..exceptions import TuyaIpcP2pSessionError
from .kcp_conversation import KcpConversation
from .kcp_segment import CMD_PUSH, build_segment, parse_segments
from .relay_connection import RelayConnection

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..models import RelayToken
    from ..signaling import HandshakeSigner

# Conversation 0 is the bidirectional control channel, 1 is video and 2 audio
# (device to client), and 0x010000f3 is the client's signaling tunnel.
CONTROL_CONVERSATION = 0
VIDEO_CONVERSATION = 1
SIGNALING_CONVERSATION = 0x010000F3

_TUNNEL_WINDOW = 64
_MAX_CONVERSATION_ID = (1 << 32) - 1

# The maximum plaintext bytes per signaling record. It must stay a multiple of
# the AES block size: the message is padded once, as a whole, and only then
# split — padding each chunk instead injects pad bytes mid-message and the
# device discards it.
_TUNNEL_CHUNK = 1312


class RelaySession:
    """
    Routes the conversations a relay connection multiplexes.

    Every conversation the device opens has to be serviced. Leaving one unread
    stalls the device's sender for the whole session, which stops the video as
    well — so conversations nobody listens to still get their acknowledgements.
    """

    def __init__(
        self,
        token: RelayToken,
        signer: HandshakeSigner,
        device_id: str,
        uid: str,
        media_key: bytes,
        direct_transmit: Callable[[bytes], None] | None = None,
    ) -> None:
        """Describe the relay rendezvous this session runs over."""
        self._connection = RelayConnection(token, signer, device_id, uid, media_key)
        self._direct_transmit = direct_transmit
        self._conversations: dict[int, KcpConversation] = {}
        self._segment_counts: dict[tuple[int, int], int] = {}
        self._malformed_datagrams = 0
        self._signaling_sequence = 0
        self._closed = False
        self._direct_ready = asyncio.Event()
        self._video_ready: asyncio.Future[KcpConversation] = (
            asyncio.get_event_loop().create_future()
        )
        self._close_handler: Callable[[Exception | None], None] | None = None
        self.control = self._conversation(CONTROL_CONVERSATION)

    async def async_connect(self) -> None:
        """Dial and authenticate the relay, then start routing its segments."""
        self._connection.set_segment_handler(self._route)
        self._connection.set_close_handler(self._on_connection_closed)
        await self._connection.async_connect()

    def set_close_handler(self, handler: Callable[[Exception | None], None] | None) -> None:
        """Install the callback that fires when the underlying connection ends."""
        self._close_handler = handler

    def _conversation(self, conversation_id: int) -> KcpConversation:
        """Return the conversation with that id, creating it on first sight."""
        existing = self._conversations.get(conversation_id)
        if existing is not None:
            return existing
        created = KcpConversation(conversation_id, self._write_segment)
        self._conversations[conversation_id] = created
        return created

    def conversation(self, conversation_id: int) -> KcpConversation:
        """Return a routed KCP conversation for a camera-defined channel ID."""
        if self._closed or not 0 <= conversation_id <= _MAX_CONVERSATION_ID:
            raise TuyaIpcP2pSessionError("Failed to open conversation: invalid channel")
        return self._conversation(conversation_id)

    def _route(self, raw: bytes) -> bool:
        """Hand every inbound segment to the conversation it belongs to."""
        segments = parse_segments(raw)
        if not segments:
            self._malformed_datagrams += 1
            return False
        for segment in segments:
            key = (segment.conversation, segment.command)
            self._segment_counts[key] = self._segment_counts.get(key, 0) + 1
            known = segment.conversation in self._conversations
            conversation = self._conversation(segment.conversation)
            if not known and segment.conversation == VIDEO_CONVERSATION:
                if not self._video_ready.done():
                    self._video_ready.set_result(conversation)
                LOGGER.debug("The device opened the video conversation")
            conversation.input(segment)
        return True

    def input_segment(self, raw: bytes) -> None:
        """Route one KCP segment received over a direct ICE candidate pair."""
        if self._route(raw):
            self._direct_ready.set()

    async def async_wait_for_direct(self) -> None:
        """Wait until authenticated direct ICE carries one valid KCP segment."""
        await self._direct_ready.wait()

    @property
    def direct_ready(self) -> bool:
        """Return whether the direct path has carried authenticated KCP."""
        return self._direct_ready.is_set()

    def _write_segment(self, segment: bytes) -> None:
        """Mirror native output across the active TCP and direct transports."""
        self._connection.write_segment(segment)
        if self._direct_transmit is not None:
            self._direct_transmit(segment)

    def set_direct_transmit(
        self, transmit: Callable[[bytes], None] | None
    ) -> None:
        """Attach the nominated direct ICE path after relay construction."""
        if self._closed:
            raise TuyaIpcP2pSessionError(
                "Failed to attach direct transport: relay is closed"
            )
        self._direct_transmit = transmit

    async def async_wait_for_video(self, timeout_seconds: float) -> KcpConversation:
        """Wait until the device opens the video conversation."""
        try:
            async with asyncio.timeout(timeout_seconds):
                return await asyncio.shield(self._video_ready)
        except TimeoutError as exception:
            raise TuyaIpcP2pSessionError(
                "Failed to start the video: the device opened no video conversation"
            ) from exception

    @property
    def diagnostics(self) -> str:
        """Return non-secret relay counters for failure reporting."""
        routed = ",".join(
            f"{conversation}:0x{command:02x}={count}"
            for (conversation, command), count in sorted(self._segment_counts.items())
        ) or "none"
        return (
            f"{self._connection.diagnostics}, routed={routed}, "
            f"malformed_datagrams={self._malformed_datagrams}"
        )

    def send_tunnel_frame(self, media_key: bytes, frame_json: bytes) -> None:
        """
        Write one signaling frame to the tunnel conversation.

        The frame is ``00 01 <len16> <json>`` — an odd-length body gets a NUL
        to reach a two-byte boundary — PKCS#7-padded as a whole, then split
        into AES-CBC records, one KCP message per record.
        """
        parts = [b"\x00\x01" + len(frame_json).to_bytes(2, "big"), frame_json]
        if len(frame_json) % 2:
            parts.append(b"\x00")
        padded = pad_pkcs7(b"".join(parts))
        for offset in range(0, len(padded), _TUNNEL_CHUNK):
            iv = secrets.token_bytes(16)
            chunk = padded[offset : offset + _TUNNEL_CHUNK]
            self._send_tunnel_segment(iv + aes_cbc_encrypt_raw(media_key, iv, chunk))

    def _send_tunnel_segment(self, record: bytes) -> None:
        """
        Write one record as a hand-built KCP push, sent exactly once.

        The device never acknowledges this conversation, so it must not be a
        real KCP one: a retransmitting sender would replay the offer for the
        whole call, and replaying the offer makes the device stop streaming.
        """
        self._connection.write_segment(
            build_segment(
                SIGNALING_CONVERSATION,
                CMD_PUSH,
                0,
                _TUNNEL_WINDOW,
                0,
                self._signaling_sequence,
                0,
                record,
            )
        )
        self._signaling_sequence += 1

    def _on_connection_closed(self, error: Exception | None) -> None:
        """Close every conversation and tell the session the transport is gone."""
        if self._closed:
            return
        self._closed = True
        for conversation in self._conversations.values():
            conversation.close()
        if not self._video_ready.done():
            self._video_ready.cancel()
        handler = self._close_handler
        self._close_handler = None
        if handler is not None:
            handler(error)

    async def async_close(self) -> None:
        """Close every conversation and drop the relay connection."""
        self._closed = True
        for conversation in self._conversations.values():
            await conversation.async_close()
        if not self._video_ready.done():
            self._video_ready.cancel()
        self._close_handler = None
        await self._connection.async_close()
