import asyncio
import json

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


class FakeConfig:
    def __init__(self, root) -> None:
        self.root = root

    def path(self, name: str) -> str:
        return str(self.root / name)


class MigrationHass:
    def __init__(self, root) -> None:
        self.config = FakeConfig(root)
        self.config_entries = FakeConfigEntries()

    async def async_add_executor_job(self, target, *args):
        return target(*args)


def test_migration_removes_stale_rtsp_port():
    hass = type("FakeHass", (), {"config_entries": FakeConfigEntries()})()
    entry = FakeEntry()

    assert asyncio.run(async_migrate_entry(hass, entry))

    assert hass.config_entries.calls == [(entry, {"data": {}, "version": 5})]


def test_migration_removes_stale_website_host_from_current_entries():
    hass = type("FakeHass", (), {"config_entries": FakeConfigEntries()})()
    entry = CurrentEntry()

    assert asyncio.run(async_migrate_entry(hass, entry))

    assert hass.config_entries.calls == [(entry, {"data": {}, "version": 5})]


def test_version_four_protocol_inference_is_removed():
    hass = type("FakeHass", (), {"config_entries": FakeConfigEntries()})()
    entry = type(
        "VersionFourEntry",
        (),
        {"version": 4, "data": {"device_protocol_versions": {"camera": "2.3"}}},
    )()

    assert asyncio.run(async_migrate_entry(hass, entry))

    assert hass.config_entries.calls == [(entry, {"data": {}, "version": 5})]


def test_migration_imports_saved_native_session_once(tmp_path):
    session = {
        "sid": "sid",
        "ecode": "ecode",
        "uid": "uid",
        "partner_identity": "partner",
        "mobile_mqtts_url": "mqtts://example.invalid:8883",
        "device_fingerprint": "fingerprint",
    }
    payload = {
        "native_app_session": session,
        "device_local_keys": {"camera": "local-key"},
        "device_protocol_versions": {"camera": "2.3"},
    }
    import_path = tmp_path / ".tuya_recordings_native_session_import.json"
    import_path.write_text(json.dumps(payload), encoding="utf-8")
    hass = MigrationHass(tmp_path)
    entry = type("LegacyEntry", (), {"version": 2, "data": {"region": "us"}})()

    assert asyncio.run(async_migrate_entry(hass, entry))

    assert hass.config_entries.calls == [
        (entry, {"data": {"region": "us", **payload}, "version": 5})
    ]
    assert not import_path.exists()
