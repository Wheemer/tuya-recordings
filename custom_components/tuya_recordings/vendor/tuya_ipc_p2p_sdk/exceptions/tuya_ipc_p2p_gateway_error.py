"""Error raised when the mobile API gateway rejects a call."""

from __future__ import annotations

from .tuya_ipc_p2p_error import TuyaIpcP2pError

# Codes that mean the login session is no longer usable, so the caller has to
# log in again rather than retry the call.
_SESSION_EXPIRED_CODES: frozenset[str] = frozenset(
    {
        "USER_SESSION_INVALID",
        "SIGN_INVALID",
        "TOKEN_INVALID",
        "NOT_EXISTS_SESSION",
        "USER_SESSION_HAS_EXPIRED",
        "PERMISSION_DENIED",
    }
)


class TuyaIpcP2pGatewayError(TuyaIpcP2pError):
    """The gateway answered, and the answer was a refusal."""

    def __init__(self, api: str, code: str, message: str) -> None:
        """Record which API refused and why."""
        super().__init__(f"Failed to call {api}: {message} ({code})")
        self.api = api
        self.code = code

    @property
    def session_expired(self) -> bool:
        """Whether the refusal means the login session has to be renewed."""
        return self.code in _SESSION_EXPIRED_CODES
