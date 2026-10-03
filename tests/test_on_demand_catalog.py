from __future__ import annotations

import json
from datetime import date

from custom_components.tuya_recordings.client import TuyaRecordingsClient


class DayCatalogBackend:
    name = "apk-native"
    apk_native = True
    clip_playback_available = True

    def __init__(self, clips):
        self.clips = clips
        self.queries = []

    def recordings_for_day(self, dev_id, config, auth, day):
        self.queries.append((dev_id, day))
        return list(self.clips)


def client_for_day(tmp_path, backend):
    client = TuyaRecordingsClient(
        {
            "cloud_activity_paused": False,
            "media_sync_enabled": False,
            "thumbnail_sync_enabled": False,
        },
        cache_path=tmp_path / "catalog.json",
        recordings_backend=backend,
    )
    client.set_camera_inventory({"camera": "Front"})
    return client


def test_on_demand_day_replaces_stale_day_and_persists_catalog(tmp_path):
    backend = DayCatalogBackend(
        [{"start": 1790011833, "end": 1790011899, "date": "2026-09-21"}]
    )
    client = client_for_day(tmp_path, backend)
    client._camera_index_cache = {
        "source": "old",
        "cameras": [{
            "devId": "camera",
            "name": "Front",
            "online": True,
            "clips": [{"start": 1789963078, "end": 1789963115, "date": "2026-09-20"}],
        }],
    }

    index = client.browse_recordings("camera", date(2026, 9, 21))

    assert backend.queries == [("camera", date(2026, 9, 21))]
    camera = index["cameras"][0]
    assert "2026-09-21" in camera["catalogDays"]
    assert [clip["date"] for clip in camera["clips"]] == [
        "2026-09-20",
        "2026-09-21",
    ]
    persisted = json.loads((tmp_path / "catalog.json").read_text(encoding="utf-8"))
    assert persisted["index"]["cameras"][0]["clips"] == camera["clips"]

    reloaded = client_for_day(tmp_path, DayCatalogBackend([]))
    reloaded.load_cache()
    assert reloaded.cached_camera_index()["cameras"][0]["clips"] == camera["clips"]


def test_on_demand_day_replaces_old_rows_for_same_day(tmp_path):
    replacement = {
        "start": 1790011901,
        "end": 1790011938,
        "date": "2026-09-21",
    }
    backend = DayCatalogBackend([replacement])
    client = client_for_day(tmp_path, backend)
    client._camera_index_cache = {
        "source": "old",
        "cameras": [{
            "devId": "camera",
            "name": "Front",
            "online": True,
            "clips": [
                {"start": 1790010000, "end": 1790010010, "date": "2026-09-21"},
                {"start": 1789963078, "end": 1789963115, "date": "2026-09-20"},
            ],
        }],
    }

    index = client.browse_recordings("camera", date(2026, 9, 21))

    assert index["cameras"][0]["clips"] == [
        {"start": 1789963078, "end": 1789963115, "date": "2026-09-20"},
        replacement,
    ]


def test_on_demand_day_throttles_repeated_camera_queries(tmp_path):
    backend = DayCatalogBackend(
        [{"start": 1790011833, "end": 1790011899, "date": "2026-09-21"}]
    )
    client = client_for_day(tmp_path, backend)

    first = client.browse_recordings("camera", date(2026, 9, 21))
    second = client.browse_recordings("camera", date(2026, 9, 21))

    assert len(backend.queries) == 1
    assert second["cameras"][0]["clips"] == first["cameras"][0]["clips"]


def test_on_demand_refresh_due_tracks_the_camera_day_cooldown(tmp_path):
    backend = DayCatalogBackend(
        [{"start": 1790011833, "end": 1790011899, "date": "2026-09-21"}]
    )
    client = client_for_day(tmp_path, backend)

    assert client.browse_refresh_due("camera", date(2026, 9, 21)) is True
    client.browse_recordings("camera", date(2026, 9, 21))
    assert client.browse_refresh_due("camera", date(2026, 9, 21)) is False


def test_transient_empty_day_does_not_erase_known_recordings(tmp_path):
    backend = DayCatalogBackend([])
    client = client_for_day(tmp_path, backend)
    known = {"start": 1790011833, "end": 1790011899, "date": "2026-09-21"}
    client._camera_index_cache = {
        "source": "old",
        "cameras": [{
            "devId": "camera",
            "name": "Front",
            "online": True,
            "clips": [known],
        }],
    }

    index = client.browse_recordings("camera", date(2026, 9, 21))

    assert index["cameras"][0]["clips"] == [known]
