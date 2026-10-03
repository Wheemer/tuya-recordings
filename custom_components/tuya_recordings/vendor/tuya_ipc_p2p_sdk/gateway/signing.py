"""
The mobile gateway request signature.

``sign`` is an HMAC-SHA256 over the whitelisted, non-empty parameters sorted by
key and joined with ``||``, under a composite key made of four constants of the
app build. ``postData`` does not enter the string as itself but as a reordered
MD5 of the encrypted body.
"""

from __future__ import annotations

from ..const import COMPOSITE_KEY
from ..crypto import hmac_sha256_hex, md5_hex

_BODY_KEY_LENGTH = 16
_MD5_HEX_LENGTH = 32

# The only parameters that enter the signature; anything else rides the form
# but stays out of the string, `sign` itself included.
_SIGN_WHITELIST: frozenset[str] = frozenset(
    {
        "a",
        "v",
        "lat",
        "lon",
        "lang",
        "deviceId",
        "appVersion",
        "ttid",
        "isH5",
        "h5Token",
        "os",
        "clientId",
        "postData",
        "time",
        "requestId",
        "et",
        "n4h5",
        "sid",
        "chKey",
        "sp",
    }
)


def sign(sign_string: str, key: str = COMPOSITE_KEY) -> str:
    """Return the signature of an assembled sign string."""
    return hmac_sha256_hex(key, sign_string)


def build_sign_string(params: dict[str, str]) -> str:
    """Keep the whitelisted non-empty parameters, sort them and join them."""
    return "||".join(
        f"{key}={params[key]}" for key in sorted(params) if params[key] and key in _SIGN_WHITELIST
    )


def swap_sign_string(value: str) -> str:
    """Reorder a 32-character MD5 hex as ``s[8:16] + s[0:8] + s[24:32] + s[16:24]``."""
    if len(value) != _MD5_HEX_LENGTH:
        return value
    return value[8:16] + value[0:8] + value[24:32] + value[16:24]


def post_data_sign_field(encrypted_post_data: str) -> str:
    """Return the value that stands in for ``postData`` inside the sign string."""
    return swap_sign_string(md5_hex(encrypted_post_data))


def body_key(request_id: str, ecode: str, key: str = COMPOSITE_KEY) -> str:
    """
    Derive the AES-GCM key that encrypts a session-scoped ``et=3`` body.

    The HMAC key is the per-request id and the message is the composite key
    joined with the session ecode; the body key is the first 16 hex characters
    of the digest, used as ASCII.
    """
    return hmac_sha256_hex(request_id, f"{key}_{ecode}")[:_BODY_KEY_LENGTH]


def pre_login_body_key(request_id: str, key: str = COMPOSITE_KEY) -> str:
    """Derive the body key of a call made before there is a session."""
    return hmac_sha256_hex(request_id, key)[:_BODY_KEY_LENGTH]
