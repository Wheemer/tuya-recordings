"""APK legacy-event capability selects the paginated ordinary fragment list."""

import asyncio
from dataclasses import replace
from datetime import date
import json
import struct

import pytest

from test_native_relay_playback import APP_SESSION, camera_config
from custom_components.tuya_recordings.lib.native_relay_playback import NativeRelayPlaybackSession, NativeRelayPlaybackError
from custom_components.tuya_recordings.lib.native_simple import NativeSimpleCommand, NativeSimpleError, decode_paged_catalog_reply


def reply(request, index, total, rows, size=2):
    payload = bytearray(0x60 + len(rows) * 12)
    struct.pack_into("<4I", payload, 0x50, index, total, size, len(rows))
    for i, (start, end) in enumerate(rows):
        struct.pack_into("<III", payload, 0x60 + i * 12, 2 << 16, start, end)
    return NativeSimpleCommand(request, 1, 3, 2, bytes(payload))


@pytest.mark.parametrize("mask,paged", [(0, False), (33554432, True), (268435456, False),
                                       (33554432 | 134217728, True), (134217728, False)])
def test_apk_catalog_route_selection(mask, paged):
    config = replace(camera_config(), skill=json.dumps({"localStorage": mask}))
    assert config.playback_catalog_paginated is paged


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "empty", "unfinished", "wrong_page", "changed_total", "truncated"])
async def test_full_day_page_collection_uses_one_control_session(outcome, caplog):
    config = replace(camera_config(), skill=json.dumps({"localStorage": 33554432}))
    session = NativeRelayPlaybackSession(config, APP_SESSION)
    session._connected = True
    session._control = object()
    session._ended = asyncio.get_running_loop().create_future()
    sent = []

    def send(raw):
        magic, request, control, high, low, length = struct.unpack_from("<IIIHHI", raw)
        assert (magic, control, high, low, length) == (0x12345678, 0, 3, 2, 84)
        assert raw[20:84] == bytes(64)
        channel, year, month, day, index = struct.unpack_from("<5I", raw, 84)
        assert (channel, year, month, day) == (0, 2026, 11, 14)
        assert index == len(sent)
        sent.append(raw)
        total = 0 if outcome == "empty" else 3
        rows = [] if outcome == "empty" else [(100, 110), (120, 130)] if index == 0 else [(140, 150)]
        if outcome == "unfinished" and index == 0:
            rows = [(100, 100), (120, 120)]
        result = reply(request, index + (outcome == "wrong_page"),
                       total + (outcome == "changed_total" and index == 1), rows)
        if outcome == "truncated":
            result = replace(result, payload=result.payload[:-1])
        session._control_messages.put_nowait(result)

    session._send_control = send
    if outcome in {"success", "empty", "unfinished"}:
        result = await session.query_recordings(date(2026, 11, 14))
        assert [item.start_time for item in result.items] == (
            [] if outcome == "empty" else [140] if outcome == "unfinished" else [100, 120, 140])
        assert len(sent) == (1 if outcome == "empty" else 2)
    else:
        with pytest.raises(NativeRelayPlaybackError):
            await session.query_recordings(date(2026, 11, 14))
        assert len(sent) <= 2
    assert not session._playback_started


def test_paged_reply_bounds_and_incomplete_range():
    assert decode_paged_catalog_reply(reply(8, 0, 0, [], size=0), request_id=8).items == ()
    result = decode_paged_catalog_reply(reply(8, 0, 2, [(100, 110), (120, 120)]), request_id=8)
    assert len(result.items) == 1
    bad = reply(8, 0, 1, [(100, 110)])
    with pytest.raises(NativeSimpleError):
        decode_paged_catalog_reply(replace(bad, control=0), request_id=8)
