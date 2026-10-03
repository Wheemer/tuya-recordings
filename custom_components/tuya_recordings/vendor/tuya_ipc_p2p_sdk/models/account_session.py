"""The logged-in mobile session every session-scoped call derives from."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class AccountSession:
    """
    The credentials the gateway hands back on a successful login.

    ``sid`` scopes the API calls, ``ecode`` keys the encrypted request bodies
    and the signaling MQTT identity, and ``uid`` names the account inside the
    signaling payloads.
    """

    sid: str = field(repr=False)
    ecode: str = field(repr=False)
    uid: str = field(repr=False)
    device_fingerprint: str = field(repr=False)
