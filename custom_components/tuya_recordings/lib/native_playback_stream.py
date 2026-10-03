"""Incremental parser for the Smart Life native SD-playback byte stream."""

from __future__ import annotations

import struct
from dataclasses import dataclass

PLAYBACK_HEADER_BYTES = 32
PLAYBACK_CODEC_INFO_BYTES = 60
PLAYBACK_MAX_PAYLOAD_BYTES = 1024 * 1024
PLAYBACK_AUDIO_FRAME_TYPE = 3

_SAMPLE_RATES = (
    8000,
    11025,
    12000,
    16000,
    22050,
    24000,
    32000,
    44100,
    48000,
    96000,
)
_CHANNEL_COUNTS = (1, 2)
_BIT_WIDTHS = (8, 16)


class NativePlaybackStreamError(ValueError):
    """The native playback stream contained an invalid frame."""


@dataclass(frozen=True, slots=True)
class NativePlaybackCodecInfo:
    """The six indexed fields at the start of the APK's 60-byte info block."""

    codec: int
    sample_rate_index: int
    channels_index: int
    bit_width_index: int
    field_4: int
    field_5: int
    raw: bytes

    @property
    def sample_rate(self) -> int:
        return _lookup(self.sample_rate_index, _SAMPLE_RATES, 8000)

    @property
    def channels(self) -> int:
        return _lookup(self.channels_index, _CHANNEL_COUNTS, 0)

    @property
    def bit_width(self) -> int:
        return _lookup(self.bit_width_index, _BIT_WIDTHS, 0)


@dataclass(frozen=True, slots=True)
class NativePlaybackStreamFrame:
    """One complete encoded video or audio frame from the reliable stream."""

    frame_type: int
    has_codec_info: bool
    payload_length: int
    field_8: int
    field_12: int
    field_16: int
    field_20: int
    field_24: int
    field_28: int
    codec_info: NativePlaybackCodecInfo | None
    payload: bytes

    @property
    def is_audio(self) -> bool:
        return self.frame_type == PLAYBACK_AUDIO_FRAME_TYPE


class NativePlaybackStreamParser:
    """Reassemble the APK's exact-read protocol from arbitrary byte chunks."""

    def __init__(self, *, max_payload_bytes: int = PLAYBACK_MAX_PAYLOAD_BYTES) -> None:
        if not 1 <= max_payload_bytes <= 8 * 1024 * 1024:
            raise NativePlaybackStreamError(
                "Native playback payload limit must be between 1 byte and 8 MiB"
            )
        self._max_payload_bytes = max_payload_bytes
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> list[NativePlaybackStreamFrame]:
        if not isinstance(data, bytes) or not data:
            raise NativePlaybackStreamError("Native playback input must be non-empty bytes")
        self._buffer.extend(data)
        frames: list[NativePlaybackStreamFrame] = []
        while len(self._buffer) >= PLAYBACK_HEADER_BYTES:
            header = struct.unpack_from(">HH7I", self._buffer)
            frame_type, info_flag, payload_length, *fields = header
            if payload_length > self._max_payload_bytes:
                self._buffer.clear()
                raise NativePlaybackStreamError(
                    f"Native playback payload exceeds {self._max_payload_bytes} bytes"
                )
            info_size = PLAYBACK_CODEC_INFO_BYTES if info_flag else 0
            total = PLAYBACK_HEADER_BYTES + info_size + payload_length
            if len(self._buffer) < total:
                break
            offset = PLAYBACK_HEADER_BYTES
            codec_info = None
            if info_size:
                raw_info = bytes(self._buffer[offset : offset + info_size])
                codec_info = NativePlaybackCodecInfo(
                    *struct.unpack_from(">6H", raw_info), raw=raw_info
                )
                offset += info_size
            payload = bytes(self._buffer[offset:total])
            del self._buffer[:total]
            frames.append(
                NativePlaybackStreamFrame(
                    frame_type=frame_type,
                    has_codec_info=bool(info_flag),
                    payload_length=payload_length,
                    field_8=fields[0],
                    field_12=fields[1],
                    field_16=fields[2],
                    field_20=fields[3],
                    field_24=fields[4],
                    field_28=fields[5],
                    codec_info=codec_info,
                    payload=payload,
                )
            )
        return frames

    def finish(self) -> None:
        """Reject a stream that ended partway through a frame."""
        if self._buffer:
            remaining = len(self._buffer)
            self._buffer.clear()
            raise NativePlaybackStreamError(
                f"Native playback stream ended with {remaining} incomplete bytes"
            )


def _lookup(index: int, values: tuple[int, ...], default: int) -> int:
    return values[index] if 0 <= index < len(values) else default
