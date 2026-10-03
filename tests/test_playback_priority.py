from types import SimpleNamespace

from custom_components.tuya_recordings.client import TuyaRecordingsClient
from custom_components.tuya_recordings.const import (
    DATA_INTERACTIVE_PLAYBACK_ACTIVE,
    DOMAIN,
    THUMBNAIL_BACKGROUND_COOLDOWN,
)
from custom_components.tuya_recordings import http as recordings_http


def test_background_cancellation_uses_generations():
    client = TuyaRecordingsClient({})
    running = client.background_work_cancellation()

    client.pause_background_work()

    assert client.background_work_paused is True
    assert running.is_set() is True
    assert client.cloud_activity_paused is False

    client.resume_background_work()

    assert client.background_work_paused is False
    assert running.is_set() is True
    assert client.background_work_cancellation().is_set() is False


def test_playback_pause_clears_queued_work_without_using_master_pause():
    client = TuyaRecordingsClient({})
    entry_data = {
        "client": client,
        recordings_http._DATA_MEDIA_SYNC_PENDING: True,
        recordings_http._DATA_THUMBNAIL_SYNC_PENDING: True,
        recordings_http._DATA_THUMBNAIL_SYNC_LIMIT: 10,
        recordings_http._DATA_THUMBNAIL_SYNC_REQUIRE_MEDIA: False,
    }
    hass = SimpleNamespace(
        data={DOMAIN: {"entry": entry_data}},
        loop=SimpleNamespace(time=lambda: 1000),
    )

    recordings_http._pause_background_camera_work(hass, client, 4)

    assert entry_data[DATA_INTERACTIVE_PLAYBACK_ACTIVE] == 4
    assert entry_data[recordings_http._DATA_MEDIA_SYNC_PENDING] is False
    assert entry_data[recordings_http._DATA_THUMBNAIL_SYNC_PENDING] is False
    assert recordings_http._DATA_THUMBNAIL_SYNC_LIMIT not in entry_data
    assert recordings_http._DATA_THUMBNAIL_SYNC_REQUIRE_MEDIA not in entry_data
    assert entry_data[recordings_http._DATA_THUMBNAIL_SYNC_RETRY_AFTER] == (
        1000 + THUMBNAIL_BACKGROUND_COOLDOWN
    )
    assert client.background_work_paused is True
    assert client.cloud_activity_paused is False


def test_superseded_playback_cannot_resume_background_work():
    client = TuyaRecordingsClient({})
    entry_data = {"client": client}
    domain_data = {
        "entry": entry_data,
        recordings_http._DATA_PLAYBACK_GENERATION: 2,
    }
    hass = SimpleNamespace(data={DOMAIN: domain_data})
    recordings_http._pause_background_camera_work(hass, client, 2)

    recordings_http._resume_background_camera_work(hass, client, 1)

    assert client.background_work_paused is True
    assert entry_data[DATA_INTERACTIVE_PLAYBACK_ACTIVE] == 2

    recordings_http._resume_background_camera_work(hass, client, 2)

    assert client.background_work_paused is False
    assert DATA_INTERACTIVE_PLAYBACK_ACTIVE not in entry_data
