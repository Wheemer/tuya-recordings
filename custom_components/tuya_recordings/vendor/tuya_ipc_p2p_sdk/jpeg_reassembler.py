"""Reassembles channel-1 packets into whole JPEG frames."""

from __future__ import annotations

from .media import MAX_FRAME_SIZE, MEDIA_HEADER_LENGTH, MEDIA_OFFSET_FIELD

_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"
_MARKER_LENGTH = 2
_MINIMUM_FRAME_LENGTH = 4


def _starts_with_soi(data: bytes) -> bool:
    """Whether a buffer begins with the JPEG start-of-image marker."""
    return data[:2] == _SOI


class JpegReassembler:
    """
    Places chunks by offset, so out-of-order delivery still reassembles.

    A new frame begins when a packet's payload starts with the JPEG SOI and its
    offset is zero; the frame before it is completed and handed back at that
    moment.
    """

    def __init__(self) -> None:
        """Start with no frame in progress."""
        self._buffer: bytearray | None = None
        self._max_end = 0

    def push(self, packet: bytes) -> bytes | None:
        """Feed one channel-1 packet and return any frame it completed."""
        if len(packet) <= MEDIA_HEADER_LENGTH:
            return None
        offset = int.from_bytes(packet[MEDIA_OFFSET_FIELD : MEDIA_OFFSET_FIELD + 4], "big")
        payload = packet[MEDIA_HEADER_LENGTH:]

        completed: bytes | None = None
        if offset == 0 and _starts_with_soi(payload):
            completed = self._flush()
            self._buffer = bytearray()
            self._max_end = 0
        if self._buffer is None:
            return completed

        end = offset + len(payload)
        if end > MAX_FRAME_SIZE:
            return completed
        if end > len(self._buffer):
            self._buffer.extend(bytes(end - len(self._buffer)))
        self._buffer[offset:end] = payload
        self._max_end = max(self._max_end, end)
        return completed

    def _flush(self) -> bytes | None:
        """Return the frame in progress, if it is a complete JPEG."""
        if self._buffer is None or self._max_end < _MINIMUM_FRAME_LENGTH:
            return None
        data = bytes(self._buffer[: self._max_end])
        if not _starts_with_soi(data):
            return None
        end = data.rfind(_EOI)
        if end < _MARKER_LENGTH:
            return None
        return data[: end + 2]

    def reset(self) -> None:
        """Drop the frame in progress."""
        self._buffer = None
        self._max_end = 0
