"""Tests for the relay KCP conversation."""

import asyncio
from dataclasses import replace

import pytest

from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport.kcp_conversation import (
    KcpConversation,
)
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport.kcp_segment import (
    CMD_PUSH,
    CMD_ACK,
    KcpSegment,
    parse_segment,
)


def _push(sequence: int, data: bytes) -> KcpSegment:
    return KcpSegment(
        conversation=1,
        command=CMD_PUSH,
        fragment=0,
        window=512,
        timestamp=sequence,
        sequence=sequence,
        unacknowledged=0,
        data=data,
    )


def test_buffers_messages_until_handler_is_installed() -> None:
    transmitted: list[bytes] = []
    delivered: list[bytes] = []
    conversation = KcpConversation(1, transmitted.append)

    conversation.input(_push(0, b"first"))
    conversation.input(_push(1, b"second"))

    assert delivered == []
    conversation.set_message_handler(delivered.append)
    assert delivered == [b"first", b"second"]

    conversation.input(_push(2, b"third"))
    assert delivered == [b"first", b"second", b"third"]


def test_removing_handler_discards_buffered_messages() -> None:
    delivered: list[bytes] = []
    conversation = KcpConversation(1, lambda _raw: None)

    conversation.input(_push(0, b"stale"))
    conversation.set_message_handler(None)
    conversation.set_message_handler(delivered.append)

    assert delivered == []


def test_close_discards_buffered_messages() -> None:
    delivered: list[bytes] = []
    conversation = KcpConversation(1, lambda _raw: None)

    conversation.input(_push(0, b"stale"))
    conversation.close()
    conversation.set_message_handler(delivered.append)

    assert delivered == []


def test_selective_ack_retains_missing_packet():
    async def run():
        c = KcpConversation(1, lambda _: None)
        try:
            c.send(b"first")
            c.send(b"second")
            c.input(replace(_push(1, b""), command=CMD_ACK))
            assert [item.sequence for item in c._pending] == [0]
        finally:
            await c.async_close()
    asyncio.run(run())


def test_receive_sequence_wraps():
    c = KcpConversation(1, lambda _: None)
    c._next_receive_sequence = 0xFFFFFFFF
    received = []
    c.set_message_handler(received.append)
    c.input(_push(0, b"second"))
    c.input(_push(0xFFFFFFFF, b"first"))
    assert received == [b"first", b"second"]
    assert c.receive_sequence == 1


def test_fragment_countdown_must_be_contiguous():
    c = KcpConversation(1, lambda _: None)
    c.input(replace(_push(0, b"first"), fragment=2))
    with pytest.raises(RuntimeError, match="countdown"):
        c.input(_push(1, b"last"))
    assert c._closed
    assert not c._fragments


def test_zero_window_defers_sends_until_window_opens():
    async def run():
        sent = []
        c = KcpConversation(1, sent.append)
        try:
            c.input(replace(_push(0, b""), command=CMD_ACK, window=0))
            c.send(b"queued")
            assert sent == []
            c.input(replace(_push(100, b""), command=CMD_ACK, window=1))
            assert len(sent) == 1
        finally:
            await c.async_close()
    asyncio.run(run())


@pytest.mark.asyncio
async def test_direct_sdk_retries_preserve_seek_after_eight_lost_transmissions(monkeypatch):
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport.direct_session import DirectSession
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport import kcp_conversation as module
    now = 1000
    monkeypatch.setattr(module, "_now_milliseconds", lambda: now)
    sent, failures = [], []
    session = DirectSession(sent.append, on_failure=failures.append)
    channel = session.control
    try:
        channel.send(b"seek-command")
        for _ in range(8):
            now += 201
            channel._retransmit_once()
        assert len(sent) == 9 and not channel.closed and not failures
        channel.input(replace(_push(0, b""), conversation=0, command=CMD_ACK))
        assert channel.pending_count == 0
        channel.send(b"next-seek")
        channel.input(replace(_push(1, b""), conversation=0, command=CMD_ACK))
        assert channel.pending_count == 0 and not channel.closed
    finally:
        await session.async_close()


@pytest.mark.asyncio
async def test_kcp_dead_link_is_bounded_and_notifies_once(monkeypatch):
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport import kcp_conversation as module
    now = 1000
    monkeypatch.setattr(module, "_now_milliseconds", lambda: now)
    sent, failures = [], []
    channel = KcpConversation(0, sent.append, max_transmits=20, retransmit_interval=0.2, on_failure=failures.append)
    channel.send(b"command")
    for _ in range(30):
        now += 201
        channel._retransmit_once()
    assert channel.closed and len(sent) == 20
    assert failures == ["delivery_exhausted"]
    await channel.async_close()


@pytest.mark.asyncio
async def test_retransmission_refreshes_timestamp_and_cumulative_ack(monkeypatch):
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport import kcp_conversation as module
    now = 1000
    monkeypatch.setattr(module, "_now_milliseconds", lambda: now)
    sent = []
    channel = KcpConversation(0, sent.append)
    try:
        channel.send(b"unchanged-video-control")
        original = parse_segment(sent[0])
        channel.input(replace(_push(0, b"reply"), conversation=0))
        now += 301
        channel._retransmit_once()
        resent = parse_segment(sent[-1])
        assert resent.data == original.data and resent.sequence == original.sequence
        assert resent.timestamp == now and resent.unacknowledged == 1
    finally:
        await channel.async_close()
