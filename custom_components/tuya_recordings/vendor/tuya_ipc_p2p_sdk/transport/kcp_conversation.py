"""
One KCP conversation on a shared relay connection.

The relay runs over TCP, so segments arrive in order and are never lost in
transit. What still matters on the wire is the bookkeeping the peer expects:
every push has to be acknowledged or the device stops sending, ``una`` has to
advance so the device can retire its send queue, and fragmented messages have
to be rejoined.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..const import LOGGER
from .kcp_segment import (
    CMD_ACK,
    CMD_PUSH,
    CMD_WINDOW_ASK,
    CMD_WINDOW_TELL,
    KcpSegment,
    build_segment,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_RECEIVE_WINDOW = 512
_RETRANSMIT_INTERVAL_SECONDS = 0.3
_MAX_RETRANSMITS = 8
_MAX_SEGMENT_SIZE = 1376
_PENDING_MESSAGE_LIMIT = 32


def _now_milliseconds() -> int:
    """Return a 32-bit millisecond clock, which is what the header carries."""
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


@dataclass(slots=True)
class _PendingSegment:
    """A pushed segment still waiting to be acknowledged."""

    sequence: int
    raw: bytes
    sent_at: int
    transmits: int


class KcpConversation:
    """Reassembles inbound messages and keeps the outbound queue acknowledged."""

    def __init__(self, conversation: int, transmit: Callable[[bytes], None]) -> None:
        """Bind the conversation to the connection that carries its segments."""
        self.conversation = conversation
        self._transmit = transmit
        self._next_send_sequence = 0
        self._next_receive_sequence = 0
        self._received: dict[int, KcpSegment] = {}
        self._pending: list[_PendingSegment] = []
        self._fragments: list[bytes] = []
        self._retransmit_task: asyncio.Task[None] | None = None
        self._handler: Callable[[bytes], None] | None = None
        self._pending_messages: list[bytes] = []
        self._closed = False

    def set_message_handler(self, handler: Callable[[bytes], None] | None) -> None:
        """Install the callback that receives every reassembled message."""
        self._handler = handler
        if handler is None:
            self._pending_messages.clear()
            return

        pending, self._pending_messages = self._pending_messages, []
        for message in pending:
            handler(message)

    @property
    def receive_sequence(self) -> int:
        """Return the next inbound KCP sequence expected by this conversation."""
        return self._next_receive_sequence

    def input(self, segment: KcpSegment) -> None:
        """Feed one inbound segment for this conversation."""
        if self._closed:
            return
        self._retire_pending(segment.unacknowledged)
        if segment.command == CMD_ACK:
            self._retire_pending(segment.sequence + 1)
        elif segment.command == CMD_PUSH:
            # Deliver before acknowledging: the acknowledgement carries the
            # receive sequence the delivery advanced, which is how the device
            # retires its own send queue.
            self._accept(segment)
            self._acknowledge(segment)
        elif segment.command == CMD_WINDOW_ASK:
            self._transmit(
                build_segment(
                    self.conversation,
                    CMD_WINDOW_TELL,
                    0,
                    _RECEIVE_WINDOW,
                    _now_milliseconds(),
                    0,
                    self._next_receive_sequence,
                )
            )

    def _acknowledge(self, segment: KcpSegment) -> None:
        """Acknowledge one received push."""
        self._transmit(
            build_segment(
                self.conversation,
                CMD_ACK,
                0,
                _RECEIVE_WINDOW,
                segment.timestamp,
                segment.sequence,
                self._next_receive_sequence,
            )
        )

    def _accept(self, segment: KcpSegment) -> None:
        """
        Queue a received segment and deliver what the queue can now complete.

        A message spans ``frg + 1`` segments, counted down to zero.
        """
        if segment.sequence < self._next_receive_sequence:
            return
        if segment.sequence >= self._next_receive_sequence + _RECEIVE_WINDOW:
            return
        if segment.sequence in self._received:
            return
        self._received[segment.sequence] = segment

        while (nxt := self._received.pop(self._next_receive_sequence, None)) is not None:
            self._next_receive_sequence += 1
            self._fragments.append(nxt.data)
            if nxt.fragment == 0:
                message = b"".join(self._fragments)
                self._fragments = []
                if not message:
                    continue
                if self._handler is not None:
                    self._handler(message)
                elif len(self._pending_messages) < _PENDING_MESSAGE_LIMIT:
                    # Relay media can arrive between conversation discovery and
                    # handler installation. Preserve those opening messages so
                    # an H.264 FU-A frame does not begin on a continuation packet.
                    self._pending_messages.append(message)

    def send(self, message: bytes) -> None:
        """Queue one message, fragmenting it when it exceeds the segment size."""
        if self._closed:
            raise RuntimeError("Failed to send: the KCP conversation is closed")
        count = max(1, -(-len(message) // _MAX_SEGMENT_SIZE))
        for index in range(count):
            chunk = message[index * _MAX_SEGMENT_SIZE : (index + 1) * _MAX_SEGMENT_SIZE]
            raw = build_segment(
                self.conversation,
                CMD_PUSH,
                count - 1 - index,
                _RECEIVE_WINDOW,
                _now_milliseconds(),
                self._next_send_sequence,
                self._next_receive_sequence,
                chunk,
            )
            self._pending.append(
                _PendingSegment(self._next_send_sequence, raw, _now_milliseconds(), 1)
            )
            self._next_send_sequence += 1
            self._transmit(raw)
        self._arm_retransmit()

    def _retire_pending(self, unacknowledged: int) -> None:
        """Drop everything the peer has acknowledged."""
        while self._pending and self._pending[0].sequence < unacknowledged:
            self._pending.pop(0)
        if not self._pending:
            self._disarm_retransmit()

    def _arm_retransmit(self) -> None:
        """Start the retransmit loop, if it is not already running."""
        if self._retransmit_task is not None or self._closed:
            return
        self._retransmit_task = asyncio.create_task(self._async_retransmit_loop())

    def _disarm_retransmit(self) -> None:
        """Stop the retransmit loop."""
        task = self._retransmit_task
        self._retransmit_task = None
        if task is not None:
            task.cancel()

    async def _async_retransmit_loop(self) -> None:
        """Resend unacknowledged segments until the peer catches up or gives up."""
        try:
            while not self._closed and self._pending:
                await asyncio.sleep(_RETRANSMIT_INTERVAL_SECONDS)
                self._retransmit_once()
        except asyncio.CancelledError:
            raise
        except Exception as exception:
            LOGGER.debug("KCP retransmit loop stopped: %s", exception)

    def _retransmit_once(self) -> None:
        """Resend whatever has been waiting longer than the retransmit interval."""
        now = _now_milliseconds()
        for entry in list(self._pending):
            if now - entry.sent_at < _RETRANSMIT_INTERVAL_SECONDS * 1000:
                continue
            if entry.transmits >= _MAX_RETRANSMITS:
                LOGGER.warning(
                    "Failed to deliver KCP segment %s after %s transmits",
                    entry.sequence,
                    entry.transmits,
                )
                self.close()
                return
            entry.sent_at = now
            entry.transmits += 1
            self._transmit(entry.raw)

    def close(self) -> None:
        """Stop the conversation and release everything it was holding."""
        if self._closed:
            return
        self._closed = True
        self._disarm_retransmit()
        self._received.clear()
        self._pending.clear()
        self._pending_messages.clear()
        self._handler = None

    async def async_close(self) -> None:
        """Close the conversation and await the retransmit loop."""
        task = self._retransmit_task
        self.close()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
