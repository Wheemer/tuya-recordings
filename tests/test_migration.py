import asyncio

from custom_components.tuya_recordings import async_migrate_entry


class FakeConfigEntries:
    def __init__(self) -> None:
        self.calls = []

    def async_update_entry(self, entry, **kwargs) -> None:
        self.calls.append((entry, kwargs))


class FakeEntry:
    version = 1
    data = {"server_host": "legacy.example.invalid", "rtsp_port": 8554}


class CurrentEntry:
    version = 2
    data = {"server_host": "legacy.example.invalid"}


def test_migration_removes_stale_rtsp_port():
    hass = type("FakeHass", (), {"config_entries": FakeConfigEntries()})()
    entry = FakeEntry()

    assert asyncio.run(async_migrate_entry(hass, entry))

    assert hass.config_entries.calls == [(entry, {"data": {}, "version": 2})]


def test_migration_removes_stale_website_host_from_current_entries():
    hass = type("FakeHass", (), {"config_entries": FakeConfigEntries()})()
    entry = CurrentEntry()

    assert asyncio.run(async_migrate_entry(hass, entry))

    assert hass.config_entries.calls == [(entry, {"data": {}, "version": 2})]
