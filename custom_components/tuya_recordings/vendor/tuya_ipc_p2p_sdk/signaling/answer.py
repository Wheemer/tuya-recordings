"""The device's SDP answer, which carries the key the video is encrypted with."""

from __future__ import annotations

from dataclasses import dataclass

from ..crypto import MEDIA_KEY_SIZE
from ..exceptions import TuyaIpcP2pProtocolError

_UFRAG_PREFIX = "a=ice-ufrag:"
_PASSWORD_PREFIX = "a=ice-pwd:"  # noqa: S105
_AES_KEY_PREFIX = "a=aes-key:"


@dataclass(frozen=True, slots=True)
class SdpAnswer:
    """The device's ICE credentials and its device-to-client media key."""

    ice_ufrag: str
    ice_password: str
    aes_key: bytes


def parse_answer(sdp: str) -> SdpAnswer:
    """Read the answer, failing when any of the three fields is missing."""
    ice_ufrag = ""
    ice_password = ""
    aes_key = b""
    for raw_line in sdp.splitlines():
        line = raw_line.rstrip("\r")
        if line.startswith(_UFRAG_PREFIX):
            ice_ufrag = line.removeprefix(_UFRAG_PREFIX)
        elif line.startswith(_PASSWORD_PREFIX):
            ice_password = line.removeprefix(_PASSWORD_PREFIX)
        elif line.startswith(_AES_KEY_PREFIX):
            aes_key = bytes.fromhex(line.removeprefix(_AES_KEY_PREFIX))
    if not ice_ufrag or not ice_password or len(aes_key) != MEDIA_KEY_SIZE:
        raise TuyaIpcP2pProtocolError(
            f"Failed to parse answer: ufrag={bool(ice_ufrag)} pwd={bool(ice_password)}"
            f" keylen={len(aes_key)}"
        )
    return SdpAnswer(ice_ufrag=ice_ufrag, ice_password=ice_password, aes_key=aes_key)
