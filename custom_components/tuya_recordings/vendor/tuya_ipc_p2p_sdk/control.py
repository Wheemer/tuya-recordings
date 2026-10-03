"""
The channel-0 control protocol, which is what releases the video.

Messages are plaintext packets, PKCS#7-padded and AES-CBC'd into records
exactly like media — the client's records under the offer key, the device's
under the answer key. All fields are little-endian::

    magic  uint32 = 0x12345678
    type   uint32                 # 1 = auth, otherwise a control/IOCtrl command
    -- auth:     username[32] "admin" ‖ cred[32] ‖ reserved[32]
    -- control:  flag uint32 ‖ subCmd uint32 ‖ len uint32 ‖ payload[len]

The device answers each message on conversation 0 and only then opens the video
conversation.
"""

from __future__ import annotations

from dataclasses import dataclass

from .crypto import md5_hex

CONTROL_MAGIC = 0x12345678
TYPE_AUTH = 1
AUTH_USERNAME = "admin"

_AUTH_FIELD_LENGTH = 32
_AUTH_BODY_LENGTH = _AUTH_FIELD_LENGTH * 3
_COMMAND_HEADER_LENGTH = 20
_MINIMUM_PACKET_LENGTH = 8

_CAPABILITY_PAYLOAD = (
    b'{"cmd":"capability_exchange_req","protocol_version":1,'
    b'"data":{"capabilities":{"opus_encode":1,"opus_decode":1}}}\x00'
)


@dataclass(frozen=True, slots=True)
class ControlPacket:
    """One decoded channel-0 message."""

    type: int
    flag: int
    sub_command: int
    payload: bytes


def auth_credential(device_password: str, local_key: str) -> str:
    """Return the channel-0 auth token, ``md5(password + "||" + localKey)``."""
    return md5_hex(f"{device_password}||{local_key}")


def build_auth(username: str, credential: str) -> bytes:
    """Build the type-1 auth packet."""
    body = bytearray(_AUTH_BODY_LENGTH)
    body[0 : len(username)] = username.encode()
    body[_AUTH_FIELD_LENGTH : _AUTH_FIELD_LENGTH + len(credential)] = credential.encode()
    return CONTROL_MAGIC.to_bytes(4, "little") + TYPE_AUTH.to_bytes(4, "little") + bytes(body)


def build_command(packet_type: int, flag: int, sub_command: int, payload: bytes) -> bytes:
    """Build one control or IOCtrl command packet."""
    return b"".join(
        (
            CONTROL_MAGIC.to_bytes(4, "little"),
            (packet_type & 0xFFFFFFFF).to_bytes(4, "little"),
            flag.to_bytes(4, "little"),
            (sub_command & 0xFFFFFFFF).to_bytes(4, "little"),
            len(payload).to_bytes(4, "little"),
            payload,
        )
    )


def _uint32(value: int) -> bytes:
    """Encode one little-endian command argument."""
    return (value & 0xFFFFFFFF).to_bytes(4, "little")


def session_sequence(credential: str, username: str = AUTH_USERNAME) -> tuple[bytes, ...]:
    """Return authentication and capability setup without starting preview."""
    return (
        build_auth(username, credential),
        build_command(0, 0, 0x0A, _uint32(0x00010001)),
        build_command(0, 0, 0x15, _CAPABILITY_PAYLOAD),
        build_command(2, 0, 0x02, _uint32(0)),
        build_command(0x00010004, 0, 0x09, _uint32(0) + _uint32(4)),
    )


def start_sequence(credential: str, username: str = AUTH_USERNAME) -> tuple[bytes, ...]:
    """
    Return the ordered channel-0 burst that brings the video up.

    Only the auth credential is session-specific; the control commands that
    follow it are fixed.
    """
    return session_sequence(credential, username) + (
        build_command(0x00010003, 0, 0x06, _uint32(0) + _uint32(0)),
        build_command(0x00010005, 0, 0x00040006, _uint32(0) + _uint32(4)),
    )


def is_control(plaintext: bytes) -> bool:
    """Whether a decrypted record is a channel-0 message rather than media."""
    return (
        len(plaintext) >= _MINIMUM_PACKET_LENGTH
        and int.from_bytes(plaintext[0:4], "little") == CONTROL_MAGIC
    )


def parse_control(plaintext: bytes) -> ControlPacket | None:
    """Decode one channel-0 message, or return None when the magic is absent."""
    if len(plaintext) < _COMMAND_HEADER_LENGTH or not is_control(plaintext):
        return None
    length = int.from_bytes(plaintext[16:20], "little")
    available = len(plaintext) - _COMMAND_HEADER_LENGTH
    end = _COMMAND_HEADER_LENGTH + min(length, available)
    return ControlPacket(
        type=int.from_bytes(plaintext[4:8], "little"),
        flag=int.from_bytes(plaintext[8:12], "little"),
        sub_command=int.from_bytes(plaintext[12:16], "little"),
        payload=plaintext[_COMMAND_HEADER_LENGTH:end],
    )
