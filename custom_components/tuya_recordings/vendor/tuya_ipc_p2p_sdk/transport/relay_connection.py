"""The TCP relay connection: handshake, keepalive, and tagged media frames."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from dataclasses import replace
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
    decode_media_frame,
    keepalive_frame,
    media_frame,
    parse_handshake_frame,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..models import RelayToken
    from ..signaling import HandshakeSigner

_CONNECT_TIMEOUT_SECONDS = 10
_HANDSHAKE_TIMEOUT_SECONDS = 10
_KEEPALIVE_INTERVAL_SECONDS = 1.0
_KEEPALIVE_TIMEOUT_SECONDS = 10.0
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
        self._phase = "created"
        self._failure = "none"
        self._read_stage = "none"
        self._read_expected = 0
        self._read_partial = 0
        self._handshake_status: int | None = None
        self._socket_errno: int | None = None
        self._endpoint_attempts = 0
        self._endpoint_results: dict[int, str] = {}
        self._received_attributes = 0
        self._multi_attribute_frames = 0
        self._last_data_attributes = 0
        self._empty_data_frames = 0

    async def async_connect(self) -> None:
        """Race advertised signaling endpoints as IMM's 001bdc5c does."""
        endpoints = self._token.endpoints
        if len(endpoints) > 16:
            raise TuyaIpcP2pProtocolError("Too many signaling endpoints")
        if len(endpoints) == 1:
            try:
                async with asyncio.timeout(_HANDSHAKE_TIMEOUT_SECONDS):
                    await self._async_connect_single()
            except BaseException as error:
                if isinstance(error, TimeoutError):
                    self._failure = "dial_timeout" if self._phase == "dial" else "handshake_timeout"
                await self.async_close()
                raise
        else:
            candidates = [RelayConnection(
                replace(self._token, urls=[f"{host}:{port}"]), self._signer,
                self._device_id, self._uid, self._media_key,
            ) for host, port in endpoints]
            tasks = [asyncio.create_task(candidate._async_connect_single()) for candidate in candidates]
            self._endpoint_attempts = len(tasks)
            winner = None
            last_error = None
            try:
                async with asyncio.timeout(_HANDSHAKE_TIMEOUT_SECONDS):
                    pending = set(tasks)
                    while pending and winner is None:
                        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                        for task in tasks:
                            if task not in done:
                                continue
                            try:
                                task.result()
                            except Exception as error:
                                last_error = error
                            else:
                                winner = candidates[tasks.index(task)]
                                break
                    if winner is None:
                        raise last_error or TuyaIpcP2pConnectionError("No signaling handshake completed")
            except BaseException as error:
                diagnostic = next((candidate for candidate in candidates if candidate._reader is not None), candidates[0])
                self._copy_handshake_diagnostics(diagnostic)
                if isinstance(error, TimeoutError):
                    self._failure = "handshake_timeout"
                raise
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self._endpoint_results = {
                    index: candidate._failure if candidate._failure != "none" else candidate._phase
                    for index, candidate in enumerate(candidates)
                }
                if winner is not None:
                    self._reader, self._writer = winner._reader, winner._writer
                    winner._reader = winner._writer = None
                    self._copy_handshake_diagnostics(winner)
                await asyncio.gather(*(candidate.async_close() for candidate in candidates))
        self._phase = "ready"
        self._reader_task = asyncio.create_task(self._async_read_loop())
        self._keepalive_task = asyncio.create_task(self._async_keepalive_loop())

    def _copy_handshake_diagnostics(self, source: RelayConnection) -> None:
        for name in ("_received_frames", "_decode_failures", "_phase", "_failure",
                     "_read_stage", "_read_expected", "_read_partial", "_handshake_status", "_socket_errno"):
            setattr(self, name, getattr(source, name))

    async def _async_connect_single(self) -> None:
        """Dial the relay, run the handshake, and start carrying media."""
        if len(self._token.credential) < _CREDENTIAL_KEY_LENGTH:
            raise TuyaIpcP2pProtocolError("Failed to dial the relay: credential too short")
        self._phase = "dial"
        try:
            async with asyncio.timeout(_CONNECT_TIMEOUT_SECONDS):
                last_error = None
                for host, port in self._token.endpoints:
                    self._endpoint_attempts += 1
                    try:
                        self._reader, self._writer = await asyncio.open_connection(host, port)
                    except OSError as error:
                        last_error = error
                        continue
                    break
                else:
                    raise last_error or OSError("No usable signaling endpoint")
        except (TimeoutError, OSError) as exception:
            self._socket_errno = getattr(exception, "errno", None)
            self._failure = "dial_timeout" if isinstance(exception, TimeoutError) else "dial_socket_error"
            raise TuyaIpcP2pConnectionError(
                "Failed to dial the signaling endpoints"
            ) from exception
        try:
            self._phase = "handshake"
            async with asyncio.timeout(_HANDSHAKE_TIMEOUT_SECONDS):
                await self._async_handshake()
        except BaseException as error:
            if self._failure == "none":
                self._failure = (
                    "cancelled" if isinstance(error, asyncio.CancelledError) else
                    "handshake_timeout" if isinstance(error, TimeoutError) else "handshake_rejected"
                )
            await self.async_close()
            raise
        self._phase = "ready"

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
        final = await self._async_next_handshake_frame()
        complete = parse_handshake_frame(key, final)
        status = complete.get("statuscode")
        self._handshake_status = status if type(status) is int else None
        if (final[:6] != b"\x00\x01\x00\x02\x00\x03"
                or complete.get("method") != "complete" or self._handshake_status != 200):
            raise TuyaIpcP2pProtocolError("Relay handshake completion rejected")

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
            self._read_stage, self._read_expected = "header", _FRAME_HEADER_LENGTH
            header = await reader.readexactly(_FRAME_HEADER_LENGTH)
            length = int.from_bytes(header[2:4], "big")
            self._read_stage, self._read_expected = "payload", length
            payload = await reader.readexactly(length) if length else b""
        except asyncio.IncompleteReadError as exception:
            self._read_partial = len(exception.partial)
            self._failure = "peer_eof"
            raise TuyaIpcP2pConnectionError("Relay peer closed the stream") from exception
        except (ConnectionError, OSError) as exception:
            self._failure = "socket_error"
            self._socket_errno = exception.errno
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
        loop = asyncio.get_running_loop()
        heartbeat_deadline = loop.time() + _KEEPALIVE_TIMEOUT_SECONDS
        try:
            while not self._closed:
                try:
                    async with asyncio.timeout_at(heartbeat_deadline):
                        magic, payload = await self._async_read_frame()
                except TimeoutError as exception:
                    self._failure = "heartbeat_timeout"
                    raise TuyaIpcP2pConnectionError("Native signaling heartbeat timed out") from exception
                if magic == FRAME_KEEPALIVE:
                    heartbeat_deadline = loop.time() + _KEEPALIVE_TIMEOUT_SECONDS
                    continue
                if magic != FRAME_MEDIA:
                    LOGGER.debug("Discarded relay frame 0x%02x (%s bytes)", magic, len(payload))
                    continue
                try:
                    segment, attributes, data_attributes = decode_media_frame(self._media_key, payload)
                except TuyaIpcP2pProtocolError:
                    self._decode_failures += 1
                    self._failure = "frame_auth_or_length"
                    raise
                self._received_attributes += attributes
                self._multi_attribute_frames += int(attributes > 1)
                self._last_data_attributes = data_attributes
                if not segment:
                    self._empty_data_frames += 1
                    continue
                self._received_segments += 1
                if self._segment_handler is not None:
                    self._segment_handler(segment)
        except asyncio.CancelledError:
            raise
        except Exception as exception:
            error = exception
            if self._failure == "none":
                self._failure = "invalid_frame" if isinstance(exception, TuyaIpcP2pProtocolError) else "reader_error"
        self._phase = (
            "timed_out" if self._failure == "heartbeat_timeout"
            else "peer_closed" if error is not None else "closed"
        )
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
        if self._writer is not None:
            self._writer.close()
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
            f"f6_attributes={self._received_attributes}, multi_attribute_frames={self._multi_attribute_frames}, "
            f"last_data_attributes={self._last_data_attributes}, "
            f"empty_data_frames={self._empty_data_frames}, "
            f"decode_failures={self._decode_failures}, phase={self._phase}, "
            f"failure={self._failure}, read_stage={self._read_stage}, "
            f"read_expected={self._read_expected}, read_partial={self._read_partial}, "
            f"handshake_status={self._handshake_status}, socket_errno={self._socket_errno}"
            f", endpoint_attempts={self._endpoint_attempts}"
            f", endpoint_results={self._endpoint_results or 'none'}"
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
                async with asyncio.timeout(2):
                    await writer.wait_closed()
        self._notify_closed(None)
