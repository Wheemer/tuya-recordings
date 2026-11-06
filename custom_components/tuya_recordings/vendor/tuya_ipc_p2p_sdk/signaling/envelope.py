"""The binary envelopes used by Tuya device MQTT signaling.

PV 1.1 uses keyed CRC32 over a plaintext body. PV 2.0 sends signed JSON
and receives keyed CRC32 frames. PV 2.1 uses signed base64
AES-ECB, and PV 2.2 uses CRC32 plus binary AES-ECB. PV 2.3 authenticates
its clear header with AES-GCM and prepends the nonce to the ciphertext.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from typing import NamedTuple
from zlib import crc32

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..crypto import aes_ecb_decrypt, aes_ecb_encrypt
from ..exceptions import TuyaIpcP2pProtocolError
from ..json_types import JsonObject, JsonValue, dump_json, parse_json_object

_VERSION_1_1 = b"1.1"
_VERSION_2_0 = b"2.0"
_VERSION_2_1 = b"2.1"
_VERSION_2_2 = b"2.2"
_VERSION_2_3 = b"2.3"
_HEADER_2_2_LENGTH = 15
_HEADER_2_3_LENGTH = 12
_SOURCE_LENGTH = 4
_GCM_NONCE_LENGTH = 12

SIG_QUERY_PROTOCOL = 22
SESSION_PROTOCOL = 302
SUPPORTED_PROTOCOL_VERSIONS = frozenset({"1.1", "2.0", "2.1", "2.2", "2.3"})


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
    device_id: str = "",
) -> bytes:
    """Serialize and frame one signaling payload for the device PV."""
    plain = dump_json({"data": data, "protocol": protocol, "t": epoch_seconds})
    if protocol_version == "1.1":
        plain = dump_json({"data": _omit_null_members(data), "protocol": protocol, "t": epoch_seconds})
        counter = sequence.to_bytes(4, "big") + source[:_SOURCE_LENGTH]
        return _VERSION_1_1 + _pv11_signature(key, counter, plain) + counter + plain
    if protocol_version == "2.0":
        if not device_id:
            raise TuyaIpcP2pProtocolError("PV 2.0 requires a gateway device ID")
        fields = {
            "data": aes_ecb_encrypt(key, dump_json(data)).hex().upper(),
            "gwId": device_id, "protocol": protocol, "pv": "2.0", "t": epoch_seconds,
        }
        signed = "||".join(f"{name}={value}" for name, value in sorted(fields.items())).encode()
        signature = hashlib.md5(signed + b"||" + key, usedforsecurity=False).hexdigest()
        return dump_json({**fields, "sign": signature})
    if protocol_version == "2.1":
        plain = dump_json({"data": data, "protocol": protocol, "t": epoch_seconds, "s": sequence})
        encoded = base64.b64encode(aes_ecb_encrypt(key, plain))
        return _VERSION_2_1 + _pv21_signature(key, encoded) + encoded
    if protocol_version == "2.3":
        return _encode_payload_2_3(
            key, sequence, source, plain, nonce=nonce
        )
    if protocol_version != "2.2":
        raise TuyaIpcP2pProtocolError(
            f"Unsupported signaling protocol version: {protocol_version}"
        )
    return encode_frame(sequence, source, aes_ecb_encrypt(key, plain))


def decode_payload(key: bytes, payload: bytes) -> tuple[int, JsonObject]:
    """Return the protocol and data of one inbound MQTT payload."""
    if payload[:3] in {_VERSION_1_1, _VERSION_2_0}:
        plain = payload[15:]
        if not plain or not hmac.compare_digest(payload[3:7], _pv11_signature(key, payload[7:15], plain)):
            raise TuyaIpcP2pProtocolError("Failed to decode envelope: invalid legacy CRC")
    elif payload[:3] == _VERSION_2_1:
        encoded = payload[19:]
        if not encoded or not hmac.compare_digest(payload[3:19], _pv21_signature(key, encoded)):
            raise TuyaIpcP2pProtocolError("Failed to decode envelope: invalid 2.1 signature")
        try:
            encrypted = base64.b64decode(encoded, validate=True)
        except binascii.Error as err:
            raise TuyaIpcP2pProtocolError("Failed to decode envelope: invalid 2.1 base64") from err
        plain = aes_ecb_decrypt(key, encrypted)
    elif payload[:3] == _VERSION_2_3:
        plain = _decode_payload_2_3(key, payload)
    else:
        plain = aes_ecb_decrypt(key, decode_frame(payload).body)
    decoded = parse_json_object(plain)
    protocol = decoded.get("protocol")
    if type(protocol) is not int:
        raise TuyaIpcP2pProtocolError("Failed to decode payload: no protocol number")
    data = decoded.get("data")
    if not isinstance(data, dict):
        raise TuyaIpcP2pProtocolError("Failed to decode payload: no data object")
    return protocol, data


def _pv11_signature(key: bytes, counter: bytes, plain: bytes) -> bytes:
    return crc32(counter + crc32(plain).to_bytes(4, "big") + key).to_bytes(4, "big")


def _omit_null_members(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {key: _omit_null_members(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_omit_null_members(item) for item in value]
    return value


def _pv21_signature(key: bytes, encoded: bytes) -> bytes:
    # This digest is part of the legacy device wire format, not a security primitive.
    signed = b"data=" + encoded + b"||pv=2.1||" + key
    return hashlib.md5(signed, usedforsecurity=False).hexdigest()[8:24].encode("ascii")
