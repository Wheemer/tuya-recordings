"""ThingCameraSimple media framing and RTP packets recovered from Smart Life."""

from __future__ import annotations

import struct
from dataclasses import dataclass

SIMPLE_MEDIA_HEADER_BYTES = 24
SIMPLE_MEDIA_EXTENSION_BYTES = 8
SIMPLE_MEDIA_CODEC_INFO_BYTES = 52
SIMPLE_MEDIA_MAX_PAYLOAD_BYTES = 10_485_100


class NativeSimpleRtpError(ValueError):
    """A type-4 media stream contains an invalid frame or RTP packet."""


@dataclass(frozen=True, slots=True)
class NativeSimpleMediaPacket:
    """One packet emitted by ThingAvStreamReader's exact-read protocol."""

    header: bytes
    extension: bytes
    codec_info: bytes
    payload: bytes

    @property
    def stream_id(self) -> int:
        """Return the persistent media-stream identifier in the native header."""
        return struct.unpack_from("<I", self.header)[0]


@dataclass(frozen=True, slots=True)
class NativeRtpPacket:
    """The fields needed to depacketize one RTP packet."""

    payload_type: int
    marker: bool
    sequence: int
    timestamp: int
    ssrc: int
    payload: bytes


class NativeSimpleMediaParser:
    """Incrementally implement ThingAvStreamReader::ThingReadData calls."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> tuple[NativeSimpleMediaPacket, ...]:
        if not isinstance(data, bytes) or not data:
            raise NativeSimpleRtpError("Simple media input must be non-empty bytes")
        self._buffer.extend(data)
        packets: list[NativeSimpleMediaPacket] = []
        while len(self._buffer) >= SIMPLE_MEDIA_HEADER_BYTES:
            header = bytes(self._buffer[:SIMPLE_MEDIA_HEADER_BYTES])
            has_extension = bool(struct.unpack_from("<I", header, 16)[0])
            extension_size = SIMPLE_MEDIA_EXTENSION_BYTES if has_extension else 0
            prefix_size = SIMPLE_MEDIA_HEADER_BYTES + extension_size
            if len(self._buffer) < prefix_size:
                break
            extension = bytes(
                self._buffer[SIMPLE_MEDIA_HEADER_BYTES:prefix_size]
            )
            codec_size = (
                SIMPLE_MEDIA_CODEC_INFO_BYTES
                if extension_size and extension[1] == 1
                else 0
            )
            length_offset = prefix_size + codec_size
            if len(self._buffer) < length_offset + 4:
                break
            payload_length = struct.unpack_from("<I", self._buffer, length_offset)[0]
            if payload_length > SIMPLE_MEDIA_MAX_PAYLOAD_BYTES:
                self._buffer.clear()
                raise NativeSimpleRtpError(
                    "Simple media payload exceeds the native SDK limit"
                )
            total = length_offset + 4 + payload_length
            if len(self._buffer) < total:
                break
            codec_info = bytes(self._buffer[prefix_size:length_offset])
            payload = bytes(self._buffer[length_offset + 4:total])
            del self._buffer[:total]
            packets.append(
                NativeSimpleMediaPacket(header, extension, codec_info, payload)
            )
        return tuple(packets)


class NativeH264RtpDepacketizer:
    """Reassemble RFC 6184 packets into Annex-B access units."""

    def __init__(self) -> None:
        self._timestamp: int | None = None
        self._last_sequence: int | None = None
        self._units: list[bytes] = []
        self._fragment: bytearray | None = None

    def push(self, packet: NativeRtpPacket) -> tuple[bytes, ...]:
        if not packet.payload:
            raise NativeSimpleRtpError("H.264 RTP payload is empty")
        completed: list[bytes] = []
        if self._timestamp is not None and packet.timestamp != self._timestamp:
            previous = self._finish()
            if previous is not None:
                completed.append(previous)
        if self._last_sequence is not None and packet.sequence != (
            self._last_sequence + 1
        ) & 0xFFFF:
            self._reset()
        self._timestamp = packet.timestamp
        self._last_sequence = packet.sequence
        nal_type = packet.payload[0] & 0x1F
        if 1 <= nal_type <= 23:
            self._units.append(packet.payload)
        elif nal_type == 24:
            self._push_stap_a(packet.payload)
        elif nal_type == 28:
            self._push_fu_a(packet.payload)
        else:
            raise NativeSimpleRtpError(
                f"Unsupported H.264 RTP NAL unit type {nal_type}"
            )
        if not packet.marker:
            return tuple(completed)
        current = self._finish()
        if current is None:
            raise NativeSimpleRtpError("H.264 marker ended an incomplete access unit")
        completed.append(current)
        return tuple(completed)

    def _push_stap_a(self, payload: bytes) -> None:
        offset = 1
        while offset < len(payload):
            if offset + 2 > len(payload):
                raise NativeSimpleRtpError("H.264 STAP-A length is truncated")
            length = struct.unpack_from(">H", payload, offset)[0]
            offset += 2
            if not length or offset + length > len(payload):
                raise NativeSimpleRtpError("H.264 STAP-A unit is truncated")
            self._units.append(payload[offset:offset + length])
            offset += length

    def _push_fu_a(self, payload: bytes) -> None:
        if len(payload) < 3:
            raise NativeSimpleRtpError("H.264 FU-A payload is truncated")
        indicator, header = payload[0], payload[1]
        start = bool(header & 0x80)
        end = bool(header & 0x40)
        nal_header = (indicator & 0xE0) | (header & 0x1F)
        if start:
            if self._fragment is not None:
                raise NativeSimpleRtpError("H.264 FU-A start overlaps a fragment")
            self._fragment = bytearray((nal_header,))
        elif self._fragment is None:
            raise NativeSimpleRtpError("H.264 FU-A continuation has no start")
        assert self._fragment is not None
        self._fragment.extend(payload[2:])
        if end:
            self._units.append(bytes(self._fragment))
            self._fragment = None

    def _reset(self) -> None:
        self._timestamp = None
        self._last_sequence = None
        self._units.clear()
        self._fragment = None

    def _finish(self) -> bytes | None:
        if self._fragment is not None or not self._units:
            self._reset()
            return None
        result = b"".join(b"\x00\x00\x00\x01" + unit for unit in self._units)
        self._reset()
        return result


def parse_rtp_packet(data: bytes) -> NativeRtpPacket:
    """Parse one RFC 3550 packet without altering its encoded payload."""
    if not isinstance(data, bytes) or len(data) < 12:
        raise NativeSimpleRtpError("RTP packet is shorter than its fixed header")
    first, second = data[0], data[1]
    if first >> 6 != 2:
        raise NativeSimpleRtpError("RTP packet has an unsupported version")
    csrc_count = first & 0x0F
    offset = 12 + csrc_count * 4
    if len(data) < offset:
        raise NativeSimpleRtpError("RTP CSRC list is truncated")
    if first & 0x10:
        if len(data) < offset + 4:
            raise NativeSimpleRtpError("RTP extension header is truncated")
        words = struct.unpack_from(">H", data, offset + 2)[0]
        offset += 4 + words * 4
        if len(data) < offset:
            raise NativeSimpleRtpError("RTP extension payload is truncated")
    end = len(data)
    if first & 0x20:
        padding = data[-1]
        if not padding or padding > end - offset:
            raise NativeSimpleRtpError("RTP padding is invalid")
        end -= padding
    return NativeRtpPacket(
        payload_type=second & 0x7F,
        marker=bool(second & 0x80),
        sequence=struct.unpack_from(">H", data, 2)[0],
        timestamp=struct.unpack_from(">I", data, 4)[0],
        ssrc=struct.unpack_from(">I", data, 8)[0],
        payload=data[offset:end],
    )
