from __future__ import annotations

import asyncio
import threading
from datetime import date

from custom_components.tuya_recordings.http import TuyaRecordingsPanelDataView


class FakeHass:
    async def async_add_executor_job(self, target, *args):
        return await asyncio.to_thread(target, *args)

    def async_create_task(self, coroutine):
        return asyncio.create_task(coroutine)


class RefreshClient:
    def __init__(self):
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.due = True

    def browse_refresh_due(self, _dev_id, _day):
        return self.due

    def browse_recordings(self, _dev_id, _day):
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=2)


def test_panel_catalog_refresh_is_deduplicated_and_nonblocking():
    async def run():
        client = RefreshClient()
        view = TuyaRecordingsPanelDataView(FakeHass())
        day = date(2026, 9, 22)
        index = {"cameras": [{"devId": "camera"}]}

        assert view._schedule_catalog_refresh(client, "camera", day, index) is True
        assert view._schedule_catalog_refresh(client, "camera", day, index) is True
        await asyncio.to_thread(client.started.wait, 1)
        assert client.calls == 1

        client.release.set()
        await asyncio.gather(*view._catalog_refresh_tasks.values())

    asyncio.run(run())


def test_panel_catalog_refresh_respects_camera_cooldown():
    async def run():
        client = RefreshClient()
        client.due = False
        view = TuyaRecordingsPanelDataView(FakeHass())

        assert view._schedule_catalog_refresh(
            client,
            "camera",
            date(2026, 9, 22),
            {"cameras": [{"devId": "camera"}]},
        ) is False
        assert client.calls == 0

    asyncio.run(run())
