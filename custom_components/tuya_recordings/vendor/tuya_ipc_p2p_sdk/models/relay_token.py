"""The TCP relay token a config fetch mints."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..exceptions import TuyaIpcP2pProtocolError
from ..json_types import JsonObject, JsonValue, require_str, str_list

_DEFAULT_RELAY_PORT = 1443


@dataclass(frozen=True, slots=True)
class RelayToken:
    """
    The relay endpoint, its credential, and the rendezvous id both peers meet on.

    The raw object is kept because the offer carries the token through
    unchanged apart from the rendezvous id, and a field the SDK does not model
    still has to reach the device.
    """

    urls: list[str]
    username: str = field(repr=False)
    credential: str = field(repr=False)
    session_id: str = field(repr=False)
    raw: JsonObject = field(repr=False)

    @classmethod
    def from_json(cls, source: JsonObject) -> RelayToken:
        """Read the relay token out of a ``p2pConfig`` object."""
        urls = str_list(source, "urls") or str_list(source, "urlsEx")
        if not urls:
            raise TuyaIpcP2pProtocolError("Failed to read tcpRelay: no urls")
        return cls(
            urls=urls,
            username=require_str(source, "username"),
            credential=require_str(source, "credential"),
            session_id=require_str(source, "sessionId"),
            raw=dict(source),
        )

    @property
    def expire_timestamp(self) -> str:
        """The ``<ts>`` prefix of the username, which both handshake signatures bind."""
        return self.username.split(":", 1)[0]

    @property
    def endpoint(self) -> tuple[str, int]:
        """The host and port to dial, preferring the IPv4 URL."""
        preferred = next((url for url in self.urls if url.startswith("tcp4:")), self.urls[0])
        without_scheme = preferred.removeprefix("tcp4:").removeprefix("tcp6:")
        host, separator, port = without_scheme.rpartition(":")
        if not separator or not port.isdigit():
            return without_scheme.strip("[]"), _DEFAULT_RELAY_PORT
        return host.strip("[]"), int(port)

    def offered(self, rendezvous_id: str) -> JsonObject:
        """
        Return the token as the offer carries it.

        The app never offers the server's own ``sessionId`` — that one embeds
        the token expiry. It mints a fresh rendezvous id per session and uses
        it both in the offer and in the relay handshake.
        """
        offered: JsonObject = dict(self.raw)
        offered["sessionId"] = rendezvous_id
        return offered

    def with_session_id(self, rendezvous_id: str) -> RelayToken:
        """Return a copy bound to the rendezvous id that was actually offered."""
        return RelayToken(
            urls=self.urls,
            username=self.username,
            credential=self.credential,
            session_id=rendezvous_id,
            raw=self.offered(rendezvous_id),
        )

    def as_json(self) -> JsonValue:
        """Return the raw token, for embedding in a signaling payload."""
        return dict(self.raw)
