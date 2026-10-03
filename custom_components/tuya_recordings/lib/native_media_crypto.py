"""Smart Life APK-compatible SD-card media encryption support."""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .native_playback_stream import NativePlaybackStreamFrame

MEDIA_SECRET_API = "thing.m.ipc.storage.secret.get.list"
MEDIA_SECRET_API_VERSION = "2.0"
_AES_BLOCK_BYTES = 16
_CODEC_UUID_START = 12
_CODEC_UUID_END = 28
_CODEC_IV_START = 44
_CODEC_IV_END = 60
_DEFAULT_MAX_SECRETS = 1024


class NativeMediaCryptoError(ValueError):
    """Encrypted native media could not be safely decoded."""


class NativeMediaSecretStore:
    """Bounded thread-safe cache for the APK's per-recording media secrets."""

    def __init__(
        self,
        call_api: Callable[[str, str, dict[str, Any], dict[str, str]], Any],
        *,
        max_secrets: int = _DEFAULT_MAX_SECRETS,
    ) -> None:
        if not callable(call_api):
            raise NativeMediaCryptoError("Media secret API caller is unavailable")
        if not 1 <= max_secrets <= 10_000:
            raise NativeMediaCryptoError("Media secret cache size is invalid")
        self._call_api = call_api
        self._max_secrets = max_secrets
        self._secrets: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.RLock()

    def prefetch(self, uuids: Iterable[str]) -> None:
        """Fetch all missing UUID secrets in one APK-equivalent gateway call."""
        requested = _normalized_uuids(uuids)
        with self._lock:
            missing = [uuid for uuid in requested if uuid not in self._secrets]
            if not missing:
                for uuid in requested:
                    self._secrets.move_to_end(uuid)
                return
            result = self._call_api(
                MEDIA_SECRET_API,
                MEDIA_SECRET_API_VERSION,
                {"uuids": json.dumps(missing, separators=(",", ":"))},
                {},
            )
            parsed = _parse_secret_rows(result, set(missing))
            for uuid, secret in parsed.items():
                self._secrets[uuid] = secret
                self._secrets.move_to_end(uuid)
            while len(self._secrets) > self._max_secrets:
                self._secrets.popitem(last=False)

    def secret(self, uuid: str) -> str:
        """Return one secret, fetching it once if the catalog did not preload it."""
        normalized = _normalized_uuid(uuid)
        self.prefetch([normalized])
        with self._lock:
            try:
                secret = self._secrets[normalized]
            except KeyError as err:
                raise NativeMediaCryptoError(
                    "Smart Life did not return the recording encryption secret"
                ) from err
            self._secrets.move_to_end(normalized)
            return secret

    def cached(self, uuid: str) -> str | None:
        """Return a cached secret without performing gateway work."""
        normalized = _normalized_uuid(uuid)
        with self._lock:
            secret = self._secrets.get(normalized)
            if secret is not None:
                self._secrets.move_to_end(normalized)
            return secret


def decrypt_playback_frame(
    frame: NativePlaybackStreamFrame,
    secrets: Mapping[str, str],
    *,
    recording_uuid: str = "",
) -> NativePlaybackStreamFrame:
    """Decrypt one packet exactly when its native codec block requests it."""
    codec_info = frame.codec_info
    if codec_info is None or codec_info.field_4 == 0:
        return frame
    if len(codec_info.raw) < _CODEC_IV_END:
        raise NativeMediaCryptoError("Encrypted media codec information is incomplete")
    if not frame.payload or len(frame.payload) % _AES_BLOCK_BYTES:
        raise NativeMediaCryptoError("Encrypted media packet is not AES block aligned")

    packet_uuid = _codec_uuid(codec_info.raw)
    lookup_uuid = packet_uuid or _normalized_uuid(recording_uuid)
    secret = secrets.get(lookup_uuid) if lookup_uuid else None
    if secret is None and recording_uuid:
        secret = secrets.get(_normalized_uuid(recording_uuid))
    if secret is None and len(secrets) == 1:
        secret = next(iter(secrets.values()))
    if not isinstance(secret, str) or not secret:
        raise NativeMediaCryptoError("Encrypted media packet has no matching secret")

    key_bytes = secret.encode("utf-8")
    if len(key_bytes) < _AES_BLOCK_BYTES:
        raise NativeMediaCryptoError(
            "Recording encryption secret is shorter than 128 bits"
        )
    iv = codec_info.raw[_CODEC_IV_START:_CODEC_IV_END]
    decryptor = Cipher(
        algorithms.AES(key_bytes[:_AES_BLOCK_BYTES]), modes.CBC(iv)
    ).decryptor()
    padded = decryptor.update(frame.payload) + decryptor.finalize()
    payload = _unpad_pkcs7(padded)
    return replace(frame, payload=payload, payload_length=len(payload))


def _parse_secret_rows(value: Any, requested: set[str]) -> dict[str, str]:
    if not isinstance(value, list):
        raise NativeMediaCryptoError("Smart Life media secret response was not a list")
    found: dict[str, str] = {}
    for row in value:
        if not isinstance(row, dict):
            raise NativeMediaCryptoError("Smart Life media secret row was malformed")
        uuid = _normalized_uuid(row.get("uuid"))
        secret = row.get("secretKey") or row.get("encrypt")
        if uuid not in requested:
            continue
        if not isinstance(secret, str) or not secret:
            raise NativeMediaCryptoError("Smart Life media secret row had no key")
        found[uuid] = secret
    return found


def _normalized_uuids(values: Iterable[str]) -> list[str]:
    if isinstance(values, str | bytes):
        raise NativeMediaCryptoError("Recording UUID collection is invalid")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        uuid = _normalized_uuid(value)
        if uuid and uuid not in seen:
            result.append(uuid)
            seen.add(uuid)
    return result


def _normalized_uuid(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _codec_uuid(raw: bytes) -> str:
    value = raw[_CODEC_UUID_START:_CODEC_UUID_END].split(b"\0", 1)[0]
    try:
        return value.decode("utf-8").strip()
    except UnicodeDecodeError as err:
        raise NativeMediaCryptoError("Encrypted media packet UUID was invalid") from err


def _unpad_pkcs7(value: bytes) -> bytes:
    if not value:
        raise NativeMediaCryptoError("Encrypted media packet decrypted to no data")
    padding = value[-1]
    if (
        not 1 <= padding <= _AES_BLOCK_BYTES
        or value[-padding:] != bytes([padding]) * padding
    ):
        raise NativeMediaCryptoError("Encrypted media packet padding was invalid")
    return value[:-padding]
