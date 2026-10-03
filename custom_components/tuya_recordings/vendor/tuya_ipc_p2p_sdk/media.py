"""
Channel-1 media packets.

Each packet is a 20-byte header followed by payload bytes::

    +0   uint16   length/flags
    +4   uint32   frame id (constant within one image)
    +8   uint32   media type (0x0a)
    +12  uint32   big-endian byte offset of this chunk within the frame
    +16  4 bytes  marker 00 ff 50 3c
    +20  …        raw JPEG bytes

A decrypted record concatenates channel packets, each immediately preceded by a
four-byte little-endian length giving the packet's total size.
"""

from __future__ import annotations

MEDIA_HEADER_LENGTH = 20
MEDIA_OFFSET_FIELD = 12
MAX_FRAME_SIZE = 4 << 20

_MEDIA_MARKER = b"\x00\xff\x50\x3c"
_MARKER_OFFSET = 16
_LENGTH_PREFIX_LENGTH = 4


def extract_media_packets(record: bytes) -> list[bytes]:
    """
    Pull the channel-1 packets out of one decrypted record.

    The marker is what makes a packet findable: the length prefix sits four
    bytes before the header it describes, and a record can carry several
    packets back to back.
    """
    packets: list[bytes] = []
    cursor = 0
    while True:
        marker_position = record.find(_MEDIA_MARKER, cursor)
        if marker_position < 0:
            return packets
        header = marker_position - _MARKER_OFFSET
        if header >= _LENGTH_PREFIX_LENGTH:
            length = int.from_bytes(record[header - _LENGTH_PREFIX_LENGTH : header], "little")
            if MEDIA_HEADER_LENGTH <= length <= MAX_FRAME_SIZE and header + length <= len(record):
                packets.append(record[header : header + length])
                cursor = header + length
                continue
        cursor = marker_position + len(_MEDIA_MARKER)
