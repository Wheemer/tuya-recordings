import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components import tuya_recordings as integration
from custom_components.tuya_recordings.client import TuyaRecordingsClient


@pytest.mark.parametrize("unload_ok", [False, True])
def test_unload_cancels_only_the_successfully_unloaded_entry(unload_ok, monkeypatch):
    client = TuyaRecordingsClient({})
    other = TuyaRecordingsClient({})
    timer = Mock()
    data = {
        "client": client,
        integration.DATA_MEDIA_SYNC_PENDING: True,
        integration.DATA_THUMBNAIL_SYNC_PENDING: True,
        integration.DATA_RECORDING_TRIGGER_TIMER: timer,
    }
    entries = {"entry": data, "other": {"client": other}}

    async def async_add_executor_job(target, *args):
        return target(*args)

    hass = SimpleNamespace(
        data={integration.DOMAIN: entries},
        config_entries=SimpleNamespace(async_unload_platforms=AsyncMock(return_value=unload_ok)),
        async_add_executor_job=async_add_executor_job,
    )
    result = asyncio.run(integration.async_unload_entry(hass, SimpleNamespace(entry_id="entry")))
    assert result is unload_ok
    assert client.cloud_activity_paused is unload_ok
    assert not other.cloud_activity_paused
    assert ("entry" not in entries) is unload_ok
    assert data[integration.DATA_MEDIA_SYNC_PENDING] is not unload_ok
    assert data[integration.DATA_THUMBNAIL_SYNC_PENDING] is not unload_ok
    assert timer.call_count == int(unload_ok)
