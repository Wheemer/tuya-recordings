import asyncio
import json
import threading
from importlib import import_module
from types import SimpleNamespace

import pytest

import custom_components.tuya_recordings.lib.native_lan_signaling as lan_module
from custom_components.tuya_recordings.lib.native_lan_signaling import (
    Lan302Client,
    NativeLanSignalError,
)


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

    def _get_socket(self, renew):
        assert not renew
        assert self.retry_limit == 1
        return True

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


@pytest.mark.asyncio
async def test_cancelled_offer_cannot_close_socket_under_running_send():
    started, release = threading.Event(), threading.Event()
    events = []

    class BlockingDevice(FakeDevice):
        def send(self, payload):
            started.set()
            assert release.wait(2)
            events.append("sent")

        def close(self):
            events.append("closed")

    client = Lan302Client(
        uid="account", device_id="camera", session_id="session",
        local_key="0123456789abcdef", on_answer=lambda _: None,
        on_candidate=lambda _: None, on_disconnect=lambda _: None,
        preconnect=False, security_level=3,
    )
    client._device = BlockingDevice()
    offer = asyncio.create_task(client.async_send_offer("sdp", [], "trace", {}, {}))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        offer.cancel()
        close = asyncio.create_task(client.async_close())
        await asyncio.sleep(0)
        offer.cancel()
        await asyncio.sleep(0)
        assert not offer.done()
        assert not close.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await offer
    await close
    assert events == ["sent", "closed"]


def test_lan_offer_uses_extended_stream_and_apk_lan_header(monkeypatch):
    monkeypatch.setattr(lan_module, "tinytuya", FakeTinyTuya)
    Lan302Client._discovery_cache.clear()
    answers = []
    client_sent = []
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
        for _ in range(100):
            if answers:
                break
            await asyncio.sleep(0.001)
        client_sent.extend(client._device.sent)
        await client.async_close()

    asyncio.run(run())

    assert answers == ["answer-sdp"]
    command, raw = client_sent[0]
    assert command == 0x40
    payload = json.loads(raw)
    assert payload["header"]["path"] == "lan"
    assert payload["header"]["security_level"] == 4
    assert payload["msg"]["preconnect"] is False


@pytest.mark.parametrize("reachable", [True, False])
def test_pinned_tinytuya_opens_once_and_never_reconnects(monkeypatch, reachable):
    xenon = import_module("tinytuya.core.XenonDevice")
    real_socket = xenon.socket
    attempts = []

    class FixtureSocket:
        def setsockopt(self, *args):
            pass

        def settimeout(self, seconds):
            pass

        def connect(self, address):
            attempts.append(address)
            if not reachable:
                raise ConnectionRefusedError

        def close(self):
            pass

    monkeypatch.setattr(xenon, "socket", SimpleNamespace(
        **{name: getattr(real_socket, name) for name in (
            "AF_INET", "AF_INET6", "SOCK_STREAM", "IPPROTO_TCP", "TCP_NODELAY", "timeout"
        )},
        socket=lambda *args: FixtureSocket(),
    ))
    client = Lan302Client(
        uid="account", device_id="camera", session_id="session",
        local_key="0123456789abcdef", on_answer=lambda _: None,
        on_candidate=lambda _: None, on_disconnect=lambda _: None,
        preconnect=False, security_level=3,
    )
    device = lan_module.tinytuya.Device("camera", "192.0.2.9", "0123456789abcdef", version=3.1)
    try:
        if reachable:
            client._connect_device(device)
            assert device._get_socket(False) is True
        else:
            with pytest.raises(lan_module.NativeLanUnavailableError):
                client._connect_device(device)
        device.close()
        assert device._get_socket(False) is not True
        assert len(attempts) == 1
    finally:
        device.close()


@pytest.mark.asyncio
async def test_tinytuya_error_dict_is_not_an_accepted_offer(monkeypatch):
    class FailedSend(FakeDevice):
        def send(self, payload):
            assert self.retry_limit == 0
            return {"Err": "901", "Error": "Network error", "Payload": None}

    class FailedTinyTuya(FakeTinyTuya):
        Device = FailedSend

    monkeypatch.setattr(lan_module, "tinytuya", FailedTinyTuya)
    client = Lan302Client(
        uid="account", device_id="camera", session_id="session",
        local_key="0123456789abcdef", on_answer=lambda _: None,
        on_candidate=lambda _: None, on_disconnect=lambda _: None,
        preconnect=False, security_level=3,
    )
    await client.async_connect()
    try:
        with pytest.raises(NativeLanSignalError, match="send failed"):
            await client.async_send_offer("sdp", [], "trace", {}, {})
        assert client._reader is None
    finally:
        await client.async_close()


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


def test_lan_reader_accepts_delayed_answer_after_candidate_and_joins_on_close(monkeypatch):
    messages = [None,
        {"header": {"type": "candidate", "sessionid": "session"}, "msg": {"candidate": "candidate"}},
        {"header": {"type": "answer", "sessionid": "other"}, "msg": {"sdp": "wrong"}},
        {"header": {"type": "answer", "sessionid": "session"}, "msg": {"sdp": "delayed"}},
    ]

    class DelayedDevice(FakeDevice):
        def receive(self):
            return messages.pop(0) if messages else None

    class DelayedTinyTuya(FakeTinyTuya):
        Device = DelayedDevice

    monkeypatch.setattr(lan_module, "tinytuya", DelayedTinyTuya)
    Lan302Client._discovery_cache.clear()
    answers, candidates = [], []
    client = Lan302Client(uid="account", device_id="camera", session_id="session",
        local_key="0123456789abcdef", on_answer=answers.append,
        on_candidate=candidates.append, on_disconnect=lambda _: None,
        preconnect=False, security_level=4)

    async def run():
        await client.async_connect()
        device = client._device
        await client.async_send_offer("offer", [], "trace", {}, {})
        async with asyncio.timeout(2):
            while not answers:
                await asyncio.sleep(0.01)
        reader = client._reader
        await client.async_close()
        assert reader.done()
        assert device.closed

    asyncio.run(run())
    assert answers == ["delayed"]
    assert candidates == ["candidate"]
