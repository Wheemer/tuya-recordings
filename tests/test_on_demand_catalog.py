from __future__ import annotations

import json
from datetime import date

import pytest

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
        "2026-09-21",
        "2026-09-20",
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
        replacement,
        {"start": 1789963078, "end": 1789963115, "date": "2026-09-20"},
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


def test_successful_empty_day_removes_remote_only_recordings(tmp_path):
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

    assert index["cameras"][0]["clips"] == []


def test_successful_empty_day_preserves_ready_local_backup(tmp_path, monkeypatch):
    client = client_for_day(tmp_path, DayCatalogBackend([]))
    known = {"start": 1790011833, "end": 1790011899, "date": "2026-09-21"}
    monkeypatch.setattr(client, "clip_ready", lambda *args: True)
    reconciled = client._reconcile_catalog("camera", [], [known], ["2026-09-21"])
    assert reconciled == [{**known, "on_camera": False}]
    assert client._reconcile_catalog("camera", [known], reconciled, ["2026-09-21"]) == [known]


def test_failed_day_does_not_erase_known_recordings(tmp_path):
    import pytest

    class FailingBackend(DayCatalogBackend):
        def recordings_for_day(self, *args):
            raise RuntimeError("transport unavailable")

    client = client_for_day(tmp_path, FailingBackend([]))
    known = {"start": 1790011833, "end": 1790011899, "date": "2026-09-21"}
    client._camera_index_cache = {"cameras": [{
        "devId": "camera", "online": True, "clips": [known],
    }]}
    with pytest.raises(RuntimeError, match="transport unavailable"):
        client.browse_recordings("camera", date(2026, 9, 21))
    assert client._camera_index_cache["cameras"][0]["clips"] == [known]


def test_month_bitmap_is_cached_without_querying_daily_clips(tmp_path):
    class MonthBackend(DayCatalogBackend):
        def recording_days_for_month(self, dev_id, month, *, cancel_event=None):
            self.queries.append((dev_id, month))
            assert not cancel_event.is_set()
            return [date(2025, 1, 2), date(2025, 1, 28)]

    backend = MonthBackend([])
    client = client_for_day(tmp_path, backend)
    client._camera_index_cache = {"cameras": [{"devId": "camera", "clips": []}]}
    month = date(2025, 1, 1)
    client.browse_recording_month("camera", month)
    index = client.browse_recording_month("camera", month)
    assert backend.queries == [("camera", month)]
    assert index["cameras"][0]["recordingMonths"]["2025-01"] == ["2025-01-02", "2025-01-28"]


def test_failed_month_keeps_old_calendar_and_only_applies_short_cooldown(tmp_path, monkeypatch):
    from custom_components.tuya_recordings import client as client_module

    class MonthBackend(DayCatalogBackend):
        def recording_days_for_month(self, dev_id, month, *, cancel_event=None):
            self.queries.append((dev_id, month))
            if len(self.queries) == 1:
                raise RuntimeError("transport unavailable")
            return [date(2025, 1, 12)]

    clock = [100.0]
    monkeypatch.setattr(client_module.time, "monotonic", lambda: clock[0])
    backend = MonthBackend([])
    client = client_for_day(tmp_path, backend)
    camera = {"devId": "camera", "clips": [], "recordingMonths": {"2025-01": ["2025-01-02"]}}
    client._camera_index_cache = {"cameras": [camera]}
    month = date(2025, 1, 1)
    with pytest.raises(RuntimeError, match="transport unavailable"):
        client.browse_recording_month("camera", month)
    assert camera["recordingMonths"]["2025-01"] == ["2025-01-02"]
    assert not client.browse_month_refresh_due("camera", month)
    clock[0] += 31
    assert client.browse_month_refresh_due("camera", month)
    client.browse_recording_month("camera", month)
    assert camera["recordingMonths"]["2025-01"] == ["2025-01-12"]
    assert not client.browse_month_refresh_due("camera", month)
