"""Error raised when a streaming session cannot be established or is torn down."""

from __future__ import annotations

from .tuya_ipc_p2p_error import TuyaIpcP2pError


class TuyaIpcP2pSessionError(TuyaIpcP2pError):
    """The device refused the session, or it ended before any frame arrived."""
