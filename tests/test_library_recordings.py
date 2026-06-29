from custom_components.tuya_recordings.lib import best_clip_match, merge_cached_clips, normalize_clip


def test_normalize_clip_accepts_epoch_milliseconds_and_thumbnail():
    clip = normalize_clip({"startTime": 1782488168000, "endTime": 1782488206000, "thumbUrl": " https://example/thumb.jpg "})

    assert clip is not None
    assert clip["start"] == 1782488168
    assert clip["end"] == 1782488206
    assert clip["thumbnail"] == "https://example/thumb.jpg"


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
