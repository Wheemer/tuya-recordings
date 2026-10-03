"""Error raised when the account credentials are rejected."""

from __future__ import annotations

from .tuya_ipc_p2p_error import TuyaIpcP2pError


class TuyaIpcP2pAuthenticationError(TuyaIpcP2pError):
    """The email, the password or the country code do not identify an account."""
