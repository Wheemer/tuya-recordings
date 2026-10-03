"""
One KCP segment, as Tuya carries it over the relay.

Several conversations share one relay connection and the conversation id in the
first four bytes routes each segment. The 24-byte header is little-endian:
``conv u32 | cmd u8 | frg u8 | wnd u16 | ts u32 | sn u32 | una u32 | len u32``.
"""

from __future__ import annotations

from dataclasses import dataclass

KCP_HEADER_LENGTH = 24

CMD_PUSH = 0x51
CMD_ACK = 0x52
CMD_WINDOW_ASK = 0x53
CMD_WINDOW_TELL = 0x54


@dataclass(frozen=True, slots=True)
class KcpSegment:
    """A parsed segment, with its payload already split off."""

    conversation: int
    command: int
    fragment: int
    window: int
    timestamp: int
    sequence: int
    unacknowledged: int
    data: bytes


def parse_segment(raw: bytes) -> KcpSegment | None:
    """Parse one segment, or return None when the buffer does not hold a whole one."""
    if len(raw) < KCP_HEADER_LENGTH:
        return None
    length = int.from_bytes(raw[20:24], "little")
    if KCP_HEADER_LENGTH + length > len(raw):
        return None
    return KcpSegment(
        conversation=int.from_bytes(raw[0:4], "little"),
        command=raw[4],
        fragment=raw[5],
        window=int.from_bytes(raw[6:8], "little"),
        timestamp=int.from_bytes(raw[8:12], "little"),
        sequence=int.from_bytes(raw[12:16], "little"),
        unacknowledged=int.from_bytes(raw[16:20], "little"),
        data=raw[KCP_HEADER_LENGTH : KCP_HEADER_LENGTH + length],
    )


def parse_segments(raw: bytes) -> list[KcpSegment]:
    """Parse every segment in one KCP output datagram.

    KCP may batch several complete segments into one output buffer. Reject the
    whole datagram when any trailing segment is incomplete so callers never
    route a valid-looking prefix from a malformed packet.
    """
    segments: list[KcpSegment] = []
    offset = 0
    while offset < len(raw):
        if len(raw) - offset < KCP_HEADER_LENGTH:
            return []
        length = int.from_bytes(raw[offset + 20 : offset + 24], "little")
        end = offset + KCP_HEADER_LENGTH + length
        if end > len(raw):
            return []
        segment = parse_segment(raw[offset:end])
        if segment is None:
            return []
        segments.append(segment)
        offset = end
    return segments


def build_segment(
    conversation: int,
    command: int,
    fragment: int,
    window: int,
    timestamp: int,
    sequence: int,
    unacknowledged: int,
    data: bytes = b"",
) -> bytes:
    """Serialize one segment."""
    return b"".join(
        (
            (conversation & 0xFFFFFFFF).to_bytes(4, "little"),
            bytes((command, fragment)),
            (window & 0xFFFF).to_bytes(2, "little"),
            (timestamp & 0xFFFFFFFF).to_bytes(4, "little"),
            (sequence & 0xFFFFFFFF).to_bytes(4, "little"),
            (unacknowledged & 0xFFFFFFFF).to_bytes(4, "little"),
            len(data).to_bytes(4, "little"),
            data,
        )
    )
