"""Tests for the LAN-only KCP session."""

import asyncio

from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport import (
    DirectSession,
    build_segment,
)
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport.kcp_segment import (
    CMD_PUSH,
)


def _push(conversation: int, sequence: int, payload: bytes) -> bytes:
    return build_segment(
        conversation,
        CMD_PUSH,
        0,
        512,
        sequence,
        sequence,
        0,
        payload,
    )


def test_routes_direct_control_and_media_without_relay_connection() -> None:
    transmitted: list[bytes] = []
    received: list[bytes] = []

    async def exercise() -> None:
        session = DirectSession(transmitted.append)
        session.control.set_message_handler(received.append)
        session.input_segment(_push(0, 0, b"control"))
        session.input_segment(_push(1, 0, b"video"))

        video = await session.async_wait_for_video(0.1)
        video.set_message_handler(received.append)
        video.send(b"acknowledge-me")

        assert received == [b"control", b"video"]
        assert transmitted
        assert "0:0x51=1" in session.diagnostics
        assert "1:0x51=1" in session.diagnostics
        await session.async_close()

    asyncio.run(exercise())


def test_rejects_malformed_direct_datagram() -> None:
    async def exercise() -> None:
        session = DirectSession(lambda _segment: None)
        session.input_segment(b"not-kcp")
        assert "malformed_datagrams=1" in session.diagnostics
        await session.async_close()

    asyncio.run(exercise())
