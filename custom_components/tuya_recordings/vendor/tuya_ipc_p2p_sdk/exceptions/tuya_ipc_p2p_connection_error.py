"""Error raised when the SDK cannot reach the gateway, the broker or the relay."""

from __future__ import annotations

from .tuya_ipc_p2p_error import TuyaIpcP2pError


class TuyaIpcP2pConnectionError(TuyaIpcP2pError):
    """A timeout, a DNS failure or a transport that dropped."""
