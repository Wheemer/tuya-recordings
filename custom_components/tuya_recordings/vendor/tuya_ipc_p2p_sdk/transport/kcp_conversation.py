"""
One KCP conversation on an authenticated transport.

Direct ICE carries datagrams that may be lost or reordered. Selective ACKs
and cumulative una are distinct; sequence counters wrap at uint32. Bound
reassembly and retransmission state so failed peers cannot grow it forever.
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
_MAX_MESSAGE_BYTES = 16 * 1024 * 1024


def _difference(left: int, right: int) -> int:
    """Signed distance between the protocol's wrapping uint32 counters."""
    return ((left - right + 0x80000000) & 0xFFFFFFFF) - 0x80000000


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

    def __init__(
        self, conversation: int, transmit: Callable[[bytes], None],
        *, max_message_bytes: int = _MAX_MESSAGE_BYTES, backoff: bool = False,
    ) -> None:
        """Bind the conversation to the connection that carries its segments."""
        self.conversation = conversation
        if not 0 < max_message_bytes <= _MAX_MESSAGE_BYTES:
            raise ValueError("Invalid KCP message limit")
        self._max_message_bytes = max_message_bytes
        self._backoff = backoff
        self._transmit = transmit
        self._next_send_sequence = 0
        self._next_receive_sequence = 0
        self._received: dict[int, KcpSegment] = {}
        self._pending: list[_PendingSegment] = []
        self._fragments: list[bytes] = []
        self._fragment_bytes = 0
        self._expected_fragment: int | None = None
        self._retransmit_task: asyncio.Task[None] | None = None
        self._handler: Callable[[bytes], None] | None = None
        self._pending_messages: list[bytes] = []
        self._closed = False
        self._remote_window = _RECEIVE_WINDOW
        self._last_probe = _now_milliseconds()

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
    def closed(self) -> bool:
        return self._closed

    @property
    def receive_sequence(self) -> int:
        """Return the next inbound KCP sequence expected by this conversation."""
        return self._next_receive_sequence

    @property
    def pending_count(self) -> int:
        """Number of sent segments still waiting for transport acknowledgement."""
        return len(self._pending)

    def input(self, segment: KcpSegment) -> None:
        """Feed one inbound segment for this conversation."""
        if self._closed:
            return
        self._remote_window = segment.window
        self._retire_pending(segment.unacknowledged)
        if segment.command == CMD_ACK:
            self._pending = [entry for entry in self._pending if entry.sequence != segment.sequence]
            if not self._pending:
                self._disarm_retransmit()
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
        self._flush_pending()

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
        if _difference(segment.sequence, self._next_receive_sequence) < 0:
            return
        if _difference(segment.sequence, self._next_receive_sequence) >= _RECEIVE_WINDOW:
            return
        if segment.sequence in self._received:
            return
        self._received[segment.sequence] = segment

        while (nxt := self._received.pop(self._next_receive_sequence, None)) is not None:
            self._next_receive_sequence = (self._next_receive_sequence + 1) & 0xFFFFFFFF
            if self._expected_fragment is not None and nxt.fragment != self._expected_fragment:
                self.close()
                raise RuntimeError("KCP fragment countdown is invalid")
            self._fragment_bytes += len(nxt.data)
            if self._fragment_bytes > self._max_message_bytes:
                self.close()
                raise RuntimeError("KCP message exceeds receive limit")
            self._expected_fragment = nxt.fragment - 1 if nxt.fragment else None
            self._fragments.append(nxt.data)
            if nxt.fragment == 0:
                message = b"".join(self._fragments)
                self._fragments = []
                self._fragment_bytes = 0
                if not message:
                    continue
                if self._handler is not None:
                    self._handler(message)
                elif len(self._pending_messages) < _PENDING_MESSAGE_LIMIT:
                    # Relay media can arrive between conversation discovery and
                    # handler installation. Preserve those opening messages so
                    # an H.264 FU-A frame does not begin on a continuation packet.
                    self._pending_messages.append(message)

    def flush_pending(self) -> None:
        """Copy unacknowledged segments onto a newly attached transport."""
        if not self._closed:
            for pending in self._pending:
                self._transmit(pending.raw)

    def discard_pending(self) -> None:
        """Stop obsolete bootstrap retransmits without resetting receive state."""
        self._pending.clear()
        self._disarm_retransmit()

    def send(self, message: bytes) -> None:
        """Queue one message, fragmenting it when it exceeds the segment size."""
        if self._closed:
            raise RuntimeError("Failed to send: the KCP conversation is closed")
        count = max(1, -(-len(message) // _MAX_SEGMENT_SIZE))
        if count > 256 or len(message) > self._max_message_bytes:
            raise ValueError("KCP message exceeds fragment limit")
        if len(self._pending) + count > _RECEIVE_WINDOW:
            raise RuntimeError("KCP send queue is full")
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
                _PendingSegment(self._next_send_sequence, raw, _now_milliseconds(), 0)
            )
            self._next_send_sequence = (self._next_send_sequence + 1) & 0xFFFFFFFF
        self._flush_pending()
        self._arm_retransmit()

    def _flush_pending(self) -> None:
        if not self._pending:
            return
        origin = self._pending[0].sequence
        for entry in self._pending:
            if _difference(entry.sequence, origin) >= self._remote_window:
                break
            if not entry.transmits:
                entry.sent_at = _now_milliseconds()
                entry.transmits = 1
                self._transmit(entry.raw)

    def _retire_pending(self, unacknowledged: int) -> None:
        """Drop everything the peer has acknowledged."""
        while self._pending and _difference(self._pending[0].sequence, unacknowledged) < 0:
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
        if self._remote_window == 0 and _difference(now, self._last_probe) >= 3000:
            self._last_probe = now
            self._transmit(build_segment(self.conversation, CMD_WINDOW_ASK, 0,
                                         _RECEIVE_WINDOW, now, 0, self._next_receive_sequence))
        self._flush_pending()
        for entry in list(self._pending):
            if not entry.transmits:
                continue
            interval = _RETRANSMIT_INTERVAL_SECONDS * (
                2 ** max(0, entry.transmits - 1) if self._backoff else 1
            )
            if _difference(now, entry.sent_at) < interval * 1000:
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
        self._fragments.clear()
        self._fragment_bytes = 0
        self._expected_fragment = None
        self._handler = None

    async def async_close(self) -> None:
        """Close the conversation and await the retransmit loop."""
        task = self._retransmit_task
        self.close()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
