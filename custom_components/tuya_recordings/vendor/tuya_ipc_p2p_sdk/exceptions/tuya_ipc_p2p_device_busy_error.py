"""Error raised when the device answers an offer with its busy reply."""

from __future__ import annotations

from .tuya_ipc_p2p_session_error import TuyaIpcP2pSessionError


class TuyaIpcP2pDeviceBusyError(TuyaIpcP2pSessionError):
    """
    The device says it is still holding a previous session.

    A single one of these is ordinary — offering again too soon after a
    teardown earns one, and the next attempt succeeds. A run of them that
    never ends is the device having stopped answering altogether, which it
    does after a dozen back-to-back attempts and stays in until it is power
    cycled.
    """
