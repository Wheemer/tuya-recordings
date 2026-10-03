"""The binary envelopes used by Tuya device MQTT signaling.

Protocol 2.2 uses CRC32 plus AES-ECB. Protocol 2.3 uses its clear header as
AES-GCM associated data and prepends the 12-byte nonce to the ciphertext.
"""

from __future__ import annotations

import secrets
from typing import NamedTuple
from zlib import crc32

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..crypto import aes_ecb_decrypt, aes_ecb_encrypt
from ..exceptions import TuyaIpcP2pProtocolError
from ..json_types import JsonObject, JsonValue, dump_json, parse_json_object

_VERSION_2_2 = b"2.2"
_VERSION_2_3 = b"2.3"
_HEADER_2_2_LENGTH = 15
_HEADER_2_3_LENGTH = 12
_SOURCE_LENGTH = 4
_GCM_NONCE_LENGTH = 12

SIG_QUERY_PROTOCOL = 22
SESSION_PROTOCOL = 302


class DecodedFrame(NamedTuple):
    """One decoded envelope, before its body is decrypted."""

    sequence: int
    source: bytes
    body: bytes


def encode_frame(sequence: int, source: bytes, body: bytes) -> bytes:
    """Wrap an already-encrypted body in the envelope."""
    tail = sequence.to_bytes(4, "big") + source[:_SOURCE_LENGTH] + body
    return _VERSION_2_2 + crc32(tail).to_bytes(4, "big") + tail


def decode_frame(payload: bytes) -> DecodedFrame:
    """Verify the envelope's CRC and split it."""
    if len(payload) < _HEADER_2_2_LENGTH or payload[:3] != _VERSION_2_2:
        raise TuyaIpcP2pProtocolError("Failed to decode envelope: not a 2.2 frame")
    want = int.from_bytes(payload[3:7], "big")
    got = crc32(payload[7:])
    if got != want:
        raise TuyaIpcP2pProtocolError(
            f"Failed to decode envelope: crc32 {got:08x}, want {want:08x}"
        )
    return DecodedFrame(
        sequence=int.from_bytes(payload[7:11], "big"),
        source=payload[11:_HEADER_2_2_LENGTH],
        body=payload[_HEADER_2_2_LENGTH:],
    )


def _encode_payload_2_3(
    key: bytes,
    sequence: int,
    source: bytes,
    plain: bytes,
    *,
    nonce: bytes | None = None,
) -> bytes:
    """Build the AES-GCM envelope used by DeviceBean PV 2.3."""
    header = (
        _VERSION_2_3
        + sequence.to_bytes(4, "big")
        + source[:_SOURCE_LENGTH]
        + b"\x00"
    )
    actual_nonce = nonce or secrets.token_bytes(_GCM_NONCE_LENGTH)
    if len(actual_nonce) != _GCM_NONCE_LENGTH:
        raise ValueError("AES-GCM signaling nonce must be 12 bytes")
    return header + actual_nonce + AESGCM(key).encrypt(actual_nonce, plain, header)


def _decode_payload_2_3(key: bytes, payload: bytes) -> bytes:
    """Authenticate and decrypt one DeviceBean PV 2.3 envelope."""
    minimum = _HEADER_2_3_LENGTH + _GCM_NONCE_LENGTH + 16
    if len(payload) < minimum or payload[:3] != _VERSION_2_3:
        raise TuyaIpcP2pProtocolError("Failed to decode envelope: not a 2.3 frame")
    header = payload[:_HEADER_2_3_LENGTH]
    nonce = payload[_HEADER_2_3_LENGTH : _HEADER_2_3_LENGTH + _GCM_NONCE_LENGTH]
    ciphertext = payload[_HEADER_2_3_LENGTH + _GCM_NONCE_LENGTH :]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, header)
    except InvalidTag as exception:
        raise TuyaIpcP2pProtocolError(
            "Failed to decode envelope: bad AES-GCM tag"
        ) from exception


def encode_payload(
    key: bytes,
    sequence: int,
    source: bytes,
    data: JsonValue,
    epoch_seconds: int,
    protocol: int,
    protocol_version: str = "2.2",
    *,
    nonce: bytes | None = None,
) -> bytes:
    """Serialize and frame one signaling payload for the device PV."""
    plain = dump_json({"data": data, "protocol": protocol, "t": epoch_seconds})
    if protocol_version == "2.3":
        return _encode_payload_2_3(
            key, sequence, source, plain, nonce=nonce
        )
    if protocol_version != "2.2":
        raise TuyaIpcP2pProtocolError(
            f"Unsupported signaling protocol version: {protocol_version}"
        )
    return encode_frame(sequence, source, aes_ecb_encrypt(key, plain))


def decode_payload(key: bytes, payload: bytes) -> JsonObject:
    """Return the ``data`` object of one inbound signaling payload."""
    if payload[:3] == _VERSION_2_3:
        plain = _decode_payload_2_3(key, payload)
    else:
        plain = aes_ecb_decrypt(key, decode_frame(payload).body)
    decoded = parse_json_object(plain)
    data = decoded.get("data")
    if not isinstance(data, dict):
        raise TuyaIpcP2pProtocolError("Failed to decode payload: no data object")
    return data
