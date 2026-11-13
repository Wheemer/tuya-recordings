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

    @property
    def audio_format_indices(self) -> tuple[int, int, int]:
        """Read HandleReadAudioStream's extension fields, not RTP guesses."""
        if not self.extension:
            return 0, 0, 1  # SDK's 8 kHz, mono, 16-bit defaults.
        return struct.unpack_from("<3H", self.extension, 2)

    @property
    def camera_timestamp(self) -> int:
        """Shared camera millisecond clock passed through the SDK callbacks."""
        return struct.unpack_from("<Q", self.header, 8)[0]


# ThingRTPUnpackerDispatcher::CreateRtpUnpacker @0x1b8394.
# These are SDK payload assignments, not universal RTP dynamic assignments.
SIMPLE_AUDIO_CODECS = {0: 133, 8: 132, 97: 4, 99: 0xFFFE}


def simple_audio_payload(packet: NativeRtpPacket) -> bytes:
    """Strip the SDK AAC AU header while retaining encoded audio bytes."""
    if packet.payload_type != 97:
        return packet.payload
    if len(packet.payload) < 4 or packet.payload[:2] != b"\x00\x10":
        raise NativeSimpleRtpError("Native AAC RTP AU header is invalid")
    size = int.from_bytes(packet.payload[2:4], "big") >> 3
    if size != len(packet.payload) - 4:
        raise NativeSimpleRtpError("Native AAC RTP AU size is invalid")
    return packet.payload[4:]


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
        self._frame_bytes = 0
        self._presentation_timestamp: int | None = None
        self._presentation_scale: int | None = None
        self.completed_timestamps: tuple[int, ...] = ()
        self.completed_timestamp_scales: tuple[int, ...] = ()

    def push(self, packet: NativeRtpPacket, *, presentation_timestamp: int | None = None, timestamp_scale: int = 90000) -> tuple[bytes, ...]:
        if not packet.payload:
            raise NativeSimpleRtpError("H.264 RTP payload is empty")
        completed: list[bytes] = []
        times: list[int] = []
        scales: list[int] = []
        self.completed_timestamps = ()
        self.completed_timestamp_scales = ()
        if self._timestamp is not None and packet.timestamp != self._timestamp:
            previous_time = self._presentation_timestamp
            previous_scale = self._presentation_scale
            previous = self._finish()
            if previous is not None:
                completed.append(previous)
                assert previous_time is not None
                times.append(previous_time)
                assert previous_scale is not None
                scales.append(previous_scale)
        if self._last_sequence is not None and packet.sequence != (
            self._last_sequence + 1
        ) & 0xFFFF:
            self._reset()
        if self._timestamp is None:
            self._presentation_timestamp = packet.timestamp if presentation_timestamp is None else presentation_timestamp
            self._presentation_scale = timestamp_scale
        self._timestamp = packet.timestamp
        self._last_sequence = packet.sequence
        self._frame_bytes += len(packet.payload)
        if self._frame_bytes > SIMPLE_MEDIA_MAX_PAYLOAD_BYTES:
            self._reset()
            raise NativeSimpleRtpError("H.264 access unit exceeds the native media limit")
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
            self.completed_timestamps = tuple(times)
            self.completed_timestamp_scales = tuple(scales)
            return tuple(completed)
        current_time = self._presentation_timestamp
        current_scale = self._presentation_scale
        current = self._finish()
        if current is None:
            raise NativeSimpleRtpError("H.264 marker ended an incomplete access unit")
        completed.append(current)
        assert current_time is not None
        times.append(current_time)
        assert current_scale is not None
        scales.append(current_scale)
        self.completed_timestamps = tuple(times)
        self.completed_timestamp_scales = tuple(scales)
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
        self._frame_bytes = 0
        self._presentation_timestamp = None
        self._presentation_scale = None

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
    if first == 0xB8:
        first = 0x80
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
