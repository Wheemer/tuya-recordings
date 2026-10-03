"""The TCP relay connection: handshake, keepalive, and tagged media frames."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from typing import TYPE_CHECKING

from ..const import LOGGER
from ..crypto import random_alphanumeric
from ..exceptions import TuyaIpcP2pConnectionError, TuyaIpcP2pProtocolError
from ..json_types import optional_str
from ..signaling import authorization_field, build_auth_ack, build_auth_request
from .relay_framing import (
    FRAME_HANDSHAKE,
    FRAME_KEEPALIVE,
    FRAME_MEDIA,
    assemble_handshake_frame,
    keepalive_frame,
    media_frame,
    parse_handshake_frame,
    unwrap_media_frame,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..models import RelayToken
    from ..signaling import HandshakeSigner

_CONNECT_TIMEOUT_SECONDS = 10
_HANDSHAKE_TIMEOUT_SECONDS = 10
_KEEPALIVE_INTERVAL_SECONDS = 1.0
_CREDENTIAL_KEY_LENGTH = 16
_CLIENT_RANDOM_LENGTH = 32
_FRAME_HEADER_LENGTH = 4
_HANDSHAKE_REQUEST = 0
_HANDSHAKE_ACK = 2


class RelayConnection:
    """
    A dialled and authenticated relay connection, seen as a stream of KCP segments.

    The handshake authenticates the connection to the relay; it is not what
    releases the video — the channel-0 auth is. Its final message only arrives
    once the peer has joined the same rendezvous, which makes it the readiness
    signal the session waits on.
    """

    def __init__(
        self,
        token: RelayToken,
        signer: HandshakeSigner,
        device_id: str,
        uid: str,
        media_key: bytes,
    ) -> None:
        """Describe the rendezvous to dial; the socket is opened by async_connect."""
        self._token = token
        self._signer = signer
        self._device_id = device_id
        self._uid = uid
        self._media_key = media_key
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._segment_handler: Callable[[bytes], None] | None = None
        self._close_handler: Callable[[Exception | None], None] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._closed = False
        self._received_frames: dict[int, int] = {}
        self._received_segments = 0
        self._decode_failures = 0

    async def async_connect(self) -> None:
        """Dial the relay, run the handshake, and start carrying media."""
        if len(self._token.credential) < _CREDENTIAL_KEY_LENGTH:
            raise TuyaIpcP2pProtocolError("Failed to dial the relay: credential too short")
        host, port = self._token.endpoint
        try:
            async with asyncio.timeout(_CONNECT_TIMEOUT_SECONDS):
                self._reader, self._writer = await asyncio.open_connection(host, port)
        except (TimeoutError, OSError) as exception:
            raise TuyaIpcP2pConnectionError(
                f"Failed to dial the relay {host}:{port}: {exception}"
            ) from exception
        try:
            async with asyncio.timeout(_HANDSHAKE_TIMEOUT_SECONDS):
                await self._async_handshake()
        except BaseException:
            await self.async_close()
            raise
        self._reader_task = asyncio.create_task(self._async_read_loop())
        self._keepalive_task = asyncio.create_task(self._async_keepalive_loop())

    async def _async_handshake(self) -> None:
        """Run the four-message challenge-response."""
        token = self._token
        key = token.credential[:_CREDENTIAL_KEY_LENGTH].encode()
        session_id = token.session_id.encode()
        username = token.username.encode()

        client_random = random_alphanumeric(_CLIENT_RANDOM_LENGTH)
        self._write_frame(
            assemble_handshake_frame(
                _HANDSHAKE_REQUEST,
                key,
                secrets.token_bytes(16),
                session_id,
                username,
                build_auth_request(self._device_id, self._uid, client_random),
            )
        )

        response = parse_handshake_frame(key, await self._async_next_handshake_frame())
        authorization = optional_str(response, "authorization") or ""
        device_signature = authorization_field(authorization, "signature")
        device_random = authorization_field(authorization, "random")
        if self._signer.device_signature(client_random) != device_signature:
            raise TuyaIpcP2pProtocolError("Failed the relay handshake: device signature mismatch")

        self._write_frame(
            assemble_handshake_frame(
                _HANDSHAKE_ACK,
                key,
                secrets.token_bytes(16),
                session_id,
                username,
                build_auth_ack(
                    self._device_id,
                    self._uid,
                    self._signer.ack_signature(device_signature, device_random),
                ),
            )
        )
        await self._async_next_handshake_frame()

    async def _async_next_handshake_frame(self) -> bytes:
        """Read frames until a handshake one arrives, skipping keepalives."""
        while True:
            magic, payload = await self._async_read_frame()
            if magic == FRAME_KEEPALIVE:
                continue
            if magic != FRAME_HANDSHAKE:
                raise TuyaIpcP2pProtocolError(
                    f"Failed the relay handshake: unexpected frame 0x{magic:02x}"
                )
            return payload

    async def _async_read_frame(self) -> tuple[int, bytes]:
        """Read one whole relay frame."""
        reader = self._reader
        if reader is None:
            raise TuyaIpcP2pConnectionError("Failed to read from the relay: not connected")
        try:
            header = await reader.readexactly(_FRAME_HEADER_LENGTH)
            length = int.from_bytes(header[2:4], "big")
            payload = await reader.readexactly(length) if length else b""
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as exception:
            raise TuyaIpcP2pConnectionError(
                f"Failed to read from the relay: {exception}"
            ) from exception
        magic = header[0]
        self._received_frames[magic] = self._received_frames.get(magic, 0) + 1
        return magic, payload

    def _write_frame(self, frame: bytes) -> None:
        """Write one already-assembled frame."""
        if self._closed or self._writer is None:
            return
        self._writer.write(frame)

    def set_segment_handler(self, handler: Callable[[bytes], None] | None) -> None:
        """Install the callback that receives every inbound KCP segment."""
        self._segment_handler = handler

    def set_close_handler(self, handler: Callable[[Exception | None], None] | None) -> None:
        """Install the callback that fires once when the connection ends."""
        self._close_handler = handler

    async def _async_read_loop(self) -> None:
        """Read frames until the connection ends, handing segments to the session."""
        error: Exception | None = None
        try:
            while not self._closed:
                magic, payload = await self._async_read_frame()
                if magic == FRAME_KEEPALIVE:
                    continue
                if magic != FRAME_MEDIA:
                    LOGGER.debug("Discarded relay frame 0x%02x (%s bytes)", magic, len(payload))
                    continue
                try:
                    segment = unwrap_media_frame(self._media_key, payload)
                except TuyaIpcP2pProtocolError:
                    self._decode_failures += 1
                    raise
                self._received_segments += 1
                if self._segment_handler is not None:
                    self._segment_handler(segment)
        except asyncio.CancelledError:
            raise
        except Exception as exception:
            error = exception
        self._notify_closed(error)

    async def _async_keepalive_loop(self) -> None:
        """Send the mandatory keepalive for as long as the connection is up."""
        frame = keepalive_frame()
        try:
            while not self._closed:
                await asyncio.sleep(_KEEPALIVE_INTERVAL_SECONDS)
                self._write_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exception:
            LOGGER.debug("Relay keepalive stopped: %s", exception)

    def write_segment(self, segment: bytes) -> None:
        """Wrap one KCP segment in a tagged f6 frame and send it."""
        self._write_frame(media_frame(self._media_key, segment))

    @property
    def diagnostics(self) -> str:
        """Return non-secret counters for one bounded relay attempt."""
        frames = ",".join(
            f"0x{magic:02x}:{count}" for magic, count in sorted(self._received_frames.items())
        )
        return (
            f"frames={frames or 'none'}, segments={self._received_segments}, "
            f"decode_failures={self._decode_failures}"
        )

    def _notify_closed(self, error: Exception | None) -> None:
        """Fire the close handler exactly once."""
        handler = self._close_handler
        self._close_handler = None
        if handler is not None:
            handler(error)

    async def async_close(self) -> None:
        """Stop the loops and drop the socket."""
        if self._closed:
            return
        self._closed = True
        for task in (self._reader_task, self._keepalive_task):
            if task is not None:
                task.cancel()
        for task in (self._reader_task, self._keepalive_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._reader_task = None
        self._keepalive_task = None
        writer = self._writer
        self._writer = None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        self._notify_closed(None)
