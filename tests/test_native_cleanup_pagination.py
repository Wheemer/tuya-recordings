"""Offline ownership and encrypted control-wire event pagination regressions."""

import asyncio
import json
import struct
import threading
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest
from test_native_relay_playback import session
from test_native_relay_runtime import APP_SESSION, Playback, camera_config

from custom_components.tuya_recordings.lib import native_relay_runtime as runtime
from custom_components.tuya_recordings.lib.commands import CAMERA_COMMANDS, CameraWorkBusy
from custom_components.tuya_recordings.lib.native_relay_playback import NativeRelayPlaybackError
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.crypto import (
    decrypt_record,
    encrypt_record,
)


@pytest.mark.parametrize("first_mode", ["metadata", "media"])
def test_mode_switch_failed_close_quarantines_until_proven_closed(first_mode):
    transports = []
    permit_close = False

    class Transport(Playback):
        async def close(self):
            if not permit_close:
                raise OSError("mode-switch close failed")
            await super().close()

    def factory(*args):
        transport = Transport()
        transports.append(transport)
        return transport

    worker = runtime.NativeRelayPlaybackWorker(
        camera_config(), APP_SESSION, playback_factory=factory
    )

    def run(mode):
        if mode == "metadata":
            return worker.catalog([date(2026, 11, 3)], None)
        return worker.stream(
            runtime.NativePlaybackRequest(start=100, end=110), lambda _: None, None
        )

    other = "media" if first_mode == "metadata" else "metadata"
    try:
        run(first_mode)
        with pytest.raises(OSError, match="mode-switch close failed"):
            run(other)
        assert worker._unfinished_transport is transports[0]
        with pytest.raises(CameraWorkBusy, match="cleanup"):
            with CAMERA_COMMANDS.slot():
                pytest.fail("quarantined camera admitted a command")
        with pytest.raises(runtime.NativeRelayRuntimeError, match="did not close"):
            run(other)
        with pytest.raises(OSError, match="mode-switch close failed"):
            worker.disconnect()
        assert len(transports) == 1
        assert worker._unfinished_transport is transports[0]
        permit_close = True
        worker.disconnect()
        assert worker._unfinished_transport is None
        with CAMERA_COMMANDS.slot():
            pass
        run(other)
        assert len(transports) == 2
        assert transports[0].closed
    finally:
        permit_close = True
        worker.close()
        assert worker.wait_closed(2)
        CAMERA_COMMANDS.clear_quarantine(worker)


def legacy_payload(page, *, total=5, size=2, count=None, extended=True):
    count = min(size, max(0, total - page * size)) if count is None else count
    offset, stride = (0x64, 0x20) if extended else (0x60, 0x0C)
    payload = bytearray(offset + count * stride)
    struct.pack_into("<III", payload, 0x50, page, total, size)
    struct.pack_into("<I", payload, offset - 4, count)
    for index in range(count):
        start = 100 + (page * size + index) * 20
        if extended:
            struct.pack_into("<IIHH20s", payload, offset + index * stride,
                             start, start + 10, 1, 2, b"event.jpg")
        else:
            struct.pack_into("<HHII", payload, offset + index * stride,
                             1, 2, start, start + 10)
    return bytes(payload)


def wire_session(reply, *, v2=False):
    playback = session()
    playback._native_config = replace(
        playback._native_config, skill=json.dumps({"localStorage": 268435456 if v2 else 0})
    )
    playback._connected = True
    playback._ended = asyncio.get_running_loop().create_future()
    key = bytes(range(16))
    playback._offer = SimpleNamespace(aes_key=key)
    requests = []

    def send(record):
        # Exercise actual record decryption, command framing, reply decoding,
        # request correlation, and the real session's encrypted ingress.
        packet = decrypt_record(key, record)
        _, request_id, control, high, low, length = struct.unpack_from("<IIIHHI", packet)
        assert (control, high, low) == (0, 3, 9 if v2 else 3)
        assert length == (20 if v2 else 84)
        page = 0 if v2 else struct.unpack_from("<I", packet, 100)[0]
        assert playback._control_messages.empty()
        requests.append((request_id, page))
        payload = reply(page)
        if payload is None:
            return
        for response_id in (request_id - 1, request_id):
            response = struct.pack("<IIIHHI", 0x12345678, response_id, 1, 3, low, len(payload)) + payload
            playback._on_control_record(encrypt_record(key, response), key)

    playback._control = SimpleNamespace(send=send)
    return playback, requests


@pytest.mark.asyncio
async def test_cleanup_only_clears_its_own_quarantine():
    owners = [object(), object()]
    workers = [SimpleNamespace(_cleanup_owner=owner) for owner in owners]

    class Transport:
        closed = False

        async def close(self):
            if not self.closed:
                raise OSError("not closed")

    transports = [Transport(), Transport()]
    try:
        for worker, transport in zip(workers, transports, strict=True):
            with pytest.raises(OSError):
                await runtime._close_worker_transport(worker, transport)
        transports[0].closed = True
        await runtime._close_worker_transport(workers[0], transports[0])
        assert workers[0]._unfinished_transport is None
        assert workers[1]._unfinished_transport is transports[1]
        with pytest.raises(CameraWorkBusy, match="cleanup"):
            with CAMERA_COMMANDS.slot():
                pytest.fail("other owner's quarantine was cleared")
        transports[1].closed = True
        await runtime._close_worker_transport(workers[1], transports[1])
        with CAMERA_COMMANDS.slot():
            pass
    finally:
        for owner in owners:
            CAMERA_COMMANDS.clear_quarantine(owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("extended", [False, True])
async def test_event_catalog_realwire_collects_three_serial_pages(extended):
    playback, requests = wire_session(lambda page: legacy_payload(page, extended=extended))
    result = await playback.query_event_recordings(date(2026, 11, 3))
    assert [page for _, page in requests] == [0, 1, 2]
    assert len({request_id for request_id, _ in requests}) == 3
    assert [item.start_time for item in result.items] == [100, 120, 140, 160, 180]
    assert (result.total_pages, result.total_files, result.page) == (1, 5, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("v2", [False, True])
async def test_event_catalog_realwire_empty_and_v2_single_callback(v2):
    playback, requests = wire_session(
        lambda page: b"[]" if v2 else legacy_payload(0, total=0), v2=v2
    )
    result = await playback.query_event_recordings(date(2026, 11, 3))
    assert result.items == ()
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_event_catalog_v2_complete_json_list():
    payload = json.dumps([{"startTime": 100, "endTime": 110, "eventType": 2}]).encode()
    playback, requests = wire_session(lambda _: payload, v2=True)
    assert len((await playback.query_event_recordings(date(2026, 11, 3))).items) == 1
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["repeat", "changed", "empty", "short", "overflow", "limit"])
async def test_event_catalog_realwire_rejects_incomplete_or_unbounded_pages(case):
    def reply(page):
        if case == "limit":
            return legacy_payload(0, total=1025, size=1)
        if case == "overflow":
            return legacy_payload(0, total=1, count=2)
        if page == 0:
            return legacy_payload(0)
        if case == "repeat":
            return legacy_payload(0)
        if case == "changed":
            return legacy_payload(page, total=6)
        if case == "empty":
            return legacy_payload(page, count=0)
        if case == "short":
            return legacy_payload(page, count=1)
        raise AssertionError(case)

    playback, requests = wire_session(reply)
    with pytest.raises(NativeRelayPlaybackError):
        await playback.query_event_recordings(date(2026, 11, 3))
    assert len(requests) <= 3


@pytest.mark.asyncio
async def test_event_catalog_cancellation_sends_no_later_page():
    playback, requests = wire_session(lambda page: legacy_payload(0) if page == 0 else None)
    task = asyncio.create_task(playback.query_event_recordings(date(2026, 11, 3)))
    while len(requests) < 2:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [page for _, page in requests] == [0, 1]


@pytest.mark.asyncio
async def test_event_catalog_realwire_deadline_is_bounded(monkeypatch):
    real_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda _: real_timeout(0.01))
    playback, requests = wire_session(lambda _: None)
    with pytest.raises(TimeoutError):
        await playback.query_event_recordings(date(2026, 11, 3))
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_event_catalog_transport_failure_does_not_return_partial_collection():
    playback, requests = wire_session(lambda page: legacy_payload(0) if page == 0 else None)
    task = asyncio.create_task(playback.query_event_recordings(date(2026, 11, 3)))
    while len(requests) < 2:
        await asyncio.sleep(0)
    playback._ended.set_result(NativeRelayPlaybackError("wire failed"))
    with pytest.raises(NativeRelayPlaybackError, match="wire failed"):
        await task
    assert [page for _, page in requests] == [0, 1]


@pytest.mark.asyncio
async def test_worker_shorter_deadline_cancels_entire_event_collection():
    playback, requests = wire_session(lambda page: legacy_payload(0) if page == 0 else None)
    worker = object.__new__(runtime.NativeRelayPlaybackWorker)
    worker._shutdown_event = threading.Event()
    worker._command_timeout = 0.01
    work = runtime._CatalogWork([date(2026, 11, 3)], None, events=True)
    with pytest.raises(runtime.NativeRelayRuntimeError, match="bounded deadline"):
        await worker._wait_for_work(
            playback.query_event_recordings(date(2026, 11, 3)), work,
            timeout=worker._command_timeout,
        )
    assert [page for _, page in requests] == [0, 1]
    assert work.results is None


@pytest.mark.asyncio
async def test_event_catalog_worker_consumes_completed_realwire_collection():
    playback, requests = wire_session(legacy_payload)

    async def connect(**kwargs):
        pass

    async def close():
        pass

    playback.connect = connect
    playback.close = close
    worker = object.__new__(runtime.NativeRelayPlaybackWorker)
    worker._requests = runtime._WorkerQueue()
    worker._shutdown_event = threading.Event()
    worker._config = worker._app_session = worker._media_secrets = None
    worker._playback_factory = lambda *args: playback
    worker._command_timeout = worker._end_idle_timeout = 1
    catalog = runtime._CatalogWork([date(2026, 11, 3)], None, events=True)
    worker._requests.put(catalog)
    worker._requests.put(runtime._STOP_WORKER)
    await asyncio.wait_for(worker._run(), 3)
    assert catalog.error is None
    assert len(catalog.results[0][1]) == 5
    assert [page for _, page in requests] == [0, 1, 2]
