import asyncio
import json

import custom_components.tuya_recordings.lib.native_lan_signaling as lan_module
from custom_components.tuya_recordings.lib.native_lan_signaling import Lan302Client


class FakeDevice:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.sent = []
        self.closed = False

    def set_socketPersistent(self, value):
        self.persistent = value

    def set_socketTimeout(self, value):
        self.timeout = value

    def set_socketRetryLimit(self, value):
        self.retry_limit = value

    def set_retry(self, value):
        self.retry = value

    def send(self, payload):
        self.sent.append(payload)

    def receive(self):
        return {
            "header": {"type": "answer", "sessionid": "session"},
            "msg": {"sdp": "answer-sdp"},
        }

    def close(self):
        self.closed = True


class FakeTinyTuya:
    MessagePayload = staticmethod(lambda command, payload: (command, payload))
    Device = FakeDevice

    @staticmethod
    def find_device(*, dev_id):
        assert dev_id == "camera"
        return {"ip": "192.0.2.9", "version": "3.5"}


def test_lan_offer_uses_extended_stream_and_apk_lan_header(monkeypatch):
    monkeypatch.setattr(lan_module, "tinytuya", FakeTinyTuya)
    Lan302Client._discovery_cache.clear()
    answers = []
    client = Lan302Client(
        uid="account",
        device_id="camera",
        session_id="session",
        local_key="0123456789abcdef",
        on_answer=answers.append,
        on_candidate=lambda _candidate: None,
        on_disconnect=lambda _reason: None,
        preconnect=False,
        security_level=4,
    )

    async def run():
        await client.async_connect()
        await client.async_send_offer("offer-sdp", [], "trace", {}, {})

    asyncio.run(run())

    assert answers == ["answer-sdp"]
    command, raw = client._device.sent[0]
    assert command == 0x40
    payload = json.loads(raw)
    assert payload["header"]["path"] == "lan"
    assert payload["header"]["security_level"] == 4
    assert payload["msg"]["preconnect"] is False


def test_lan_discovery_is_cached_between_sessions(monkeypatch):
    calls = []

    class CountingTinyTuya(FakeTinyTuya):
        @staticmethod
        def find_device(*, dev_id):
            calls.append(dev_id)
            return {"ip": "192.0.2.9", "version": "3.5"}

    monkeypatch.setattr(lan_module, "tinytuya", CountingTinyTuya)
    Lan302Client._discovery_cache.clear()

    def client():
        return Lan302Client(
            uid="account",
            device_id="camera",
            session_id="session",
            local_key="0123456789abcdef",
            on_answer=lambda _sdp: None,
            on_candidate=lambda _candidate: None,
            on_disconnect=lambda _reason: None,
            preconnect=False,
            security_level=3,
        )

    async def run():
        first = client()
        second = client()
        await first.async_connect()
        await second.async_connect()

    asyncio.run(run())
    assert calls == ["camera"]
