"""
The signatures of the TCP-relay handshake.

Four JSON messages: the client offers a random, the device answers with its
signature over it plus a random of its own, and the client proves itself by
signing both back. Both directions HMAC-SHA256 a colon-joined message under the
relay credential, right-padded with NUL bytes to 64.
"""

from __future__ import annotations

from ..crypto import hmac_sha256_hex
from ..exceptions import TuyaIpcP2pProtocolError
from ..json_types import dump_json

_AUTH_KEY_LENGTH = 64


class HandshakeSigner:
    """Signs and verifies the relay's challenge-response for one session."""

    def __init__(
        self,
        credential: str,
        expire_timestamp: str,
        device_id: str,
        session_id: str,
        uid: str,
    ) -> None:
        """Bind the signer to the rendezvous the handshake is for."""
        if not credential:
            raise TuyaIpcP2pProtocolError("Failed to sign handshake: no relay credential")
        self._key = credential.encode().ljust(_AUTH_KEY_LENGTH, b"\x00")
        self._prefix = (expire_timestamp, device_id, session_id, uid)

    def _sign(self, *tail: str) -> str:
        """Sign the colon-joined prefix plus whatever the message adds to it."""
        return hmac_sha256_hex(self._key, ":".join((*self._prefix, *tail)))

    def device_signature(self, client_random: str) -> str:
        """Return the signature the device should produce over our random."""
        return self._sign(client_random)

    def ack_signature(self, device_signature: str, device_random: str) -> str:
        """Our own signature, binding the device's signature and random."""
        return self._sign(device_signature, device_random)


def build_auth_request(device_id: str, uid: str, random: str) -> bytes:
    """Build handshake message 1."""
    return dump_json(
        {
            "clientType": 1,
            "method": "request",
            "devId": device_id,
            "uId": uid,
            "authorization": f"random={random}",
        }
    )


def build_auth_ack(device_id: str, uid: str, signature: str) -> bytes:
    """Build handshake message 3."""
    return dump_json(
        {
            "clientType": 1,
            "method": "ack",
            "devId": device_id,
            "uId": uid,
            "statuscode": 200,
            "authorization": f"signature={signature}",
        }
    )


def authorization_field(authorization: str, key: str) -> str:
    """Pull one ``key=value`` out of a comma-joined authorization string."""
    for part in authorization.split(","):
        if part.startswith(f"{key}="):
            return part[len(key) + 1 :]
    return ""
