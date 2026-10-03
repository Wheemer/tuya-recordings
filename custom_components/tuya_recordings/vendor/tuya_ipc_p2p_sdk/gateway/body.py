"""
The ``et=3`` encrypted request and response body.

AES-GCM with a fresh 12-byte nonce, on the wire as base64 of
``nonce || ciphertext || tag``. Despite the Java helper being named
"appendNonce", the nonce goes at the front.
"""

from __future__ import annotations

import secrets
from base64 import b64decode, b64encode

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..exceptions import TuyaIpcP2pProtocolError

_NONCE_LENGTH = 12
_TAG_LENGTH = 16


def encrypt_post_data(key: str, post_json: bytes) -> str:
    """Encrypt one request body and return it base64-encoded."""
    nonce = secrets.token_bytes(_NONCE_LENGTH)
    sealed = AESGCM(key.encode()).encrypt(nonce, post_json, None)
    return b64encode(nonce + sealed).decode()


def decrypt_post_data(key: str, encrypted: str) -> bytes:
    """Decrypt one base64 body, whether it came from a request or a response."""
    try:
        raw = b64decode(encrypted, validate=True)
    except ValueError as exception:
        raise TuyaIpcP2pProtocolError(f"Failed to decode body: {exception}") from exception
    if len(raw) < _NONCE_LENGTH + _TAG_LENGTH:
        raise TuyaIpcP2pProtocolError("Failed to decode body: shorter than nonce and tag")
    try:
        return AESGCM(key.encode()).decrypt(raw[:_NONCE_LENGTH], raw[_NONCE_LENGTH:], None)
    except InvalidTag as exception:
        raise TuyaIpcP2pProtocolError("Failed to decrypt body: authentication tag") from exception
