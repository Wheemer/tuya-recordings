from datetime import date

from custom_components.tuya_recordings.lib import best_clip_match, merge_cached_clips, normalize_clip


def test_normalize_clip_accepts_epoch_milliseconds_and_thumbnail():
    clip = normalize_clip({"startTime": 1782488168000, "endTime": 1782488206000, "thumbUrl": " https://example/thumb.jpg "})

    assert clip is not None
    assert clip["start"] == 1782488168
    assert clip["end"] == 1782488206
    assert clip["thumbnail"] == "https://example/thumb.jpg"


def test_normalize_clip_uses_authoritative_catalog_day():
    clip = normalize_clip(
        {"start_time": 1789963078, "end_time": 1789963115},
        catalog_day=date(2026, 9, 21),
    )

    assert clip is not None
    assert clip["date"] == "2026-09-21"


def test_normalize_clip_preserves_apk_playback_metadata():
    clip = normalize_clip(
        {
            "startTime": 1782488168,
            "endTime": 1782488206,
            "uuid": "clip-uuid",
            "encrypt": True,
            "encryptMD5": "abc123",
            "fragments": [{"start": 1782488168, "end": 1782488206}],
            "eventTypeArr": ["motion"],
            "aiDetectList": [{"type": "person"}],
            "cameraChannel": 0,
            "ignored": "not stored",
        }
    )

    assert clip is not None
    assert clip["raw"] == {
        "startTime": 1782488168,
        "endTime": 1782488206,
        "uuid": "clip-uuid",
        "encrypt": True,
        "encryptMD5": "abc123",
        "fragments": [{"start": 1782488168, "end": 1782488206}],
        "eventTypeArr": ["motion"],
        "aiDetectList": [{"type": "person"}],
        "cameraChannel": 0,
    }


def test_normalize_clip_accepts_native_v4_registered_fields():
    clip = normalize_clip(
        {
            "start_time": 1782488168,
            "end_time": 1782488206,
            "event_type": 2,
            "video_type": 1,
            "encrypt": 0,
            "uuid": "native-clip",
            "encrypt_md5": "digest",
        }
    )

    assert clip is not None
    assert clip["start"] == 1782488168
    assert clip["end"] == 1782488206
    assert clip["raw"] == {
        "start_time": 1782488168,
        "end_time": 1782488206,
        "event_type": 2,
        "video_type": 1,
        "encrypt": 0,
        "uuid": "native-clip",
        "encrypt_md5": "digest",
    }


def test_merge_cached_clips_prefers_new_clip_values():
    merged = merge_cached_clips(
        [{"start": 10, "end": 20, "name": "new"}],
        [{"start": 10, "end": 20, "name": "old"}, {"start": 30, "end": 40}],
    )

    assert merged == [{"start": 30, "end": 40}, {"start": 10, "end": 20, "name": "new"}]


def test_best_clip_match_prefers_nearest_overlap():
    clips = [
        {"start": 80, "end": 95},
        {"start": 98, "end": 131},
        {"start": 50, "end": 180},
    ]

    assert best_clip_match(clips, 100, 130) == {"start": 98, "end": 131}
