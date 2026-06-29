from types import SimpleNamespace

from custom_components.tuya_recordings.const import DOMAIN
from custom_components.tuya_recordings.sensor import TuyaRecordingsClipCountSensor


class FakeClient:
    def cached_camera_index(self):
        return {
            "cameras": [
                {"clips": [{}, {}]},
                {"clips": [{}]},
            ]
        }

    def diagnostics(self):
        return {
            "cache_expires_at": "2099-01-01T00:00:00+00:00",
            "refresh_running": False,
            "media_sync_status": {"state": "idle", "downloaded": 2, "deleted_videos": 1},
        }


def test_clip_count_sensor_counts_cached_clips():
    hass = SimpleNamespace(data={DOMAIN: {"entry": {"client": FakeClient()}}})
    entry = SimpleNamespace(entry_id="entry", title="Account")
    sensor = TuyaRecordingsClipCountSensor(hass, entry)

    assert sensor.native_value == 3
    assert sensor.extra_state_attributes["camera_count"] == 2
    assert sensor.extra_state_attributes["media_sync_state"] == "idle"
    assert sensor.extra_state_attributes["media_sync_downloaded"] == 2
    assert sensor.extra_state_attributes["media_sync_deleted_videos"] == 1
