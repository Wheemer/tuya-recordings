"""The server-coordinated P2P session a config fetch mints."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..json_types import JsonObject, optional_str, require_str


@dataclass(frozen=True, slots=True)
class P2pSession:
    """
    Session identity and local offer credentials.

    An accepted app token supplies these fields. Native HTTP initialization
    instead generates local ICE/media credentials before posting its SDP.
    """

    session_id: str = field(repr=False)
    aes_key: bytes = field(repr=False)
    ice_ufrag: str = field(repr=False)
    ice_password: str = field(repr=False)
    trace_id: str = field(repr=False)
    uid: str | None = field(repr=False)

    @classmethod
    def from_json(cls, source: JsonObject) -> P2pSession:
        """Read the session out of a ``p2pConfig`` object."""
        return cls(
            session_id=require_str(source, "sessionId"),
            aes_key=bytes.fromhex(require_str(source, "aesKey")),
            ice_ufrag=require_str(source, "iceUfrag"),
            ice_password=require_str(source, "icePassword"),
            trace_id=optional_str(source, "traceId") or "",
            uid=optional_str(source, "uid"),
        )
