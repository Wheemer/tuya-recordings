"""Tests for the relay KCP conversation."""

from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport.kcp_conversation import (
    KcpConversation,
)
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport.kcp_segment import (
    CMD_PUSH,
    KcpSegment,
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
