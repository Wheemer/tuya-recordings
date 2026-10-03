from __future__ import annotations

from datetime import date

from custom_components.tuya_recordings.client import (
    TuyaRecordingsClient,
    _playback_fragments_json,
    _truthy,
)


class MetadataClient(TuyaRecordingsClient):
    def __init__(self, index):
        self._index = index

    def cached_camera_index(self):
        return self._index


def test_truthy_accepts_apk_boolean_shapes():
    assert _truthy(True) is True
    assert _truthy(1) is True
    assert _truthy("true") is True
    assert _truthy("1") is True
    assert _truthy("on") is True
    assert _truthy(False) is False
    assert _truthy(0) is False
    assert _truthy("false") is False


def test_playback_fragments_json_matches_smart_life_time_piece():
    assert _playback_fragments_json(
        1782488168000,
        1782488206000,
    ) == '{"fragments":[{"start":1782488168,"end":1782488206}]}'


def test_clip_playback_metadata_uses_cached_apk_fields():
    client = MetadataClient(
        {
            "cameras": [
                {
                    "devId": "camera-1",
                    "clips": [
                        {
                            "start": 1782488168,
                            "end": 1782488206,
                            "date": "2026-06-26",
                            "raw": {
                                "uuid": "clip-uuid",
                                "encryptMD5": "abc123",
                                "fragments": [{"start": 1782488168, "end": 1782488206}],
                            },
                        }
                    ],
                }
            ]
        }
    )

    assert client._clip_playback_metadata("camera-1", 1782488168, 1782488206) == {
        "encrypted": True,
        "encryption_uuid": "clip-uuid",
        "catalog_day": date(2026, 6, 26),
        "fragments_json": '{"fragments":[{"start":1782488168,"end":1782488206}]}',
    }


def test_clip_playback_metadata_defaults_when_clip_not_indexed():
    client = MetadataClient({"cameras": [{"devId": "camera-1", "clips": []}]})

    assert client._clip_playback_metadata("camera-1", 10, 20) == {
        "encrypted": False,
        "fragments_json": '{"fragments":[{"start":10,"end":20}]}',
    }


def test_timeline_request_always_carries_apk_play_mode_fragment():
    client = MetadataClient({"cameras": [{"devId": "camera-1", "clips": []}]})

    request = client.timeline_request("camera-1", 100, 140, 125)

    assert request.start == 100
    assert request.end == 140
    assert request.position == 125
    assert request.fragments_json == '{"fragments":[{"start":100,"end":140}]}'


def test_clip_playback_metadata_accepts_native_snake_case_encryption_fields():
    client = MetadataClient(
        {
            "cameras": [
                {
                    "devId": "camera-1",
                    "clips": [
                        {
                            "start": 10,
                            "end": 20,
                            "raw": {
                                "uuid": "native-uuid",
                                "encrypt_md5": "digest",
                            },
                        }
                    ],
                }
            ]
        }
    )

    assert client._clip_playback_metadata("camera-1", 10, 20) == {
        "encrypted": True,
        "encryption_uuid": "native-uuid",
        "fragments_json": '{"fragments":[{"start":10,"end":20}]}',
    }
