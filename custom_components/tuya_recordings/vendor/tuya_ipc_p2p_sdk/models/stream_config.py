"""The streaming configuration one session is built from."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..json_types import (
    JsonObject,
    JsonValue,
    object_list,
    optional_object,
    optional_int,
    optional_str,
    require_object,
)
from .p2p_session import P2pSession
from .relay_token import RelayToken


@dataclass(frozen=True, slots=True)
class StreamConfig:
    """
    One ``m.ipc.v4.rtc.config.get`` result, plus the local key it does not carry.

    Every asset in it is minted per fetch, so a reconnect has to fetch a new
    one: a device does not answer an offer built from a stale session.
    """

    device_id: str
    local_key: str = field(repr=False)
    device_password: str = field(repr=False)
    moto_id: str = field(repr=False)
    p2p_session: P2pSession = field(repr=False)
    relay_token: RelayToken = field(repr=False)
    security_level: int = field(default=3, repr=False)
    ice_servers: list[JsonObject] = field(default_factory=list, repr=False)
    log_config: JsonObject | None = field(default=None, repr=False)

    @classmethod
    def from_json(cls, source: JsonObject, device_id: str, local_key: str) -> StreamConfig:
        """Read a config fetch result, filling in the local key the caller holds."""
        p2p_config = require_object(source, "p2pConfig")
        security_level = optional_int(p2p_config, "securityLevel")
        if security_level not in {2, 3, 4}:
            security_level = optional_int(source, "securityLevel")
        if security_level not in {2, 3, 4}:
            security_level = 3
        return cls(
            device_id=device_id,
            local_key=local_key,
            device_password=optional_str(source, "password") or "",
            moto_id=(
                optional_str(p2p_config, "motoId")
                or optional_str(source, "motoId")
                or ""
            ),
            security_level=security_level,
            p2p_session=P2pSession.from_json(require_object(p2p_config, "session")),
            relay_token=RelayToken.from_json(require_object(p2p_config, "tcpRelay")),
            ice_servers=object_list(p2p_config, "ices"),
            log_config=optional_object(p2p_config, "log"),
        )

    def ice_servers_as_json(self) -> JsonValue:
        """Return the ICE servers as the offer's ``token`` field carries them."""
        return [dict(server) for server in self.ice_servers]

    def log_config_as_json(self) -> JsonValue:
        """Return the log configuration as the offer carries it."""
        return dict(self.log_config) if self.log_config else {}
