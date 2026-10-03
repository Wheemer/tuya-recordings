"""
The framing of the TCP relay that carries the media.

Everything on the connection is length-framed as
``magic(1) | flag(1)=00 | len(2 BE) | payload[len]``. ``f4`` carries the JSON
challenge-response handshake, ``f5`` is the keepalive and ``f6`` wraps the KCP
media segments. Relay TLVs are padded with zeroes to a four-byte boundary; the
outer length includes that padding while the inner attribute length does not.
"""

from __future__ import annotations

from ..crypto import aes_cbc_decrypt, aes_cbc_encrypt, hmac_sha1, hmac_sha256
from ..exceptions import TuyaIpcP2pProtocolError
from ..json_types import JsonObject, parse_json_object

FRAME_HANDSHAKE = 0xF4
FRAME_KEEPALIVE = 0xF5
FRAME_MEDIA = 0xF6

# Security level 3 initializes this build's mbedTLS digest type 5 (SHA-1).
# Every f6 frame carries that HMAC after the KCP segment, keyed by the session
# media key and covering the segment alone. A device silently drops a missing
# or wrong tag.
MEDIA_TAG_LENGTH = 20

_MAC_LENGTH = 32
_TLV_HEADER_LENGTH = 4
_TLV_ALIGNMENT = 4
_HANDSHAKE_TLV_START = 8
_ENCRYPTED_SECTION_MARKER = 0x07
_TLV_INITIALIZATION_VECTOR = 2


def media_tag(media_key: bytes, kcp_segment: bytes) -> bytes:
    """Return the per-frame tag of one KCP segment."""
    return hmac_sha1(media_key, kcp_segment)


def relay_frame(magic: int, body: bytes) -> bytes:
    """Wrap a payload in the relay's outer framing."""
    return bytes((magic, 0x00)) + len(body).to_bytes(2, "big") + body


def keepalive_frame() -> bytes:
    """
    Return the keepalive frame.

    It has to go out about once a second in both directions; the relay drops a
    session that goes silent.
    """
    return relay_frame(FRAME_KEEPALIVE, b"")


def media_frame(media_key: bytes, kcp_segment: bytes) -> bytes:
    """Wrap one KCP segment in a tagged f6 frame."""
    tagged_length = len(kcp_segment) + MEDIA_TAG_LENGTH
    inner = bytearray(
        b"\x00\x07"
        + tagged_length.to_bytes(2, "big")
        + kcp_segment
        + media_tag(media_key, kcp_segment)
    )
    inner.extend(bytes((-tagged_length) % _TLV_ALIGNMENT))
    return relay_frame(FRAME_MEDIA, bytes(inner))


def unwrap_media_frame(media_key: bytes, payload: bytes) -> bytes:
    """Verify an inbound f6 frame's tag and return the KCP segment it carries."""
    if len(payload) < _TLV_HEADER_LENGTH + MEDIA_TAG_LENGTH:
        raise TuyaIpcP2pProtocolError("Failed to read media frame: payload too short")
    inner = int.from_bytes(payload[2:4], "big")
    if inner < MEDIA_TAG_LENGTH or _TLV_HEADER_LENGTH + inner > len(payload):
        raise TuyaIpcP2pProtocolError(f"Failed to read media frame: bad inner length {inner}")
    end = _TLV_HEADER_LENGTH + inner
    segment = payload[_TLV_HEADER_LENGTH : end - MEDIA_TAG_LENGTH]
    if payload[end - MEDIA_TAG_LENGTH : end] != media_tag(media_key, segment):
        raise TuyaIpcP2pProtocolError("Failed to read media frame: tag mismatch")
    return segment


def _type_length_value(value_type: int, value: bytes) -> bytes:
    """Build one TLV of the handshake frame."""
    return value_type.to_bytes(2, "big") + len(value).to_bytes(2, "big") + value


def assemble_handshake_frame(
    message_index: int,
    key: bytes,
    iv: bytes,
    session_id: bytes,
    username: bytes,
    json_message: bytes,
) -> bytes:
    """
    Build one f4 frame.

    The trailing MAC is keyed by ``credential[:16]`` and covers the whole frame
    up to and including the ``00 08 00 20`` TLV header, excluding only the
    digest itself.
    """
    ciphertext = aes_cbc_encrypt(key, iv, json_message)
    body = b"".join(
        (
            b"\x00\x01\x00\x02",
            message_index.to_bytes(2, "big") + b"\x00\x00",
            _type_length_value(2, iv),
            _type_length_value(3, session_id),
            _type_length_value(4, username),
            b"\x00\x00\x00\x00",
            bytes((_ENCRYPTED_SECTION_MARKER,)) + len(ciphertext).to_bytes(2, "big"),
            ciphertext,
        )
    )
    full_length = len(body) + _TLV_HEADER_LENGTH + _MAC_LENGTH
    header = bytes((FRAME_HANDSHAKE, 0x00)) + full_length.to_bytes(2, "big")
    signed = header + body + b"\x00\x08\x00\x20"
    return signed + hmac_sha256(key, signed)


def parse_handshake_frame(key: bytes, payload: bytes) -> JsonObject:
    """Decrypt an inbound f4 frame's JSON message."""
    cursor = _HANDSHAKE_TLV_START
    iv: bytes | None = None
    while cursor + _TLV_HEADER_LENGTH <= len(payload):
        value_type = int.from_bytes(payload[cursor : cursor + 2], "big")
        if value_type == 0 and payload[cursor + 2 : cursor + 4] == b"\x00\x00":
            cursor += _TLV_HEADER_LENGTH
            break
        length = int.from_bytes(payload[cursor + 2 : cursor + 4], "big")
        if cursor + _TLV_HEADER_LENGTH + length > len(payload):
            raise TuyaIpcP2pProtocolError("Failed to read handshake frame: truncated TLV")
        if value_type == _TLV_INITIALIZATION_VECTOR:
            iv = payload[cursor + _TLV_HEADER_LENGTH : cursor + _TLV_HEADER_LENGTH + length]
        cursor += _TLV_HEADER_LENGTH + length
    if iv is None or cursor >= len(payload) or payload[cursor] != _ENCRYPTED_SECTION_MARKER:
        raise TuyaIpcP2pProtocolError("Failed to read handshake frame: no encrypted section")
    length = int.from_bytes(payload[cursor + 1 : cursor + 3], "big")
    plain = aes_cbc_decrypt(key, iv, payload[cursor + 3 : cursor + 3 + length])
    return parse_json_object(plain)
