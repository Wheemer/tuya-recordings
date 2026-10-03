"""ThingCameraSimple binary SD-card commands recovered from Smart Life."""

from __future__ import annotations

import base64
import json
import struct
from dataclasses import dataclass
from datetime import date
from typing import Any

from .native_v4 import NativeV4CatalogPage, NativeV4Recording

_MAGIC = 0x12345678
_HEADER = struct.Struct("<IIIHHI")
_CATALOG = struct.Struct("<4i")
_CATALOG_V3 = struct.Struct("<5i")
_EVENT_CATALOG_V1 = struct.Struct("<64x5i")
_EVENT_CATALOG_V2 = struct.Struct("<5i")
_CATALOG_ENTRY = struct.Struct("<III")
_CATALOG_V2_ENTRY_SIZE = 0x40
_CATALOG_V3_ENTRY_SIZE = 0x48
_EVENT_CATALOG_PAGE_OFFSET = 0x50
_EVENT_CATALOG_TOTAL_OFFSET = 0x54
_EVENT_CATALOG_PAGE_SIZE_OFFSET = 0x58
_EVENT_CATALOG_SIMPLE_COUNT_OFFSET = 0x5C
_EVENT_CATALOG_SIMPLE_ITEMS_OFFSET = 0x60
_EVENT_CATALOG_SIMPLE_ENTRY = struct.Struct("<HHII")
_EVENT_CATALOG_EXT_COUNT_OFFSET = 0x60
_EVENT_CATALOG_EXT_ITEMS_OFFSET = 0x64
_EVENT_CATALOG_EXT_ENTRY = struct.Struct("<IIHH20s")
_PLAYBACK = struct.Struct("<5i")
_AUDIO = struct.Struct("<2i")
_THUMBNAIL_REQUEST_V2 = struct.Struct("<4i32x")
_THUMBNAIL_METADATA_OFFSET = 44
_THUMBNAIL_REPLY_V2_HEADER_SIZE = 56
_THUMBNAIL_IMAGE_TYPE_JPEG = 3
_MAX_PAYLOAD = 1024 * 1024
_MAX_CATALOG_ITEMS = 24 * 60 * 60
_CATALOG_V1_COUNT_OFFSETS = (0x10, 0x50)


class NativeSimpleError(ValueError):
    """A ThingCameraSimple packet is malformed or unsupported."""


@dataclass(frozen=True, slots=True)
class NativeSimpleCommand:
    """One decrypted ThingCameraSimple control-channel command."""

    request_id: int
    control: int
    high_command: int
    low_command: int
    payload: bytes


class NativeSimpleCommandDecoder:
    """Incrementally decode binary camera commands from AES records."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> tuple[NativeSimpleCommand, ...]:
        if not isinstance(data, bytes):
            raise NativeSimpleError("Native control data must be bytes")
        self._buffer.extend(data)
        commands: list[NativeSimpleCommand] = []
        while len(self._buffer) >= _HEADER.size:
            magic, request_id, control, high, low, length = _HEADER.unpack_from(
                self._buffer
            )
            if magic != _MAGIC:
                self._buffer.clear()
                raise NativeSimpleError("Native control command has invalid magic")
            if length > _MAX_PAYLOAD:
                self._buffer.clear()
                raise NativeSimpleError("Native control payload exceeds size limit")
            total = _HEADER.size + length
            if len(self._buffer) < total:
                break
            payload = bytes(self._buffer[_HEADER.size:total])
            del self._buffer[:total]
            commands.append(
                NativeSimpleCommand(request_id, control, high, low, payload)
            )
        return tuple(commands)


def playback_stream_id(*, task_id: int, request_id: int) -> int:
    """Return the stream ID assembled by ThingCameraSimple::StartPlayBack."""
    _uint16("task_id", task_id)
    _uint16("request_id", request_id)
    if task_id == 0 or request_id == 0:
        raise NativeSimpleError("Playback task and request IDs must be non-zero")
    return (task_id << 16) | request_id


def encode_catalog_request(
    *,
    request_id: int,
    channel: int,
    day: date,
    version: int = 0,
    encrypted: int = 0,
) -> bytes:
    """Encode the APK's GetRecordFragmentsByDay V1, V2, or V3 request."""
    _uint32("request_id", request_id)
    _non_negative("channel", channel)
    if not isinstance(day, date):
        raise NativeSimpleError("Catalog day must be a date")
    if version not in (0, 1, 2):
        raise NativeSimpleError("Catalog version is invalid")
    if encrypted not in (0, 1):
        raise NativeSimpleError("Catalog encryption flag is invalid")
    low_command = (1, 4, 7)[version]
    payload = (
        _CATALOG_V3.pack(channel, day.year, day.month, day.day, encrypted)
        if version == 2
        else _CATALOG.pack(channel, day.year, day.month, day.day)
    )
    return _command(request_id, 0, 3, low_command, payload)


def encode_event_catalog_request(
    *,
    request_id: int,
    channel: int,
    day: date,
    page: int = 0,
    v2: bool = False,
    encrypted: int = 1,
) -> bytes:
    """Encode the APK's V1 (3/3) or V2 (3/9) event catalog request."""
    _uint32("request_id", request_id)
    _non_negative("channel", channel)
    if not isinstance(day, date):
        raise NativeSimpleError("Event catalog day must be a date")
    _non_negative("page", page)
    if encrypted not in (0, 1):
        raise NativeSimpleError("Event catalog encryption flag is invalid")
    if not isinstance(v2, bool):
        raise NativeSimpleError("Event catalog version flag is invalid")
    if not v2:
        return _command(
            request_id,
            0,
            3,
            3,
            _EVENT_CATALOG_V1.pack(
                channel, day.year, day.month, day.day, page
            ),
        )
    return _command(
        request_id,
        0,
        3,
        9,
        _EVENT_CATALOG_V2.pack(
            channel, day.year, day.month, day.day, encrypted
        ),
    )


def encode_playback_start(
    *,
    stream_id: int,
    channel: int,
    start: int,
    end: int,
    play_time: int,
    encrypted: bool = False,
) -> bytes:
    """Encode the APK's StartPlayBack or encrypted StartPlayBackV2 command."""
    _uint32("stream_id", stream_id)
    for name, value in (("channel", channel), ("start", start), ("end", end), ("play_time", play_time)):
        _non_negative(name, value)
    if end <= start or not start <= play_time < end:
        raise NativeSimpleError("Playback bounds are invalid")
    return _command(
        stream_id,
        0,
        100 if encrypted else 7,
        20 if encrypted else 0,
        _PLAYBACK.pack(channel, 0, start, end, play_time),
    )


def encode_playback_start_fragments(
    *,
    stream_id: int,
    channel: int,
    play_time: int,
    fragments_json: str,
) -> bytes:
    """Encode the APK's play-mode StartPlayBack command (high 7, low 21)."""
    _uint32("stream_id", stream_id)
    _non_negative("channel", channel)
    _non_negative("play_time", play_time)
    try:
        decoded = json.loads(fragments_json)
    except (TypeError, ValueError) as err:
        raise NativeSimpleError("Playback fragments are invalid") from err
    raw_fragments = decoded.get("fragments") if isinstance(decoded, dict) else None
    if not isinstance(raw_fragments, list) or not raw_fragments:
        raise NativeSimpleError("Playback fragments are invalid")
    fragments: list[tuple[int, int]] = []
    for raw in raw_fragments:
        if not isinstance(raw, dict):
            raise NativeSimpleError("Playback fragments are invalid")
        start, end = raw.get("start"), raw.get("end")
        if type(start) is not int or type(end) is not int or start < 0 or end <= start:
            raise NativeSimpleError("Playback fragments are invalid")
        fragments.append((start, end))
    if not any(start <= play_time < end for start, end in fragments):
        raise NativeSimpleError("Playback position is outside the fragments")
    payload = bytearray(_PLAYBACK.pack(channel, 21, 0, play_time, len(fragments)))
    for start, end in fragments:
        payload.extend(struct.pack("<2i", start, end))
    return _command(stream_id, 0, 7, 21, bytes(payload))


def encode_audio_open(*, stream_id: int, channel: int) -> bytes:
    """Encode the APK's playback audio-open operation (high 7, low 4)."""
    _uint32("stream_id", stream_id)
    _non_negative("channel", channel)
    return _command(stream_id, 0, 7, 4, _AUDIO.pack(channel, 4))


def encode_playback_pause(*, stream_id: int, channel: int) -> bytes:
    """Encode the APK's PausePlayBack operation (high 7, low 1)."""
    _uint32("stream_id", stream_id)
    _non_negative("channel", channel)
    return _command(
        stream_id,
        0,
        7,
        1,
        _PLAYBACK.pack(channel, 1, 0, 0, 0),
    )


def encode_playback_resume(*, stream_id: int, channel: int) -> bytes:
    """Encode the APK's ResumePlayBack operation (high 7, low 2)."""
    _uint32("stream_id", stream_id)
    _non_negative("channel", channel)
    return _command(
        stream_id,
        0,
        7,
        2,
        _PLAYBACK.pack(channel, 2, 0, 0, 0),
    )


def encode_preview_stop(*, stream_id: int, channel: int) -> tuple[bytes, bytes]:
    """Encode the APK's ordered StopPreview operations (6/3 then 6/5)."""
    _uint32("stream_id", stream_id)
    _non_negative("channel", channel)
    return (
        _command(stream_id, 0, 6, 3, _AUDIO.pack(channel, 3)),
        _command(stream_id, 0, 6, 5, _AUDIO.pack(channel, 5)),
    )


def encode_thumbnail_request(
    *,
    request_id: int,
    channel: int,
    start: int,
    end: int,
) -> bytes:
    """Encode Smart Life's direct recording-JPEG request."""
    _uint32("request_id", request_id)
    for name, value in (
        ("channel", channel),
        ("start", start),
        ("end", end),
    ):
        _non_negative(name, value)
    if end <= start:
        raise NativeSimpleError("Thumbnail bounds are invalid")
    return _command(
        request_id,
        0,
        3,
        8,
        _THUMBNAIL_REQUEST_V2.pack(channel, start, end, 0),
    )


def encode_playback_stop(*, stream_id: int, channel: int) -> tuple[bytes, bytes]:
    """Encode Smart Life's ordered StopPlayBack operations (7/3 then 7/5)."""
    _uint32("stream_id", stream_id)
    _non_negative("channel", channel)
    return (
        _command(stream_id, 0, 7, 3, _PLAYBACK.pack(channel, 3, 0, 0, 0)),
        _command(stream_id, 0, 7, 5, _AUDIO.pack(channel, 5)),
    )


def decode_catalog_reply(
    command: NativeSimpleCommand, *, request_id: int, version: int = 0
) -> NativeV4CatalogPage:
    """Decode GetRecordFragmentsByDay as emitted by the Smart Life SDK."""
    if version not in (0, 1, 2):
        raise NativeSimpleError("Catalog version is invalid")
    if (
        command.request_id != request_id
        or command.control != 1
    ):
        raise NativeSimpleError("Catalog response does not match the request")
    payload = command.payload.rstrip(b"\0")
    if payload.startswith((b"{", b"[")):
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise NativeSimpleError("Catalog response is not valid JSON") from err
        return _catalog_page_from_json(value, request_id=request_id)
    if version == 2:
        return _catalog_page_from_v3_binary(command.payload, request_id=request_id)
    if version == 1:
        return _catalog_page_from_v2_binary(command.payload, request_id=request_id)
    return _catalog_page_from_v1_binary(command.payload, request_id=request_id)


def decode_event_catalog_reply(
    command: NativeSimpleCommand, *, request_id: int, v2: bool = False
) -> NativeV4CatalogPage:
    """Decode the APK's legacy binary or V2 JSON event catalog."""
    if command.request_id != request_id or command.control != 1:
        raise NativeSimpleError("Event catalog response does not match the request")
    payload = command.payload.rstrip(b"\0")
    if payload.startswith((b"{", b"[")):
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise NativeSimpleError("Event catalog response is not valid JSON") from err
        return _catalog_page_from_json(value, request_id=request_id)
    if v2:
        raise NativeSimpleError(
            "V2 event catalog response is not JSON "
            f"(bytes={len(command.payload)}, prefix={command.payload[:16].hex()})"
        )
    return _event_catalog_page_from_v1_binary(
        command.payload, request_id=request_id
    )


def decode_thumbnail_reply(
    command: NativeSimpleCommand, *, request_id: int
) -> bytes:
    """Decode Smart Life's direct-JPEG callback for command 3/8."""
    _validate_thumbnail_response(command, request_id=request_id)
    payload = command.payload
    header_size = _THUMBNAIL_REPLY_V2_HEADER_SIZE
    if len(payload) < header_size:
        raise NativeSimpleError("Thumbnail response is truncated")
    image_type, encrypted, length = struct.unpack_from(
        "<III", payload, _THUMBNAIL_METADATA_OFFSET
    )
    if image_type != _THUMBNAIL_IMAGE_TYPE_JPEG:
        raise NativeSimpleError("Thumbnail response is not a JPEG")
    if encrypted != 0:
        raise NativeSimpleError("Encrypted thumbnail responses are unsupported")
    return _thumbnail_image(payload, header_size=header_size, length=length)


def _validate_thumbnail_response(
    command: NativeSimpleCommand, *, request_id: int
) -> None:
    # ThingNetProtocolManager correlates asynchronous callbacks by request ID
    # and the response control flag. The APK callback does not require the
    # camera to echo the outgoing high/low command IDs.
    if command.request_id != request_id or command.control != 1:
        raise NativeSimpleError("Thumbnail response does not match the request")


def _thumbnail_image(payload: bytes, *, header_size: int, length: int) -> bytes:
    image = payload[header_size:]
    if length != len(image):
        raise NativeSimpleError("Thumbnail response length is invalid")
    if len(image) < 4 or not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9"):
        raise NativeSimpleError("Thumbnail response has invalid JPEG markers")
    return image


def _catalog_page_from_json(value: Any, *, request_id: int) -> NativeV4CatalogPage:
    """Convert the SDK's JSON response variants into a catalog page."""
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, dict):
        raw_items = value.get("items")
        if not isinstance(raw_items, list):
            raw_items = value.get("data")
    else:
        raw_items = None
    if not isinstance(raw_items, list):
        raise NativeSimpleError("Catalog response has no recording list")
    items = tuple(_recording(item) for item in raw_items)
    return NativeV4CatalogPage(request_id, 0, "", 1, len(items), 1, items)


def _catalog_page_from_v1_binary(
    payload: bytes, *, request_id: int
) -> NativeV4CatalogPage:
    """Decode ``ThingRecordFragmentCallBack::onResponse0`` wire records.

    The native SDK reads the item count at offset 0x10 (or 0x50 for its
    extended envelope), then consumes 12-byte records containing packed
    video/event type, start time, and end time.
    """
    candidates: list[tuple[NativeV4Recording, ...]] = []
    for count_offset in _CATALOG_V1_COUNT_OFFSETS:
        records_offset = count_offset + 4
        if len(payload) < records_offset:
            continue
        (count,) = struct.unpack_from("<I", payload, count_offset)
        if count > _MAX_CATALOG_ITEMS:
            continue
        required = records_offset + count * _CATALOG_ENTRY.size
        if required > len(payload):
            continue

        items: list[NativeV4Recording] = []
        try:
            for offset in range(records_offset, required, _CATALOG_ENTRY.size):
                packed_type, start_time, end_time = _CATALOG_ENTRY.unpack_from(
                    payload, offset
                )
                items.append(
                    NativeV4Recording(
                        start_time=start_time,
                        end_time=end_time,
                        event_type=(packed_type >> 16) & 0xFFFF,
                        video_type=packed_type & 0xFFFF,
                        encrypt=0,
                        uuid="",
                        encrypt_md5="",
                    ).validate()
                )
        except (ValueError, NativeSimpleError):
            continue
        candidates.append(tuple(items))

    if not candidates:
        raise NativeSimpleError("Catalog response has an invalid V1 binary layout")
    items = max(candidates, key=len)
    return NativeV4CatalogPage(request_id, 0, "", 1, len(items), 1, items)


def _event_catalog_page_from_v1_binary(
    payload: bytes, *, request_id: int
) -> NativeV4CatalogPage:
    """Decode ``GetRecordEventFragmentsByDay`` (3/3) callback input.

    Smart Life's native callback accepts two legacy camera layouts. Both have
    page metadata at 0x50. The extended layout includes a 20-byte ``pic_name``
    beside each event; the compact layout contains the same event boundaries
    without that name. ``DownloadPlayBackImage`` uses those boundaries.
    """
    if len(payload) < _EVENT_CATALOG_EXT_ITEMS_OFFSET:
        raise NativeSimpleError(
            "Event catalog response has an invalid legacy binary layout"
        )
    page_id, total_count, page_size = struct.unpack_from(
        "<III", payload, _EVENT_CATALOG_PAGE_OFFSET
    )
    candidates: list[tuple[NativeV4Recording, ...]] = []

    (extended_count,) = struct.unpack_from(
        "<I", payload, _EVENT_CATALOG_EXT_COUNT_OFFSET
    )
    extended_end = (
        _EVENT_CATALOG_EXT_ITEMS_OFFSET
        + extended_count * _EVENT_CATALOG_EXT_ENTRY.size
    )
    if extended_count <= _MAX_CATALOG_ITEMS and extended_end <= len(payload):
        extended_items: list[NativeV4Recording] = []
        try:
            for index in range(extended_count):
                offset = (
                    _EVENT_CATALOG_EXT_ITEMS_OFFSET
                    + index * _EVENT_CATALOG_EXT_ENTRY.size
                )
                start_time, end_time, video_type, event_type, pic_name = (
                    _EVENT_CATALOG_EXT_ENTRY.unpack_from(payload, offset)
                )
                # The APK exposes pic_name in JSON, but addresses the JPEG by
                # this event's start/end times. Validate the fixed string so a
                # compact reply cannot be mistaken for the extended layout.
                raw_name = pic_name.split(b"\0", 1)[0]
                raw_name.decode("utf-8")
                extended_items.append(
                    NativeV4Recording(
                        start_time=start_time,
                        end_time=end_time,
                        event_type=event_type,
                        video_type=video_type,
                    ).validate()
                )
        except (UnicodeDecodeError, ValueError):
            pass
        else:
            candidates.append(tuple(extended_items))

    (compact_count,) = struct.unpack_from(
        "<I", payload, _EVENT_CATALOG_SIMPLE_COUNT_OFFSET
    )
    compact_end = (
        _EVENT_CATALOG_SIMPLE_ITEMS_OFFSET
        + compact_count * _EVENT_CATALOG_SIMPLE_ENTRY.size
    )
    if compact_count <= _MAX_CATALOG_ITEMS and compact_end <= len(payload):
        compact_items: list[NativeV4Recording] = []
        try:
            for index in range(compact_count):
                offset = (
                    _EVENT_CATALOG_SIMPLE_ITEMS_OFFSET
                    + index * _EVENT_CATALOG_SIMPLE_ENTRY.size
                )
                video_type, event_type, start_time, end_time = (
                    _EVENT_CATALOG_SIMPLE_ENTRY.unpack_from(payload, offset)
                )
                compact_items.append(
                    NativeV4Recording(
                        start_time=start_time,
                        end_time=end_time,
                        event_type=event_type,
                        video_type=video_type,
                    ).validate()
                )
        except ValueError:
            pass
        else:
            candidates.append(tuple(compact_items))

    if not candidates:
        raise NativeSimpleError(
            "Event catalog response has an invalid legacy binary layout"
        )
    items = max(candidates, key=len)
    effective_page_size = page_size or len(items) or 1
    total_pages = max(1, (total_count + effective_page_size - 1) // effective_page_size)
    return NativeV4CatalogPage(
        request_id,
        0,
        "",
        total_pages,
        total_count,
        page_id + 1,
        items,
    )


def _catalog_page_from_v2_binary(
    payload: bytes, *, request_id: int
) -> NativeV4CatalogPage:
    """Decode ``ThingRecordFragmentCallBack::onResponse1`` wire records."""
    if len(payload) < 0x14:
        raise NativeSimpleError("Catalog response has an invalid V2 binary layout")
    (count,) = struct.unpack_from("<I", payload, 0x10)
    required = 0x14 + count * _CATALOG_V2_ENTRY_SIZE
    if count > _MAX_CATALOG_ITEMS or required > len(payload):
        raise NativeSimpleError("Catalog response has an invalid V2 binary layout")
    items = tuple(
        _encrypted_catalog_record(payload, 0x14 + index * _CATALOG_V2_ENTRY_SIZE, v3=False)
        for index in range(count)
    )
    return NativeV4CatalogPage(request_id, 0, "", 1, len(items), 1, items)


def _catalog_page_from_v3_binary(
    payload: bytes, *, request_id: int
) -> NativeV4CatalogPage:
    """Decode ``ThingRecordFragmentCallBack::onResponse2`` wire records."""
    if len(payload) < 0x14:
        raise NativeSimpleError("Catalog response has an invalid V3 binary layout")
    (count,) = struct.unpack_from("<I", payload, 0x10)
    if count > _MAX_CATALOG_ITEMS:
        raise NativeSimpleError("Catalog response has an invalid V3 binary layout")
    offset = 0x14
    items: list[NativeV4Recording] = []
    for _ in range(count):
        if offset + _CATALOG_V3_ENTRY_SIZE > len(payload):
            raise NativeSimpleError("Catalog response has an invalid V3 binary layout")
        (event_count,) = struct.unpack_from("<I", payload, offset + 0x44)
        if event_count > _MAX_CATALOG_ITEMS:
            raise NativeSimpleError("Catalog response has an invalid V3 binary layout")
        record_end = offset + _CATALOG_V3_ENTRY_SIZE + event_count * 4
        if record_end > len(payload):
            raise NativeSimpleError("Catalog response has an invalid V3 binary layout")
        event_types = struct.unpack_from(
            f"<{event_count}I", payload, offset + _CATALOG_V3_ENTRY_SIZE
        ) if event_count else ()
        item = _encrypted_catalog_record(
            payload, offset, v3=True, event_types=event_types
        )
        offset = record_end
        items.append(item)
    return NativeV4CatalogPage(request_id, 0, "", 1, len(items), 1, tuple(items))


def _encrypted_catalog_record(
    payload: bytes,
    offset: int,
    *,
    v3: bool,
    event_types: tuple[int, ...] = (),
) -> NativeV4Recording:
    """Decode one V2/V3 fragment structure traced from the native callback."""
    if v3:
        video_type, event_type = struct.unpack_from("<II", payload, offset)
        uuid_offset = offset + 8
        times_offset = offset + 0x28
    else:
        video_type, event_type = struct.unpack_from("<HH", payload, offset)
        uuid_offset = offset + 4
        times_offset = offset + 0x24
    uuid_raw = payload[uuid_offset : uuid_offset + 32].split(b"\0", 1)[0]
    try:
        uuid = uuid_raw.decode("ascii")
    except UnicodeDecodeError as err:
        raise NativeSimpleError("Catalog recording UUID is invalid") from err
    start_time, end_time, encrypted = struct.unpack_from("<III", payload, times_offset)
    digest = payload[times_offset + 12 : times_offset + 28]
    encrypt_md5 = (
        base64.b64encode(digest.hex().encode("ascii")).decode("ascii")
        if encrypted
        else ""
    )
    return NativeV4Recording(
        start_time=start_time,
        end_time=end_time,
        event_type=event_type,
        event_types=event_types,
        video_type=video_type,
        encrypt=encrypted,
        uuid=uuid,
        encrypt_md5=encrypt_md5,
    ).validate()


def _recording(value: Any) -> NativeV4Recording:
    if not isinstance(value, dict):
        raise NativeSimpleError("Catalog recording is not an object")

    def integer(*names: str, default: int | None = None) -> int:
        raw = next((value[name] for name in names if name in value), default)
        if type(raw) is not int or raw < 0:
            raise NativeSimpleError(f"Catalog field {names[0]} is invalid")
        return raw

    def text(*names: str) -> str:
        raw = next((value[name] for name in names if name in value), "")
        if not isinstance(raw, str) or "\0" in raw:
            raise NativeSimpleError(f"Catalog field {names[0]} is invalid")
        return raw

    raw_event_types = next(
        (
            value[name]
            for name in ("eventTypeArr", "event_types", "event_type_arr")
            if name in value
        ),
        [],
    )
    if not isinstance(raw_event_types, list) or any(
        type(item) is not int or item < 0 for item in raw_event_types
    ):
        raise NativeSimpleError("Catalog field eventTypeArr is invalid")

    return NativeV4Recording(
        start_time=integer("startTime", "start_time"),
        end_time=integer("endTime", "end_time"),
        event_type=integer("eventType", "event_type", default=0),
        event_types=tuple(raw_event_types),
        video_type=integer("videoType", "video_type", default=0),
        encrypt=integer("encrypt", default=0),
        uuid=text("uuid"),
        encrypt_md5=text("encryptMD5", "encrypt_md5"),
    ).validate()


def _command(
    request_id: int, control: int, high: int, low: int, payload: bytes
) -> bytes:
    return _HEADER.pack(_MAGIC, request_id, control, high, low, len(payload)) + payload


def _uint16(name: str, value: int) -> None:
    if type(value) is not int or not 0 <= value <= 0xFFFF:
        raise NativeSimpleError(f"{name} must fit an unsigned 16-bit integer")


def _uint32(name: str, value: int) -> None:
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
        raise NativeSimpleError(f"{name} must fit an unsigned 32-bit integer")


def _non_negative(name: str, value: int) -> None:
    if type(value) is not int or not 0 <= value <= 0x7FFFFFFF:
        raise NativeSimpleError(f"{name} is outside the native integer range")
