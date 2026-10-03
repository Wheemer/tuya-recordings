"""A multi-conversation KCP session carried only over nominated ICE."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..const import LOGGER
from ..exceptions import TuyaIpcP2pSessionError
from .kcp_conversation import KcpConversation
from .kcp_segment import parse_segments

if TYPE_CHECKING:
    from collections.abc import Callable

CONTROL_CONVERSATION = 0
VIDEO_CONVERSATION = 1
_MAX_CONVERSATION_ID = (1 << 32) - 1


class DirectSession:
    """Route Tuya KCP conversations over one authenticated LAN datagram path."""

    def __init__(self, transmit: Callable[[bytes], None]) -> None:
        self._transmit = transmit
        self._conversations: dict[int, KcpConversation] = {}
        self._segment_counts: dict[tuple[int, int], int] = {}
        self._malformed_datagrams = 0
        self._closed = False
        self._video_ready: asyncio.Future[KcpConversation] = (
            asyncio.get_event_loop().create_future()
        )
        self.control = self._conversation(CONTROL_CONVERSATION)

    def _conversation(self, conversation_id: int) -> KcpConversation:
        existing = self._conversations.get(conversation_id)
        if existing is not None:
            return existing
        created = KcpConversation(conversation_id, self._transmit)
        self._conversations[conversation_id] = created
        return created

    def conversation(self, conversation_id: int) -> KcpConversation:
        """Return a routed KCP conversation for a camera-defined channel ID."""
        if self._closed or not 0 <= conversation_id <= _MAX_CONVERSATION_ID:
            raise TuyaIpcP2pSessionError("Failed to open conversation: invalid channel")
        return self._conversation(conversation_id)

    def input_segment(self, raw: bytes) -> None:
        """Route one authenticated KCP datagram received directly from the camera."""
        segments = parse_segments(raw)
        if not segments:
            self._malformed_datagrams += 1
            return
        for segment in segments:
            key = (segment.conversation, segment.command)
            self._segment_counts[key] = self._segment_counts.get(key, 0) + 1
            known = segment.conversation in self._conversations
            conversation = self._conversation(segment.conversation)
            if not known and segment.conversation == VIDEO_CONVERSATION:
                if not self._video_ready.done():
                    self._video_ready.set_result(conversation)
                LOGGER.debug("The camera opened the direct video conversation")
            conversation.input(segment)

    async def async_wait_for_video(self, timeout_seconds: float) -> KcpConversation:
        """Wait until the camera opens the direct video conversation."""
        try:
            async with asyncio.timeout(timeout_seconds):
                return await asyncio.shield(self._video_ready)
        except TimeoutError as exception:
            raise TuyaIpcP2pSessionError(
                "Failed to start direct video: camera opened no video conversation"
            ) from exception

    @property
    def diagnostics(self) -> str:
        """Return non-secret direct-transport counters."""
        routed = ",".join(
            f"{conversation}:0x{command:02x}={count}"
            for (conversation, command), count in sorted(self._segment_counts.items())
        ) or "none"
        return f"routed={routed}, malformed_datagrams={self._malformed_datagrams}"

    async def async_close(self) -> None:
        """Close every conversation without touching any cloud relay."""
        if self._closed:
            return
        self._closed = True
        for conversation in self._conversations.values():
            await conversation.async_close()
        if not self._video_ready.done():
            self._video_ready.cancel()
