"""
The primitives the Tuya P2P protocol is built out of.

None of these choices are the SDK's: the gateway signs with MD5-derived
material, the signaling envelope is AES-ECB and the media records are
AES-128-CBC with a per-record IV. They are reproduced here because that is what
the device answers.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .exceptions import TuyaIpcP2pProtocolError

AES_BLOCK_SIZE = 16
MEDIA_KEY_SIZE = 16

_ALPHANUMERIC = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def _as_bytes(value: str | bytes) -> bytes:
    """Accept either representation, because the protocol mixes both."""
    return value.encode() if isinstance(value, str) else value


def md5_hex(value: str | bytes) -> str:
    """Return the lowercase hex MD5 digest the gateway's key derivations use."""
    return hashlib.md5(_as_bytes(value), usedforsecurity=False).hexdigest()


def hmac_sha256(key: str | bytes, message: str | bytes) -> bytes:
    """Return the raw HMAC-SHA256 digest."""
    return hmac.new(_as_bytes(key), _as_bytes(message), hashlib.sha256).digest()


def hmac_sha256_hex(key: str | bytes, message: str | bytes) -> str:
    """Return the lowercase hex HMAC-SHA256 digest."""
    return hmac.new(_as_bytes(key), _as_bytes(message), hashlib.sha256).hexdigest()


def hmac_sha1(key: bytes, message: bytes) -> bytes:
    """Return the raw HMAC-SHA1 digest that tags every relay media frame."""
    return hmac.new(key, message, hashlib.sha1).digest()


def pad_pkcs7(plain: bytes) -> bytes:
    """Pad to the AES block size, always adding at least one byte."""
    padding = AES_BLOCK_SIZE - (len(plain) % AES_BLOCK_SIZE)
    return plain + bytes([padding]) * padding


def unpad_pkcs7(padded: bytes) -> bytes:
    """Strip PKCS#7 padding, rejecting anything that is not valid padding."""
    if not padded:
        raise TuyaIpcP2pProtocolError("Failed to unpad: empty plaintext")
    padding = padded[-1]
    if padding < 1 or padding > AES_BLOCK_SIZE or padding > len(padded):
        raise TuyaIpcP2pProtocolError(f"Failed to unpad: bad PKCS#7 length {padding}")
    if padded[-padding:] != bytes([padding]) * padding:
        raise TuyaIpcP2pProtocolError("Failed to unpad: bad PKCS#7 bytes")
    return padded[:-padding]


def aes_cbc_encrypt_raw(key: bytes, iv: bytes, padded: bytes) -> bytes:
    """
    Encrypt an already block-aligned plaintext.

    The signaling tunnel pads one whole message and only then splits it across
    records, so it needs an entry point that does not pad each chunk again.
    """
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def aes_cbc_encrypt(key: bytes, iv: bytes, plain: bytes) -> bytes:
    """Pad and encrypt with AES-CBC."""
    return aes_cbc_encrypt_raw(key, iv, pad_pkcs7(plain))


def aes_cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """Decrypt an AES-CBC ciphertext and strip its padding."""
    if not ciphertext or len(ciphertext) % AES_BLOCK_SIZE:
        raise TuyaIpcP2pProtocolError(
            f"Failed to decrypt: ciphertext not block-aligned ({len(ciphertext)})"
        )
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return unpad_pkcs7(decryptor.update(ciphertext) + decryptor.finalize())


def aes_ecb_encrypt(key: bytes, plain: bytes) -> bytes:
    """
    Encrypt with AES-ECB and PKCS#7.

    ECB is the Java "AES" default, and it is what the signaling envelope body
    is encrypted with under the device local key.
    """
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()  # noqa: S305
    return encryptor.update(pad_pkcs7(plain)) + encryptor.finalize()


def aes_ecb_decrypt(key: bytes, ciphertext: bytes) -> bytes:
    """Decrypt an AES-ECB signaling envelope body."""
    if not ciphertext or len(ciphertext) % AES_BLOCK_SIZE:
        raise TuyaIpcP2pProtocolError(
            f"Failed to decrypt: ciphertext not block-aligned ({len(ciphertext)})"
        )
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()  # noqa: S305
    return unpad_pkcs7(decryptor.update(ciphertext) + decryptor.finalize())


def _require_media_key(key: bytes) -> None:
    """Reject a key the record layer cannot use."""
    if len(key) != MEDIA_KEY_SIZE:
        raise TuyaIpcP2pProtocolError(f"Failed to use media key: {len(key)} bytes, want 16")


def encrypt_record(key: bytes, plaintext: bytes) -> bytes:
    """Build one on-wire record: a fresh IV followed by the AES-CBC ciphertext."""
    _require_media_key(key)
    iv = secrets.token_bytes(AES_BLOCK_SIZE)
    return iv + aes_cbc_encrypt_raw(key, iv, pad_pkcs7(plaintext))


def decrypt_record(key: bytes, record: bytes) -> bytes:
    """
    Decrypt one on-wire record.

    The device-to-client direction is keyed by the answer's ``aes-key``, the
    client-to-device direction by the offer's.
    """
    _require_media_key(key)
    if len(record) < AES_BLOCK_SIZE * 2 or len(record) % AES_BLOCK_SIZE:
        raise TuyaIpcP2pProtocolError("Failed to decrypt record: not IV plus whole AES blocks")
    return aes_cbc_decrypt(key, record[:AES_BLOCK_SIZE], record[AES_BLOCK_SIZE:])


def random_alphanumeric(length: int) -> str:
    """Return a random alphanumeric string, as the app's random fields are shaped."""
    return "".join(secrets.choice(_ALPHANUMERIC) for _ in range(length))
