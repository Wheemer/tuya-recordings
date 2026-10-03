import json
import struct

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from custom_components.tuya_recordings.lib.native_media_crypto import (
    MEDIA_SECRET_API,
    MEDIA_SECRET_API_VERSION,
    NativeMediaCryptoError,
    NativeMediaSecretStore,
    decrypt_playback_frame,
)
from custom_components.tuya_recordings.lib.native_playback_stream import (
    PLAYBACK_CODEC_INFO_BYTES,
    NativePlaybackStreamParser,
)


def test_secret_store_batches_missing_uuids_and_reuses_cached_values():
    calls = []

    def call_api(api, version, body, extra_params):
        calls.append((api, version, body, extra_params))
        return [
            {"uuid": uuid, "secretKey": f"0123456789abcdef-{uuid}"}
            for uuid in json.loads(body["uuids"])
        ]

    store = NativeMediaSecretStore(call_api)
    store.prefetch(["clip-1", "clip-2", "clip-1"])

    assert calls == [
        (
            MEDIA_SECRET_API,
            MEDIA_SECRET_API_VERSION,
            {"uuids": '["clip-1","clip-2"]'},
            {},
        )
    ]
    assert store.secret("clip-1") == "0123456789abcdef-clip-1"
    assert len(calls) == 1


def test_secret_store_rejects_malformed_gateway_rows():
    store = NativeMediaSecretStore(lambda *args: [{"uuid": "clip-1"}])

    with pytest.raises(NativeMediaCryptoError, match="had no key"):
        store.prefetch(["clip-1"])


def test_decrypt_playback_frame_matches_apk_aes_cbc_packet_contract():
    uuid = "clip-uuid"
    secret = "0123456789abcdef-extra"
    iv = bytes(range(16))
    clear = b"\x00\x00\x00\x01\x65native-h264"
    padding = 16 - len(clear) % 16
    padded = clear + bytes([padding]) * padding
    encryptor = Cipher(
        algorithms.AES(secret.encode()[:16]), modes.CBC(iv)
    ).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    codec_info = bytearray(PLAYBACK_CODEC_INFO_BYTES)
    struct.pack_into(">6H", codec_info, 0, 1, 8, 1, 1, 1, 0)
    codec_info[12 : 12 + len(uuid)] = uuid.encode()
    codec_info[44:60] = iv
    header = struct.pack(
        ">HH7I", 1, 1, len(encrypted), 1, 2, 3, 4, 5, 6
    )
    [frame] = NativePlaybackStreamParser().feed(
        header + bytes(codec_info) + encrypted
    )

    decrypted = decrypt_playback_frame(frame, {uuid: secret})

    assert decrypted.payload == clear
    assert decrypted.payload_length == len(clear)


def test_decrypt_playback_frame_rejects_bad_padding():
    uuid = "clip-uuid"
    secret = "0123456789abcdef"
    iv = bytes(range(16))
    encryptor = Cipher(algorithms.AES(secret.encode()), modes.CBC(iv)).encryptor()
    encrypted = encryptor.update(b"invalid-padding!") + encryptor.finalize()
    codec_info = bytearray(PLAYBACK_CODEC_INFO_BYTES)
    struct.pack_into(">6H", codec_info, 0, 1, 8, 1, 1, 1, 0)
    codec_info[12 : 12 + len(uuid)] = uuid.encode()
    codec_info[44:60] = iv
    header = struct.pack(">HH7I", 1, 1, len(encrypted), 1, 2, 3, 4, 5, 6)
    [frame] = NativePlaybackStreamParser().feed(
        header + bytes(codec_info) + encrypted
    )

    with pytest.raises(NativeMediaCryptoError, match="padding"):
        decrypt_playback_frame(frame, {uuid: secret})
