"""One streaming session, end to end: signaling, relay, channel-0 auth, media."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

from .const import CLOSE_REASON_BUSY, LOGGER
from .control import auth_credential, is_control, parse_control, start_sequence
from .crypto import decrypt_record, encrypt_record, random_alphanumeric
from .exceptions import (
    TuyaIpcP2pDeviceBusyError,
    TuyaIpcP2pError,
    TuyaIpcP2pSessionError,
)
from .jpeg_reassembler import JpegReassembler
from .media import extract_media_packets
from .signaling import (
    HandshakeSigner,
    MotoClient,
    SdpAnswer,
    build_offer,
    parse_answer,
    relay_candidate_frame,
    relay_offer_frame,
)
from .transport import IceResponder, RelaySession

if TYPE_CHECKING:
    from collections.abc import Callable

    from .json_types import JsonValue
    from .models import MqttIdentity, RelayToken, StreamConfig
    from .signaling import SdpOffer

_ANSWER_TIMEOUT_SECONDS = 20.0
_VIDEO_TIMEOUT_SECONDS = 20.0
_RELAY_DIAL_ATTEMPTS = 12
_RELAY_DIAL_BACKOFF_SECONDS = 3.0
_DISCONNECT_GRACE_SECONDS = 0.3
_RENDEZVOUS_SUFFIX_LENGTH = 8


class StreamSession:
    """
    Runs one session and ends on transport close, a device disconnect, or close().

    Every asset the session needs is minted per config fetch, so a caller that
    wants to reconnect builds a new session from a freshly fetched config
    rather than restarting this one.
    """

    def __init__(
        self,
        config: StreamConfig,
        identity: MqttIdentity,
        uid: str,
        on_frame: Callable[[bytes], None],
    ) -> None:
        """Bind the session to one config and the callback its frames go to."""
        loop = asyncio.get_running_loop()
        self._config = config
        self._identity = identity
        self._uid = uid
        self._on_frame = on_frame
        self._reassembler = JpegReassembler()
        self._candidates: list[str] = []
        self._candidate_tasks: set[asyncio.Task[None]] = set()
        self._moto: MotoClient | None = None
        self._relay: RelaySession | None = None
        self._ice: IceResponder | None = None
        self._answer: asyncio.Future[SdpAnswer] = loop.create_future()
        self._ended: asyncio.Future[str] = loop.create_future()
        self._answer_key = b""
        self._frames = 0
        self._closing = False

    @property
    def frame_count(self) -> int:
        """How many whole JPEG frames this session has delivered."""
        return self._frames

    async def async_start(self) -> None:
        """Bring the session up to the point where frames are flowing."""
        config = self._config
        p2p = config.p2p_session
        # The app never offers the server's own relay session id — that one
        # embeds the token expiry. It mints a rendezvous id per session and
        # uses it both in the offer and in the relay handshake.
        rendezvous_id = (
            f"{config.device_id}{int(time.time())}{random_alphanumeric(_RENDEZVOUS_SUFFIX_LENGTH)}"
        )
        token = config.relay_token.with_session_id(rendezvous_id)
        trace_id = f"{p2p.trace_id}_{config.device_id}_{int(time.time() * 1000)}"
        offer = build_offer(
            self._uid,
            p2p.session_id,
            int(time.time()),
            p2p.ice_ufrag,
            p2p.ice_password,
            p2p.aes_key,
        )
        LOGGER.debug("Starting session %s (ufrag %s)", p2p.session_id, p2p.ice_ufrag)

        await self._async_connect_signaling(p2p.session_id)
        await self._async_publish_offer(offer, trace_id, token)
        await self._async_gather_candidates(p2p.ice_password)

        answer = await self._async_await_answer()
        self._answer_key = answer.aes_key
        LOGGER.debug("The device answered (ufrag %s)", answer.ice_ufrag)

        self._relay = await self._async_dial_relay(token, offer.aes_key)
        self._relay.set_close_handler(self._on_relay_closed)
        self._tunnel_signaling(offer, trace_id, token)
        self._send_channel_zero(offer.aes_key, answer.aes_key)

        video = await self._relay.async_wait_for_video(_VIDEO_TIMEOUT_SECONDS)
        video.set_message_handler(self._on_media_record)
        LOGGER.debug("The video conversation is up")

    async def _async_connect_signaling(self, session_id: str) -> None:
        """Connect the signaling broker before anything is published on it."""
        self._moto = MotoClient(
            identity=self._identity,
            uid=self._uid,
            device_id=self._config.device_id,
            session_id=session_id,
            local_key=self._config.local_key,
            on_answer=self._on_answer,
            on_candidate=self._on_remote_candidate,
            on_disconnect=self._on_device_disconnect,
        )
        await self._moto.async_connect()

    async def _async_publish_offer(self, offer: SdpOffer, trace_id: str, token: RelayToken) -> None:
        """
        Publish the signal query and then the offer.

        Order matters: the query first, the offer next, candidates only once
        the device has the offer.
        """
        moto = self._require_moto()
        with contextlib.suppress(TuyaIpcP2pError):
            await moto.async_send_sig_query()
        await moto.async_send_offer(
            offer.sdp,
            self._config.ice_servers_as_json(),
            trace_id,
            token.as_json(),
            self._config.log_config_as_json(),
        )

    async def _async_gather_candidates(self, ice_password: str) -> None:
        """Bind the ICE socket and trickle the host candidate it listens on."""
        self._ice = IceResponder(ice_password, self._on_local_candidate)
        await self._ice.async_gather()

    async def _async_await_answer(self) -> SdpAnswer:
        """Wait for the device's answer, or fail the session."""
        try:
            async with asyncio.timeout(_ANSWER_TIMEOUT_SECONDS):
                return await asyncio.shield(self._answer)
        except TimeoutError as exception:
            raise TuyaIpcP2pSessionError(
                "Failed to start the session: the device sent no answer"
            ) from exception

    async def _async_dial_relay(self, token: RelayToken, media_key: bytes) -> RelaySession:
        """
        Connect the relay, retrying while the device settles.

        The device joins the rendezvous only once it has processed the offer,
        so the first attempts legitimately find nobody there.
        """
        signer = HandshakeSigner(
            credential=token.credential,
            expire_timestamp=token.expire_timestamp,
            device_id=self._config.device_id,
            session_id=token.session_id,
            uid=self._uid,
        )
        host, port = token.endpoint
        LOGGER.debug("Connecting the relay %s:%s", host, port)
        last_error: Exception | None = None
        for attempt in range(1, _RELAY_DIAL_ATTEMPTS + 1):
            if self._ended.done():
                raise TuyaIpcP2pSessionError(
                    "Failed to connect the relay: the session ended while dialling"
                )
            relay = RelaySession(token, signer, self._config.device_id, self._uid, media_key)
            try:
                await relay.async_connect()
            except TuyaIpcP2pError as exception:
                last_error = exception
                LOGGER.debug("Relay attempt %s failed: %s", attempt, exception)
                await relay.async_close()
                await asyncio.sleep(_RELAY_DIAL_BACKOFF_SECONDS)
                continue
            return relay
        raise TuyaIpcP2pSessionError(f"Failed to connect the relay: {last_error}")

    def _tunnel_signaling(self, offer: SdpOffer, trace_id: str, token: RelayToken) -> None:
        """
        Send the relay's copy of the offer and the candidates gathered so far.

        This is what binds the relay connection to the media session; without
        it the device joins the rendezvous and never starts streaming.
        """
        relay = self._require_relay()
        ice_servers = self._config.ice_servers_as_json()
        log_config = self._config.log_config_as_json()
        tcp_token: JsonValue = token.as_json()
        relay.send_tunnel_frame(
            offer.aes_key,
            relay_offer_frame(
                self._uid,
                self._config.device_id,
                offer.session_id,
                trace_id,
                offer.sdp,
                ice_servers,
                tcp_token,
                log_config,
            ),
        )
        for candidate in self._candidates:
            relay.send_tunnel_frame(
                offer.aes_key,
                relay_candidate_frame(
                    self._uid,
                    self._config.device_id,
                    offer.session_id,
                    trace_id,
                    candidate,
                ),
            )
        LOGGER.debug("Tunnelled the offer and %s candidates", len(self._candidates))

    def _send_channel_zero(self, send_key: bytes, receive_key: bytes) -> None:
        """Authenticate on channel 0 and send the start burst."""
        relay = self._require_relay()
        credential = auth_credential(self._config.device_password, self._config.local_key)
        relay.control.set_message_handler(
            lambda record: self._on_control_record(record, receive_key)
        )
        for packet in start_sequence(credential):
            relay.control.send(encrypt_record(send_key, packet))

    def _on_control_record(self, record: bytes, receive_key: bytes) -> None:
        """Drain the device's control replies so the conversation keeps acknowledging."""
        try:
            packet = parse_control(decrypt_record(receive_key, record))
        except TuyaIpcP2pError:
            return
        if packet is not None:
            LOGGER.debug(
                "Channel-0 reply type=0x%x flag=%s sub=0x%x",
                packet.type,
                packet.flag,
                packet.sub_command,
            )

    def _on_media_record(self, record: bytes) -> None:
        """Decrypt one video record and publish whatever frames it completes."""
        try:
            plaintext = decrypt_record(self._answer_key, record)
        except TuyaIpcP2pError:
            return
        if is_control(plaintext):
            return
        for packet in extract_media_packets(plaintext):
            frame = self._reassembler.push(packet)
            if frame is None:
                continue
            self._frames += 1
            if self._frames == 1:
                LOGGER.debug("First JPEG frame (%s bytes)", len(frame))
            self._on_frame(frame)

    def _on_answer(self, sdp: str) -> None:
        """Resolve the answer the start sequence is waiting on."""
        if self._answer.done():
            return
        try:
            self._answer.set_result(parse_answer(sdp))
        except TuyaIpcP2pError as exception:
            self._answer.set_exception(exception)

    def _on_remote_candidate(self, candidate: str) -> None:
        """Note a device candidate; the controlled role never checks against it."""
        LOGGER.debug("Device candidate: %s", candidate.strip())

    def _on_local_candidate(self, candidate: str) -> None:
        """Publish one gathered candidate and keep it for the relay tunnel."""
        self._candidates.append(candidate)
        moto = self._moto
        if moto is None:
            return
        task = asyncio.create_task(self._async_send_candidate(moto, candidate))
        self._candidate_tasks.add(task)
        task.add_done_callback(self._candidate_tasks.discard)

    async def _async_send_candidate(self, moto: MotoClient, candidate: str) -> None:
        """Trickle one candidate, tolerating a broker that has already gone."""
        with contextlib.suppress(TuyaIpcP2pError):
            await moto.async_send_candidate(candidate)

    def _on_device_disconnect(self, close_reason: int) -> None:
        """End the session because the device refused or dropped it."""
        if not self._answer.done():
            message = (
                f"Failed to start the session: the device refused it (close_reason={close_reason})"
            )
            # A busy reply is worth telling apart: on its own it is ordinary,
            # and in a long run it is the device having stopped answering.
            error = (
                TuyaIpcP2pDeviceBusyError(message)
                if close_reason == CLOSE_REASON_BUSY
                else TuyaIpcP2pSessionError(message)
            )
            self._answer.set_exception(error)
        self._finish(f"device disconnect, close_reason={close_reason}")

    def _on_relay_closed(self, error: Exception | None) -> None:
        """End the session because its relay connection went away."""
        self._finish(f"relay closed: {error}" if error else "relay closed")

    def _finish(self, reason: str) -> None:
        """Record why the session ended, once."""
        if not self._ended.done():
            self._ended.set_result(reason)

    async def async_wait_closed(self) -> str:
        """Wait until the session ends and return why."""
        return await asyncio.shield(self._ended)

    def _require_moto(self) -> MotoClient:
        """Return the signaling client, or fail loudly if the order was broken."""
        if self._moto is None:
            raise TuyaIpcP2pSessionError("Failed to publish: the session has no signaling client")
        return self._moto

    def _require_relay(self) -> RelaySession:
        """Return the relay session, or fail loudly if the order was broken."""
        if self._relay is None:
            raise TuyaIpcP2pSessionError("Failed to send: the session has no relay")
        return self._relay

    async def async_close(self) -> None:
        """
        Tear the session down and release the device.

        The disconnect is what lets the device drop its own session; without it
        it holds the session and refuses the next offer until its timer fires.
        """
        if self._closing:
            return
        self._closing = True
        self._finish("closed")
        for task in list(self._candidate_tasks):
            task.cancel()
        self._candidate_tasks.clear()
        if not self._answer.done():
            self._answer.cancel()
        if self._ice is not None:
            self._ice.close()
            self._ice = None
        if self._relay is not None:
            await self._relay.async_close()
            self._relay = None
        moto = self._moto
        self._moto = None
        if moto is not None:
            with contextlib.suppress(TuyaIpcP2pError):
                await moto.async_send_disconnect()
            await asyncio.sleep(_DISCONNECT_GRACE_SECONDS)
            await moto.async_close()
