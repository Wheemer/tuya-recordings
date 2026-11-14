import asyncio
import json
import threading
import hashlib
import hmac
from importlib import import_module
from types import SimpleNamespace

import pytest

import custom_components.tuya_recordings.lib.native_lan_signaling as lan_module
from custom_components.tuya_recordings.lib.native_lan_signaling import (
    Lan302Client,
    NativeLanSignalError,
)


@pytest.fixture(autouse=True)
def stub_discovery(monkeypatch):
    monkeypatch.setattr(lan_module, "find_lan_device", lambda dev: lan_module.tinytuya.find_device(dev_id=dev))


class FakeDevice:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.version = kwargs.get("version", 3.3)
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

    def _send_receive(self, payload):
        self.sent.append(payload)
        self.cmd_retcode = 0
        return None

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


@pytest.mark.parametrize("version", [3.4, 3.5])
@pytest.mark.parametrize("preceding", [[], [8], [9, 8, 0, 8]])
def test_lan_key_exchange_waits_for_shello_not_unsolicited_reports(monkeypatch, version, preceding):
    """APK ReceiveSHello checks command 4, not the first nonempty payload."""
    tiny = lan_module.tinytuya
    device = tiny.Device("camera", "192.0.2.9", "0123456789abcdef", version=version)
    sent = []
    timeouts = []
    device.socket = SimpleNamespace(settimeout=timeouts.append)
    remote_nonce = b"remote-nonce-123"

    def reply():
        payload = remote_nonce + hmac.new(device.real_local_key, device.local_nonce, hashlib.sha256).digest()
        if version == 3.4:
            payload = tiny.AESCipher(device.real_local_key).encrypt(payload, False)
        return tiny.TuyaMessage(2, 4, 0, payload, 0, True, 0, None)

    incoming = iter([*(lambda command=command: tiny.TuyaMessage(
        1, command, 0, b'{"dps":{"248":true}}', 0, True, 0, None
    ) for command in preceding), reply])

    def send(payload, retries):
        assert retries is None
        sent.append(payload)
        return True

    monkeypatch.setattr(device, "_send_receive_quick", send)
    monkeypatch.setattr(device, "_receive", lambda: next(incoming)())
    assert lan_module._negotiate_lan_session(device)
    assert [payload.cmd for payload in sent] == [3, 5]
    assert device.remote_nonce == remote_nonce
    assert device.local_key != device.real_local_key
    assert len(timeouts) == len(preceding) + 2
    assert all(0 < value <= 10 for value in timeouts)
    device.socket = None


@pytest.mark.parametrize("failure", ["bad_hmac", "bad_crc", "short", "flood", "deadline", "finish_send"])
def test_lan_key_exchange_failure_is_bounded_and_never_finalizes(monkeypatch, failure):
    tiny = lan_module.tinytuya
    device = tiny.Device("camera", "192.0.2.9", "0123456789abcdef", version=3.5)
    device.socket = SimpleNamespace(settimeout=lambda _: None)
    sent = []
    received = []
    finalized = []

    def receive():
        received.append(1)
        payload = b"remote-nonce-123" + hmac.new(device.real_local_key, device.local_nonce, hashlib.sha256).digest()
        if failure == "bad_hmac":
            payload = b"x" * 48
        elif failure == "short":
            payload = b"x"
        return tiny.TuyaMessage(2, 8 if failure == "flood" else 4, 0,
                               payload, 0, failure != "bad_crc", 0, None)

    def send(payload, retries):
        sent.append(payload.cmd)
        return not (failure == "finish_send" and payload.cmd == 5)

    if failure == "deadline":
        ticks = iter([0, 11])
        monkeypatch.setattr(lan_module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(device, "_receive", receive)
    monkeypatch.setattr(device, "_send_receive_quick", send)
    monkeypatch.setattr(device, "_negotiate_session_key_generate_finalize", lambda: finalized.append(1))
    assert not lan_module._negotiate_lan_session(device)
    assert not finalized
    assert len(received) <= 64
    assert sent == ([3, 5] if failure == "finish_send" else [3])
    device.socket = None


@pytest.mark.parametrize("fixed", [False, True])
def test_pinned_key_exchange_before_after_unsolicited_status(monkeypatch, fixed):
    tiny = lan_module.tinytuya
    device = tiny.Device("camera", "192.0.2.9", "0123456789abcdef", version=3.5)
    writes = []
    device.socket = SimpleNamespace(settimeout=lambda _: None, sendall=writes.append)
    monkeypatch.setattr(device, "_get_socket", lambda _: True)
    replies = 0
    def receive():
        nonlocal replies
        replies += 1
        if replies == 1:
            return tiny.TuyaMessage(1, 8, 0, b'{"dps":{"248":1}}', 0, True, 0, None)
        payload = b"remote-nonce-123" + hmac.new(device.real_local_key, device.local_nonce, hashlib.sha256).digest()
        return tiny.TuyaMessage(2, 4, 0, payload, 0, True, 0, None)
    monkeypatch.setattr(device, "_receive", receive)
    negotiate = (lambda: lan_module._negotiate_lan_session(device)) if fixed else device._negotiate_session_key
    assert negotiate() is fixed
    assert [tiny.parse_header(raw).cmd for raw in writes] == ([3, 5] if fixed else [3])
    assert replies == (2 if fixed else 1)
    device.socket = None


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [3.4, 3.5])
@pytest.mark.parametrize("report", [
    {"devId": "camera", "dps": {"110": 1}},
    {"devId": "other", "dps": {"110": 2}},
    {"cid": "child", "dps": {"110": 2}},
    {"cid": "child", "data": {"dps": {"110": 2}}},
    {"dps": {"110": True}},
])
async def test_sd_report_during_key_exchange_is_preserved_and_identity_checked(monkeypatch, version, report):
    tiny = lan_module.tinytuya
    device = tiny.Device("camera", "192.0.2.9", "0123456789abcdef", version=version)
    device.socket = SimpleNamespace(settimeout=lambda _: None)
    replies = 0
    def receive():
        nonlocal replies
        replies += 1
        if replies == 1:
            payload = json.dumps(report).encode()
            command = 8
        else:
            payload = b"remote-nonce-123" + hmac.new(device.real_local_key, device.local_nonce, hashlib.sha256).digest()
            command = 4
        if version == 3.4:
            payload = tiny.AESCipher(device.real_local_key).encrypt(payload, False)
        return tiny.TuyaMessage(replies, command, 0, payload, 0, True, 0, None)
    monkeypatch.setattr(device, "_receive", receive)
    monkeypatch.setattr(device, "_send_receive_quick", lambda *a: True)
    assert lan_module._negotiate_lan_session(device)
    client = Lan302Client(uid="account", device_id="camera", session_id="session",
                          local_key="0123456789abcdef", on_answer=lambda _: None,
                          on_candidate=lambda _: None, on_disconnect=lambda _: None,
                          preconnect=False, security_level=3)
    client._device = device
    queries = []
    def query(*args):
        queries.append(1)
        return {"dps": {"110": 1}}
    monkeypatch.setattr(client, "_send_query", query)
    assert await client.async_query_direct_dp("110") == 1
    assert bool(queries) is (report.get("devId") != "camera")
    assert device._native_lan_early_reports == []
    device.socket = None


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [901, 902, 914, {"Err": "901"}, {"Err": "secret-address"}, OSError("private")])
async def test_lan_connection_failure_diagnostics_are_bounded_and_clear_discovery(monkeypatch, error):
    class Device(FakeDevice):
        def _get_socket(self, renew):
            if isinstance(error, Exception):
                raise error
            return error

    monkeypatch.setattr(lan_module, "tinytuya", FakeTinyTuya)
    monkeypatch.setattr(FakeTinyTuya, "Device", Device)
    Lan302Client._discovery_cache.clear()
    client = Lan302Client(uid="account", device_id="camera", session_id="session",
                          local_key="0123456789abcdef", on_answer=lambda _: None,
                          on_candidate=lambda _: None, on_disconnect=lambda _: None,
                          preconnect=False, security_level=3)
    with pytest.raises((lan_module.NativeLanUnavailableError, OSError)):
        await client.async_connect()
    assert "connect_state=socket_failed" in client.diagnostics
    if isinstance(error, int):
        assert f"connect_error=tinytuya_{error}" in client.diagnostics
    assert "camera" not in Lan302Client._discovery_cache
    assert client._device.retry_limit == 0
    assert not any(secret in client.diagnostics for secret in ("secret-address", "private", "192.0.2.9", "0123456789abcdef"))
    await client.async_close()
    assert client._device is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [0, "0"])
async def test_lan_dp_query_uses_control_frame_and_ignores_ack(monkeypatch, status):
    replies = iter([
        {},
        {"devId": "other", "dps": {"110": 8}},
        {"dps": {"110": True}},
        {"data": {"devId": "camera", "dps": {"110": status}}},
    ])

    class Device(FakeDevice):
        def receive(self):
            return next(replies)

    monkeypatch.setattr(lan_module, "tinytuya", FakeTinyTuya)
    monkeypatch.setattr(FakeTinyTuya, "Device", Device)
    lan = Lan302Client(
        uid="account", device_id="camera", session_id="session", local_key="0123456789abcdef",
        on_answer=lambda _: None, on_candidate=lambda _: None, on_disconnect=lambda _: None,
        preconnect=False, security_level=3,
    )
    await lan.async_connect()
    try:
        assert await asyncio.wait_for(lan.async_query_direct_dp("110"), 1) == 0
        (command, payload), = lan._device.sent
        assert command == 7
        message = json.loads(payload)
        assert message["protocol"] == 5
        assert type(message["t"]) is int
        message = message["data"]
        assert type(message.pop("t")) is int
        assert message == {"devId": "camera", "uid": "account", "dps": {"110": None}}
    finally:
        await lan.async_close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["immediate", "timeout", "retcode"])
async def test_lan_query_checks_delivery_and_keeps_immediate_report(monkeypatch, outcome):
    class Device(FakeDevice):
        def _send_receive(self, payload):
            self.sent.append(payload)
            assert self.timeout == 5.0
            assert self.retry_limit == 0
            self.cmd_retcode = 1 if outcome == "retcode" else 0
            return {"Error": "fixture timeout", "Err": "902"} if outcome == "timeout" else {"dps": {"110": "1"}}

        def send(self, payload):
            pytest.fail("SD query must wait for the LAN delivery response")

        def receive(self):
            pytest.fail("An immediate status report must not be discarded")

    monkeypatch.setattr(lan_module, "tinytuya", FakeTinyTuya)
    monkeypatch.setattr(FakeTinyTuya, "Device", Device)
    lan = Lan302Client(
        uid="account", device_id="camera", session_id="session", local_key="0123456789abcdef",
        on_answer=lambda _: None, on_candidate=lambda _: None, on_disconnect=lambda _: None,
        preconnect=False, security_level=3,
    )
    await lan.async_connect()
    try:
        if outcome == "immediate":
            assert await lan.async_query_direct_dp("110") == 1
        else:
            with pytest.raises(NativeLanSignalError, match="delivery failed"):
                await lan.async_query_direct_dp("110")
        assert len(lan._device.sent) == 1
    finally:
        await lan.async_close()


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


def test_lan_offer_uses_ipc_lan_302_and_apk_lan_header(monkeypatch):
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
    assert command == 32
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
