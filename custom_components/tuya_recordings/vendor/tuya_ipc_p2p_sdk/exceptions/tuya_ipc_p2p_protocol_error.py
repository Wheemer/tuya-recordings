"""Error raised when a peer sends something the protocol does not allow."""

from __future__ import annotations

from .tuya_ipc_p2p_error import TuyaIpcP2pError


class TuyaIpcP2pProtocolError(TuyaIpcP2pError):
    """A frame, a record or a payload that does not decode."""
