"""APK-native SD playback with cloud signaling and direct LAN media."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from datetime import date
from functools import partial
from typing import Any

from ..vendor.tuya_ipc_p2p_sdk import MqttIdentity, StreamConfig
from ..vendor.tuya_ipc_p2p_sdk.models.p2p_session import P2pSession
from ..vendor.tuya_ipc_p2p_sdk.control import (
    auth_credential,
    session_sequence,
    start_sequence,
)
from ..vendor.tuya_ipc_p2p_sdk.crypto import (
    decrypt_record,
    encrypt_record,
    random_alphanumeric,
)
from ..vendor.tuya_ipc_p2p_sdk.exceptions import TuyaIpcP2pError
from ..vendor.tuya_ipc_p2p_sdk.signaling import (
    MotoClient,
    SdpAnswer,
    SdpOffer,
    build_offer,
    parse_answer,
)
from ..vendor.tuya_ipc_p2p_sdk.transport import (
    DirectSession,
    IceResponder,
    KcpConversation,
)

from .native_app_mqtt import NativeAppMqttConfig
from .native_gateway import NativeGatewayError
from .native_lan_signaling import (
    Lan302Client,
    NativeLanDpReportError,
    NativeLanSignalError,
    NativeLanUnavailableError,
)
from .native_playback_stream import (
    NativePlaybackCodecInfo,
    NativePlaybackStreamFrame,
    NativePlaybackStreamParser,
)
from .commands import CAMERA_COMMANDS
from .native_session import NativeCameraSessionConfig, native_session_api_request
from .native_simple_rtp import (
    NativeH264RtpDepacketizer,
    NativeSimpleMediaParser,
    NativeSimpleRtpError,
    parse_rtp_packet,
    SIMPLE_AUDIO_CODECS,
    simple_audio_payload,
)
from .native_simple import (
    NativeSimpleCommand,
    NativeSimpleCommandDecoder,
    NativeSimpleError,
    decode_catalog_reply,
    decode_event_catalog_reply,
    decode_thumbnail_reply,
    encode_audio_open,
    encode_catalog_request,
    encode_recording_month_request,
    decode_recording_month_reply,
    encode_event_catalog_request,
    encode_playback_start,
    encode_playback_start_fragments,
    encode_playback_pause,
    encode_playback_resume,
    encode_playback_stop,
    encode_preview_stop,
    encode_thumbnail_request,
    playback_stream_id,
)
from .native_v4 import NativeV4CatalogPage

_ANSWER_TIMEOUT_SECONDS = 20.0
# Absolute elapsed thresholds from the APK's native connection-state timer.
_OFFER_RETRY_OFFSETS = (1.5, 3.0, 6.0, 12.0)
_DIRECT_NOMINATION_TIMEOUT_SECONDS = 10.0
_MEDIA_CHANNEL_TIMEOUT_SECONDS = 20.0
_CONTROL_RESPONSE_TIMEOUT_SECONDS = 8.0
_THUMBNAIL_RESPONSE_TIMEOUT_SECONDS = 15.0
_RENDEZVOUS_SUFFIX_LENGTH = 8
_CONTROL_QUEUE_LIMIT = 64
_MEDIA_QUEUE_LIMIT = 256
_VIDEO_CONVERSATION = 1
_AUDIO_CONVERSATION = 2
_VIDEO_RTP_PAYLOAD_TYPE = 96
_DISCONNECT_GRACE_SECONDS = 0.3
_CLOSE_STEP_TIMEOUT_SECONDS = 2.0
_HTTP_TOKEN_TIMEOUT_SECONDS = 15.0
_LOGGER = logging.getLogger(__name__)


def _consume_cleanup_result(task: asyncio.Task[Any]) -> None:
    """Consume completion from a cleanup task that outlived its deadline."""
    with suppress(BaseException):
        task.result()


async def _bounded_cleanup(operation: Any) -> bool:
    """Run one transport cleanup step without allowing it to wedge playback."""
    task = asyncio.ensure_future(operation)
    done, _ = await asyncio.wait({task}, timeout=_CLOSE_STEP_TIMEOUT_SECONDS)
    if task in done:
        await task
        return True
    task.cancel()
    task.add_done_callback(_consume_cleanup_result)
    return False


class NativeRelayPlaybackError(RuntimeError):
    """The native IMM playback session could not complete."""


class NativeRelayPlaybackSession:
    """One bounded Smart Life signaled, direct-LAN SD playback session.

    Construction performs no I/O. ``connect`` follows the APK's LAN-first
    signaling policy, then uses MQTT only when local transport is unavailable.
    A locally reached camera is never sent a second MQTT offer after an
    unsuccessful LAN offer. It authenticates conversation zero and prepares one
    playback conversation. No relay or retry loop exists.
    """

    def __init__(
        self,
        config: NativeCameraSessionConfig,
        app_session: dict[str, Any],
        *,
        call_api: Any = None,
    ) -> None:
        self._native_config = config.validate()
        self._call_api = call_api
        if self._native_config.p2p_type != 4:
            raise NativeRelayPlaybackError(
                "Native relay playback currently supports P2P type 4 cameras"
            )
        self._http_initialized = False
        self._http_trace_id = ""
        self._uid = config.app_uid
        if not self._uid:
            raise NativeRelayPlaybackError("Smart Life account UID is unavailable")
        self._local = self._new_local_session(config.trace_id)
        try:
            self._stream_config = _stream_config(config, self._local)
        except NativeRelayPlaybackError:
            if not self._requires_http_token():
                raise
            self._stream_config = None
        self._mqtt_identity = _mqtt_identity(
            app_session, config.app_region, config.app_profile_id
        )
        self._control_messages: asyncio.Queue[NativeSimpleCommand] = asyncio.Queue(
            maxsize=_CONTROL_QUEUE_LIMIT
        )
        self._media_frames: asyncio.Queue[NativePlaybackStreamFrame] = asyncio.Queue(
            maxsize=_MEDIA_QUEUE_LIMIT
        )
        self._parser = NativePlaybackStreamParser()
        self._control_decoder = NativeSimpleCommandDecoder()
        self._moto: MotoClient | Lan302Client | None = None
        self._status_lan: Lan302Client | None = None
        self._signaling_path = "unconnected"
        self._lan_status = "not_attempted"
        self._offer_status = "not_sent"
        self._retry_offer: tuple[float, Callable[[], Awaitable[None]]] | None = None
        self._offer_retries = 0
        self._answer_status = "not_received"
        self._connect_phase = "not_started"
        self._connect_started: float | None = None
        self._connect_mode = "unknown"
        self._token_route = "not_selected"
        self._on_demand_catalog = False
        self._retry_mqtt_offer = False
        self._wake_status = "not_requested"
        self._sd_status_query = "not_requested"
        self._session: DirectSession | None = None
        self._ice: IceResponder | None = None
        self._control: KcpConversation | None = None
        self._video: KcpConversation | None = None
        self._audio: KcpConversation | None = None
        self._stream_id: int | None = None
        self._raw_media_samples: dict[int, tuple[int, str]] = {}
        self._rtp_parsers = {
            _VIDEO_CONVERSATION: NativeSimpleMediaParser(),
            _AUDIO_CONVERSATION: NativeSimpleMediaParser(),
        }
        self._rtp_packets: dict[tuple[int, int, int], int] = {}
        self._rtp_codec_info: dict[int, int] = {}
        self._media_stream_packets: dict[int, int] = {}
        self._preview_sequences: tuple[int, int] | None = None
        self._h264 = NativeH264RtpDepacketizer()
        self._video_frame_number = 0
        self._audio_frame_number = 0
        self._control_payloads: dict[str, str] = {}
        self._next_request_id = 8
        self._task_id = 2
        self._offer: SdpOffer | None = None
        self._answer: asyncio.Future[SdpAnswer] | None = None
        self._ended: asyncio.Future[NativeRelayPlaybackError] | None = None
        self._candidate_tasks: set[asyncio.Task[None]] = set()
        self._control_records = 0
        self._control_decode_failures = 0
        self._control_commands: dict[str, int] = {}
        self._connected = False
        self._playback_started = False
        self._playback_paused = False
        self._closed = False

    @property
    def local_cid(self) -> str:
        """Return the SDK session identifier used by V4 camera requests."""
        return self._local.session_id

    def _new_local_session(self, trace_id: str) -> P2pSession:
        """Generate connect_v3's local identity independently of the app token."""
        session_id = (
            f"{self._native_config.dev_id}{int(time.time())}"
            f"{random_alphanumeric(_RENDEZVOUS_SUFFIX_LENGTH)}"
        )
        return P2pSession(
            session_id=session_id, aes_key=secrets.token_bytes(16),
            ice_ufrag=random_alphanumeric(4), ice_password=random_alphanumeric(24),
            trace_id=trace_id, uid=self._uid,
        )

    @property
    def transport_diagnostics(self) -> str:
        """Return non-secret diagnostics for the current bounded transaction."""
        session = self._session
        ice = self._ice
        session_status = session.diagnostics if session is not None else "unavailable"
        ice_status = ice.diagnostics if ice is not None else "unavailable"
        config = self._native_config
        _, api_version, _, _ = native_session_api_request(
            config.dev_id, trace_id=config.trace_id, app_profile_id=config.app_profile_id,
        )
        elapsed = (
            "none" if self._connect_started is None
            else f"{time.monotonic() - self._connect_started:.2f}"
        )
        commands = (
            ",".join(
                f"{command}={count}"
                for command, count in sorted(self._control_commands.items())
            )
            or "none"
        )
        samples = (
            ",".join(
                f"{conversation}:{length}"
                for conversation, (length, prefix) in sorted(
                    self._raw_media_samples.items()
                )
            )
            or "none"
        )
        rtp = (
            ",".join(
                f"{conversation}:pt{payload_type}:n{unit_type}={count}"
                for (conversation, payload_type, unit_type), count in sorted(
                    self._rtp_packets.items()
                )
            )
            or "none"
        )
        return (
            f"transport=direct-ice, signaling={self._signaling_path}, relay=disabled, "
            f"connected={self._connected}, "
            f"session=({session_status}), "
            f"ice=({ice_status}), control=(records={self._control_records},"
            f"decode_failures={self._control_decode_failures},commands={commands}), "
            f"media_samples=({samples}), rtp=({rtp}), "
            f"media_stream_packets={self._media_stream_packets or 'none'}, "
            f"sequences=({self._preview_sequences or 'none'}->"
            f"{self._media_sequences()}), "
            f"codec_info={self._rtp_codec_info or 'none'}, "
            f"signaling_details=({getattr(self._moto, 'diagnostics', 'unavailable')}), "
            f"lan={self._lan_status}, offer={self._offer_status}, "
            "offer_delivery=unconfirmed, "
            f"answer={self._answer_status}, "
            f"offer_retries={self._offer_retries}, "
            f"http_initialized={self._http_initialized}, phase={self._connect_phase}, "
            f"mode={self._connect_mode}, elapsed_seconds={elapsed}, "
            f"app_profile={config.app_profile_id}, bootstrap_api_version={api_version}, "
            f"p2p_type={config.p2p_type}, p2p_policy={config.p2p_policy}, "
            f"mqtt_pv={config.mqtt_protocol_version or 'unknown'}, "
            f"preconnect={bool(config.preconnect)}, security_level={config.p2p_security_level}, "
            f"token_route={self._token_route}, "
            f"wake_capability={config.wake_capability}, wake_direct={config.wake_direct}, "
            f"wake_status={self._wake_status}, sd_low_power_wake={config.sd_low_power_wake}, "
            f"sd_status_query={self._sd_status_query}"
        )

    async def connect(
        self, *, media: bool = True, allow_wake: bool = False, retry_offer: bool = False,
    ) -> None:
        """Negotiate LAN-first, then require direct LAN media."""
        if self._closed or self._moto is not None:
            raise NativeRelayPlaybackError("Playback session cannot be connected")
        self._connect_started = time.monotonic()
        self._on_demand_catalog = allow_wake and not media
        self._retry_mqtt_offer = retry_offer
        self._connect_mode = "media" if media else "metadata"
        self._connect_phase = "select_transport"
        loop = asyncio.get_running_loop()
        self._answer = loop.create_future()
        self._ended = loop.create_future()
        if self._native_config.p2p_policy != 1:
            self._lan_status = "not_selected_by_policy"
        if self._stream_config is None:
            if self._native_config.p2p_policy == 1:
                self._lan_status = "bootstrap_unavailable"
            try:
                await self._initialize_http_offer(self._native_config.trace_id)
            except BaseException as error:
                self._annotate_connect_failure(error)
                await self.close()
                raise
        config = self._stream_config
        p2p = config.p2p_session
        rendezvous_id = (
            f"{config.device_id}{int(time.time())}"
            f"{random_alphanumeric(_RENDEZVOUS_SUFFIX_LENGTH)}"
        )
        token = config.relay_token if self._http_initialized else config.relay_token.with_session_id(rendezvous_id)
        trace_id = self._http_trace_id if self._http_initialized else self._native_config.trace_id
        self._offer = self._offer or build_offer(
            self._uid,
            p2p.session_id,
            int(time.time()),
            p2p.ice_ufrag,
            p2p.ice_password,
            p2p.aes_key,
        )
        try:
            await self._open_signaling(
                self._offer.sdp,
                config.ice_servers_as_json(),
                trace_id,
                token.as_json(),
                config.log_config_as_json(),
            )
            config = self._stream_config
            p2p = config.p2p_session
            self._connect_phase = "ice_gather"
            self._ice = IceResponder(
                p2p.ice_password,
                self._on_local_candidate,
                send_key=self._offer.aes_key,
                on_segment=self._on_direct_segment,
                local_ufrag=p2p.ice_ufrag,
            )
            await self._ice.async_gather()
            self._session = DirectSession(self._ice.send_segment)
            self._connect_phase = "wait_answer"
            answer = await self._wait_for_answer()
            if self._ended.done():
                raise self._ended.result()
            self._ice.set_receive_key(answer.aes_key)
            self._connect_phase = "ice_nomination"
            await self._ice.async_wait_for_nomination(
                _DIRECT_NOMINATION_TIMEOUT_SECONDS
            )
            if self._ended.done():
                raise self._ended.result()
            self._connect_phase = "native_control"
            self._control = self._session.control
            self._control.set_message_handler(
                lambda record: self._on_control_record(record, answer.aes_key)
            )
            credential = auth_credential(config.device_password, config.local_key)
            packets = (
                start_sequence(credential) if media else session_sequence(credential)
            )
            for packet in packets:
                self._send_control_plain(packet)

            if media:
                self._connect_phase = "preview_transition"
                # Smart Life establishes preview as task 1, then allocates task 2
                # for playback on the same continuously serviced conversations.
                self._video = await self._session.async_wait_for_video(
                    _MEDIA_CHANNEL_TIMEOUT_SECONDS
                )
                self._audio = self._session.conversation(_AUDIO_CONVERSATION)
                self._preview_sequences = (
                    self._video.receive_sequence,
                    self._audio.receive_sequence,
                )
                # The Smart Life playback model stops its bootstrap preview
                # before StartPlayBack. Tuya keeps RTP sequence and FU-A state
                # continuous across that transition, so parse preview packets
                # now and discard only completed preview frames in start().
                self._video.set_message_handler(
                    lambda record: self._on_rtp_record(
                        _VIDEO_CONVERSATION, record, answer.aes_key
                    )
                )
                self._audio.set_message_handler(
                    lambda record: self._on_rtp_record(
                        _AUDIO_CONVERSATION, record, answer.aes_key
                    )
                )
                await self._stop_preview()
            self._connected = True
            self._connect_phase = "connected"
            _LOGGER.debug("Native SD handshake complete: %s", self.transport_diagnostics)
        except BaseException as error:
            self._annotate_connect_failure(error)
            await self.close()
            raise

    def _annotate_connect_failure(self, error: BaseException) -> None:
        if isinstance(error, Exception) and "signaling_details=" not in str(error):
            error.add_note(f"Native SD handshake: {self.transport_diagnostics}")

    async def _open_signaling(
        self,
        sdp: str,
        ice_servers: Any,
        trace_id: str,
        tcp_token: Any,
        log_config: Any,
    ) -> None:
        """Use the camera-advertised local policy before MQTT fallback."""
        if self._native_config.p2p_policy == 1 and not self._http_initialized:
            lan = Lan302Client(
                uid=self._uid,
                device_id=self._stream_config.device_id,
                session_id=self._stream_config.p2p_session.session_id,
                local_key=self._stream_config.local_key,
                on_answer=self._on_answer,
                on_candidate=self._on_remote_candidate,
                on_disconnect=self._on_disconnect,
                preconnect=bool(self._native_config.preconnect),
                security_level=self._native_config.p2p_security_level,
                moto_id=self._stream_config.moto_id,
            )
            self._moto = lan
            self._signaling_path = "lan"
            self._lan_status = "connecting"
            self._connect_phase = "lan_connect"
            try:
                await lan.async_connect()
                self._lan_status = "socket_open"
                self._offer_status = "sending"
                self._connect_phase = "lan_offer"
                await lan.async_send_offer(sdp, ice_servers, trace_id, tcp_token, log_config)
                self._offer_status = "sent"
                self._token_route = "lan_app_token"
                return
            except NativeLanUnavailableError:
                self._lan_status = "unavailable"
                await self._close_failed_lan(lan)
            except NativeLanSignalError as err:
                self._lan_status = "failed"
                await self._close_failed_lan(lan)
                raise NativeRelayPlaybackError(
                    f"Camera did not complete local P2P signaling ({self.transport_diagnostics})"
                ) from err
        if not self._http_initialized and self._requires_http_token():
            await self._initialize_http_offer(trace_id)
            sdp = self._require_offer().sdp
            ice_servers = self._stream_config.ice_servers_as_json()
            tcp_token = self._stream_config.relay_token.as_json()
            log_config = self._stream_config.log_config_as_json()
        if not self._http_initialized:
            self._token_route = "app_token"
        if self._moto is None:
            await self._connect_mqtt(self._stream_config.p2p_session.session_id)
        if self._ended is not None and self._ended.done():
            raise self._ended.result()
        self._signaling_path = "mqtt"
        self._connect_phase = "mqtt_offer"
        self._offer_status = "sending"
        send_offer = partial(
            self._moto.async_send_offer, sdp, ice_servers, trace_id, tcp_token, log_config
        )
        started = asyncio.get_running_loop().time()
        try:
            await send_offer()
        except TuyaIpcP2pError as err:
            self._offer_status = "publish_failed"
            raise NativeRelayPlaybackError(
                f"Native MQTT offer publish failed ({self.transport_diagnostics})"
            ) from err
        self._offer_status = "published"
        if self._on_demand_catalog or self._retry_mqtt_offer:
            self._retry_offer = (started, send_offer)

    async def _close_failed_lan(self, lan: Lan302Client) -> None:
        if not await _bounded_cleanup(lan.async_close()):
            raise NativeRelayPlaybackError("Native LAN cleanup timed out")
        self._moto = None

    async def _connect_mqtt(self, session_id: str) -> None:
        if not self._native_config.mqtt_protocol_version:
            raise NativeRelayPlaybackError(
                "Camera MQTT protocol version is unknown; refresh the camera inventory"
            )
        self._signaling_path = "mqtt"
        self._connect_phase = "mqtt_subscribe"
        self._moto = MotoClient(
            identity=self._mqtt_identity,
            uid=self._uid,
            device_id=self._native_config.dev_id,
            session_id=session_id,
            local_key=self._native_config.local_key,
            on_answer=self._on_answer,
            on_candidate=self._on_remote_candidate,
            on_disconnect=self._on_disconnect,
            protocol_version=self._native_config.mqtt_protocol_version,
            preconnect=bool(self._native_config.preconnect),
            security_level=self._native_config.p2p_security_level,
            moto_id=self._stream_config.moto_id if self._stream_config else "",
            on_failure=self._on_signaling_failure,
        )
        await self._moto.async_connect()
        await self._maybe_send_wake(self._moto)

    async def _maybe_send_wake(self, moto: MotoClient) -> None:
        config = self._native_config
        if not self._on_demand_catalog:
            return
        if CAMERA_COMMANDS.wake_report_on_cooldown(config.dev_id):
            self._wake_status = "cooldown"
            self._sd_status_query = "report_timeout_cooldown"
            raise NativeRelayPlaybackError(
                f"Camera SD status retry is on cooldown ({self.transport_diagnostics})"
            )
        # Product wake flags belong to the generic connector, not the SD presenter.
        if config.sd_low_power_wake is not True:
            self._wake_status = (
                "sd_low_power_not_eligible" if config.sd_low_power_wake is False
                else "sd_low_power_unconfirmed"
            )
            return
        if not config.wake_direct:
            self._wake_status = "direct_target_unconfirmed"
            return
        if not CAMERA_COMMANDS.reserve_wake(config.dev_id):
            self._wake_status = "cooldown"
            return
        self._connect_phase = "mqtt_wake"
        self._wake_status = "sending"
        try:
            await moto.async_send_wake()
        except TuyaIpcP2pError:
            self._wake_status = "send_failed"
        else:
            self._wake_status = "sent_unconfirmed"
            deadline = asyncio.get_running_loop().time() + 30
            if config.sd_status_lan_dp.isascii() and config.sd_status_lan_dp.isdecimal():
                if await self._try_lan_sd_status(config.sd_status_lan_dp, deadline):
                    return
            dp = config.sd_status_mqtt_dp or config.sd_status_http_dp
            use_mqtt = bool(config.sd_status_mqtt_dp)
            if self._sd_status_query == "lan_send_failed":
                route = config.sd_status_lan_error_route
                if route not in {"mqtt", "http"}:
                    return
                dp = config.sd_status_lan_dp
                use_mqtt = route == "mqtt"
            if not dp.isascii() or not dp.isdecimal():
                if self._sd_status_query != "lan_send_failed":
                    self._sd_status_query = "route_or_schema_unconfirmed"
                return
            self._connect_phase = "mqtt_sd_status" if use_mqtt else "http_sd_status"
            self._sd_status_query = "waiting_for_report"
            try:
                async with asyncio.timeout_at(deadline):
                    if use_mqtt:
                        status = await moto.async_query_direct_dp(dp)
                    else:
                        status = await moto.async_wait_for_dp_report(dp, partial(self._send_http_sd_status, dp))
            except TimeoutError:
                raise self._missing_sd_status_report() from None
            except (TuyaIpcP2pError, NativeGatewayError):
                self._sd_status_query = "send_failed"
            else:
                self._accept_sd_status_report(status)

    def _accept_sd_status_report(self, status: int) -> None:
        # Both playback presenters stop before connecting for these states.
        blocked = {2: "card_error", 4: "formatting", 5: "no_card", 7: "loading", 8: "unavailable"}
        reason = blocked.get(status)
        if reason is None:
            self._sd_status_query = "report_received"
            return
        self._sd_status_query = f"report_{reason}"
        raise NativeRelayPlaybackError(
            f"Camera SD status prevents native playback ({self.transport_diagnostics})"
        )

    def _missing_sd_status_report(self) -> NativeRelayPlaybackError:
        self._sd_status_query = "report_timeout"
        CAMERA_COMMANDS.mark_wake_report_missing(self._native_config.dev_id)
        return NativeRelayPlaybackError(
            f"Camera did not report SD status after wake ({self.transport_diagnostics})"
        )

    async def _try_lan_sd_status(self, dp: str, deadline: float) -> bool:
        """Return false only when LAN could not dispatch the query."""
        config = self._native_config
        lan = Lan302Client(
            uid=self._uid, device_id=config.dev_id, session_id=self.local_cid,
            local_key=config.local_key, on_answer=self._on_answer,
            on_candidate=self._on_remote_candidate, on_disconnect=self._on_disconnect,
            preconnect=False, security_level=config.p2p_security_level,
        )
        self._connect_phase = "lan_sd_status"
        self._sd_status_query = "waiting_for_report"
        self._status_lan = lan
        try:
            async with asyncio.timeout_at(deadline):
                try:
                    await lan.async_connect()
                except (NativeLanUnavailableError, OSError):
                    self._sd_status_query = "lan_unavailable"
                    return False
                status = await lan.async_query_direct_dp(dp)
        except NativeLanDpReportError:
            self._sd_status_query = "lan_report_failed"
        except NativeLanSignalError:
            self._sd_status_query = "lan_send_failed"
            return False
        except TimeoutError:
            raise self._missing_sd_status_report() from None
        else:
            self._accept_sd_status_report(status)
        finally:
            try:
                await lan.async_close()
            except BaseException:
                self._sd_status_query = "lan_cleanup_incomplete"
                raise
            else:
                self._status_lan = None
        return True

    async def _send_http_sd_status(self, dp: str) -> None:
        if not callable(self._call_api):
            raise NativeGatewayError("SD status gateway unavailable")
        device = self._native_config.dev_id
        body = {
            "gwId": device, "devId": device,
            "dps": json.dumps({dp: None}, separators=(",", ":")),
        }
        task = asyncio.create_task(asyncio.to_thread(
            self._call_api, "thing.m.device.dp.publish", "1.0", body, {},
        ))
        try:
            accepted = await asyncio.shield(task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await task
            raise
        if accepted is not True:
            raise NativeGatewayError("SD status query not accepted")

    def _requires_http_token(self) -> bool:
        """Match ordinary connect_v3 token selection, never a timeout retry."""
        return self._http_token_reason() is not None

    def _http_token_reason(self) -> str | None:
        token = self._native_config.p2p_config
        session = token.get("session", {})
        session = session if isinstance(session, dict) else {}
        skill = token.get("skill", session.get("skill", {}))
        cloud = skill.get("cloudP2P", 0) if isinstance(skill, dict) else 0
        expiry = token.get("expire", session.get("expire", 0))
        if type(cloud) is int and cloud != 0:
            return "http_cloud_p2p"
        if type(expiry) is not int or not expiry:
            return "http_missing_expiry"
        if expiry <= int(time.time()):
            return "http_expired_token"
        return None

    async def _initialize_http_offer(self, trace_id: str) -> None:
        """Run native state zero once, before the ordinary MQTT offer state."""
        if self._http_initialized:
            return
        self._token_route = self._http_token_reason() or "http_bootstrap_unavailable"
        self._connect_phase = "http_prepare"
        if not callable(self._call_api):
            raise NativeRelayPlaybackError("Native initial HTTP offer gateway is unavailable")
        device_id = self._native_config.dev_id
        local = self._local
        session_id = local.session_id
        offer = build_offer(self._uid, session_id, int(time.time()),
                            local.ice_ufrag, local.ice_password, local.aes_key)
        message = {
            "header": {
                "from": self._uid, "to": device_id,
                "sessionid": session_id, "moto_id": "",
                "type": "offer", "trace_id": trace_id,
                "is_pre": 0, "p2p_skill": 1635,
            },
            # Discarded/expired app tokens are not installed into state zero.
            # The native builder omits its empty token/relay/log fields.
            "msg": {"sdp": offer.sdp},
        }
        body = {"api": "thing.m.rtc.session.offer_1.0", "devId": device_id,
                "msg": json.dumps(message, separators=(",", ":"))}
        # The app already listens to account MQTT while native HTTP callbacks
        # run. Subscribe first so a fast answer cannot be lost.
        await self._connect_mqtt(session_id)
        if self._ended is not None and self._ended.done():
            raise self._ended.result()
        # Joining cancellation avoids a detached gateway request overlapping
        # the next serialized camera command.
        task = asyncio.create_task(asyncio.to_thread(
            self._call_api, "thing.m.rtc.session.offer", "1.0", body, {}
        ))
        self._signaling_path = "http-init"
        self._connect_phase = "http_offer"
        try:
            async with asyncio.timeout(_HTTP_TOKEN_TIMEOUT_SECONDS):
                result = await asyncio.shield(task)
        except (asyncio.CancelledError, TimeoutError) as error:
            with suppress(Exception):
                await task
            if isinstance(error, TimeoutError):
                raise NativeRelayPlaybackError("Native initial HTTP offer timed out") from error
            raise
        if not isinstance(result, dict) or not isinstance(result.get("p2pConfig"), dict):
            raise NativeRelayPlaybackError("Native HTTP offer returned no P2P configuration")
        token = result["p2pConfig"]
        remote = token.get("session")
        if not isinstance(remote, dict) or remote.get("sessionId") != session_id:
            raise NativeRelayPlaybackError("Native HTTP offer returned a different session")
        if token.get("isCloudSession"):
            raise NativeRelayPlaybackError("Native HTTP offer requires a cloud media session; relay is disabled")
        merged = {**self._native_config.p2p_config, **token,
                  "session": {**self._native_config.p2p_config.get("session", {}), **remote}}
        info = json.loads(self._native_config.config_json)
        info["p2pConfig"] = merged
        updated = replace(self._native_config, token=json.dumps(merged), config_json=json.dumps(info))
        # The local offer already committed these ICE/media credentials.
        self._stream_config = _stream_config(updated, local)
        self._native_config = updated
        self._offer = offer
        self._http_initialized = True
        self._http_trace_id = trace_id
        if isinstance(self._moto, MotoClient):
            self._moto.configure_offer_metadata(
                moto_id=self._stream_config.moto_id,
                security_level=self._native_config.p2p_security_level,
            )

    async def query_recordings(self, day: date) -> NativeV4CatalogPage:
        """Query one recording day on authenticated conversation zero."""
        self._require_connected()
        if self._playback_started:
            raise NativeRelayPlaybackError("Catalog cannot run after playback starts")
        request_id = self._allocate_request_id()
        version = self._native_config.playback_catalog_version
        self._send_control(
            encode_catalog_request(
                request_id=request_id,
                channel=0,
                day=day,
                version=version,
                encrypted=0,
            )
        )
        while True:
            command = await self._receive_or_transport_failure(self._control_messages)
            if command.request_id != request_id:
                continue
            try:
                return decode_catalog_reply(
                    command, request_id=request_id, version=version
                )
            except NativeSimpleError as err:
                raise NativeRelayPlaybackError(
                    f"Camera returned an invalid catalog reply: {err}"
                ) from err

    async def query_recording_days(self, month: date) -> list[date]:
        """Query one month bitmap on the same serialized metadata session."""
        self._require_connected()
        if self._playback_started:
            raise NativeRelayPlaybackError("Recording month cannot be queried during playback")
        request_id = self._allocate_request_id()
        self._send_control(encode_recording_month_request(request_id=request_id, channel=0, month=month))
        while True:
            command = await self._receive_or_transport_failure(self._control_messages)
            if command.request_id == request_id:
                return decode_recording_month_reply(command, request_id=request_id, month=month)

    async def query_event_recordings(self, day: date) -> NativeV4CatalogPage:
        """Query Smart Life's capability-gated event timeline."""
        self._require_connected()
        if self._playback_started:
            raise NativeRelayPlaybackError(
                "Event catalog cannot run after playback starts"
            )
        request_id = self._allocate_request_id()
        self._send_control(
            encode_event_catalog_request(
                request_id=request_id,
                channel=0,
                day=day,
                page=0,
                v2=self._native_config.event_catalog_v2,
            )
        )
        while True:
            command = await self._receive_or_transport_failure(self._control_messages)
            if command.request_id != request_id:
                continue
            try:
                return decode_event_catalog_reply(
                    command,
                    request_id=request_id,
                    v2=self._native_config.event_catalog_v2,
                )
            except NativeSimpleError as err:
                raise NativeRelayPlaybackError(
                    f"Camera returned an invalid event catalog reply: {err}"
                ) from err

    async def download_thumbnail(self, *, start: int, end: int) -> bytes:
        """Download the recording JPEG with Smart Life's current 3/8 command."""
        self._require_connected()
        if self._playback_started:
            raise NativeRelayPlaybackError(
                "Thumbnail cannot run while playback is active"
            )
        request_id = self._allocate_request_id()
        request = encode_thumbnail_request(
            request_id=request_id,
            channel=0,
            start=start,
            end=end,
        )
        self._send_control(request)
        try:
            async with asyncio.timeout(_THUMBNAIL_RESPONSE_TIMEOUT_SECONDS):
                while True:
                    command = await self._receive_or_transport_failure(
                        self._control_messages
                    )
                    if command.request_id != request_id:
                        continue
                    try:
                        return decode_thumbnail_reply(
                            command,
                            request_id=request_id,
                        )
                    except NativeSimpleError as err:
                        raise NativeRelayPlaybackError(
                            "Camera returned an invalid thumbnail reply: "
                            f"{err} ({self.transport_diagnostics})"
                        ) from err
        except TimeoutError as err:
            raise NativeRelayPlaybackError(
                "Camera did not return the recording thumbnail "
                f"({self.transport_diagnostics})"
            ) from err

    async def start(
        self,
        *,
        start: int,
        end: int,
        play_time: int | None = None,
        speed: int = 1,
        encrypted: bool = False,
        fragments_json: str = "",
        play_mode_supported: bool = False,
    ) -> None:
        """Start one SD recording with encoded video and audio enabled."""
        self._require_connected()
        if self._playback_started:
            raise NativeRelayPlaybackError("Playback already started")
        await self._start_or_seek(
            start=start,
            end=end,
            play_time=play_time,
            speed=speed,
            encrypted=encrypted,
            fragments_json=fragments_json,
            play_mode_supported=play_mode_supported,
        )

    async def seek(
        self,
        *,
        start: int,
        end: int,
        play_time: int | None = None,
        speed: int = 1,
        encrypted: bool = False,
        fragments_json: str = "",
        play_mode_supported: bool = False,
    ) -> None:
        """Seek on the existing camera session exactly as StartPlayBack does."""
        self._require_connected()
        if not self._playback_started:
            raise NativeRelayPlaybackError("Playback has not started")
        await self._start_or_seek(
            start=start,
            end=end,
            play_time=play_time,
            speed=speed,
            encrypted=encrypted,
            fragments_json=fragments_json,
            play_mode_supported=play_mode_supported,
        )

    async def _start_or_seek(
        self,
        *,
        start: int,
        end: int,
        play_time: int | None,
        speed: int,
        encrypted: bool,
        fragments_json: str,
        play_mode_supported: bool,
    ) -> None:
        """Issue StartPlayBack without replacing the connected camera object."""
        del speed
        self._reset_media_state()
        request_id = self._allocate_request_id()
        stream_id = playback_stream_id(task_id=self._task_id, request_id=request_id)
        if self._video is None or self._audio is None:
            raise NativeRelayPlaybackError("Native media conversations are unavailable")
        self._stream_id = stream_id
        self._playback_started = True
        self._playback_paused = False
        answer = self._require_answer()
        self._video.set_message_handler(
            lambda record: self._on_rtp_record(
                _VIDEO_CONVERSATION, record, answer.aes_key
            )
        )
        self._audio.set_message_handler(
            lambda record: self._on_rtp_record(
                _AUDIO_CONVERSATION, record, answer.aes_key
            )
        )
        start_command = (100, 20) if encrypted else (7, 21 if play_mode_supported else 0)
        if play_mode_supported and not encrypted:
            start_payload = encode_playback_start_fragments(
                stream_id=stream_id,
                channel=0,
                play_time=start if play_time is None else play_time,
                fragments_json=fragments_json,
            )
        else:
            start_payload = encode_playback_start(
                stream_id=stream_id,
                channel=0,
                start=start,
                end=end,
                play_time=start if play_time is None else play_time,
                encrypted=encrypted,
            )
        self._send_control(start_payload)
        self._send_control(encode_audio_open(stream_id=stream_id, channel=0))
        await self._wait_for_playback_responses(
            stream_id,
            start_command=start_command,
        )

    async def pause(self) -> None:
        """Pause playback on the active SDK stream."""
        self._require_connected()
        if not self._playback_started or self._playback_paused:
            return
        stream_id = self._require_stream_id()
        self._send_control(encode_playback_pause(stream_id=stream_id, channel=0))
        await self._wait_for_control_response(stream_id, (7, 1))
        self._playback_paused = True

    async def resume(self) -> None:
        """Resume playback on the active SDK stream."""
        self._require_connected()
        if not self._playback_started or not self._playback_paused:
            return
        stream_id = self._require_stream_id()
        self._send_control(encode_playback_resume(stream_id=stream_id, channel=0))
        await self._wait_for_control_response(stream_id, (7, 2))
        self._playback_paused = False

    async def receive_frame(self) -> NativePlaybackStreamFrame:
        self._require_connected()
        if not self._playback_started:
            raise NativeRelayPlaybackError("Playback has not started")
        return await self._receive_or_transport_failure(self._media_frames)

    async def stop(self) -> None:
        """Stop active SD playback using the native SDK's ordered commands."""
        if not self._playback_started:
            return
        self._require_connected()
        stream_id = self._stream_id
        if stream_id is None:
            raise NativeRelayPlaybackError("Active playback has no stream ID")
        media_stop, playback_stop = encode_playback_stop(stream_id=stream_id, channel=0)
        self._send_control(media_stop)
        await self._wait_for_control_response(stream_id, (7, 3))
        # The native SDK sends 7/5 only after 7/3 succeeds and does not wait
        # for another response before releasing its playback state.
        self._send_control(playback_stop)
        self._playback_started = False
        self._playback_paused = False
        self._stream_id = None

    def _require_stream_id(self) -> int:
        stream_id = self._stream_id
        if stream_id is None:
            raise NativeRelayPlaybackError("Active playback has no stream ID")
        return stream_id

    async def _stop_preview(self) -> None:
        """Apply Smart Life's StopPreview transition before SD playback."""
        request_id = self._allocate_request_id()
        stream_id = playback_stream_id(task_id=1, request_id=request_id)
        video_stop, audio_stop = encode_preview_stop(
            stream_id=stream_id,
            channel=0,
        )
        self._send_control_plain(video_stop)
        await self._wait_for_control_response(stream_id, (6, 3))
        self._send_control_plain(audio_stop)

    def _reset_media_state(self) -> None:
        """Discard every decoder-bound state before a new playback stream."""
        self._media_frames = asyncio.Queue(maxsize=_MEDIA_QUEUE_LIMIT)
        self._parser = NativePlaybackStreamParser()
        self._rtp_packets.clear()
        self._rtp_codec_info.clear()
        self._media_stream_packets.clear()
        self._raw_media_samples.clear()
        self._h264 = NativeH264RtpDepacketizer()
        self._video_frame_number = 0
        self._audio_frame_number = 0

    async def close(self) -> None:
        """Close every transport once and release the camera-side session."""
        if (self._closed and self._session is None and self._moto is None
                and self._status_lan is None and self._ice is None and not self._candidate_tasks):
            return
        if self._connected and self._playback_started:
            with suppress(Exception):
                await _bounded_cleanup(self.stop())
        self._closed = True
        self._connected = False
        failures = []
        for task in list(self._candidate_tasks):
            task.cancel()
        if self._candidate_tasks:
            try:
                finished = await _bounded_cleanup(
                    asyncio.gather(*self._candidate_tasks, return_exceptions=True)
                )
                if not finished:
                    failures.append("candidate tasks timed out")
            except Exception:
                failures.append("candidate tasks failed")
        self._candidate_tasks = {task for task in self._candidate_tasks if not task.done()}
        if self._answer is not None and not self._answer.done():
            self._answer.cancel()
        elif self._answer is not None and not self._answer.cancelled():
            self._answer.exception()
        if self._ice is not None:
            try:
                self._ice.close()
            except Exception:
                failures.append("ICE close failed")
            else:
                self._ice = None
        session = self._session
        if session is not None:
            try:
                if await _bounded_cleanup(session.async_close()):
                    self._session = None
                else:
                    failures.append("direct session close timed out")
            except Exception:
                failures.append("direct session close failed")
        if self._status_lan is not None:
            try:
                if await _bounded_cleanup(self._status_lan.async_close()):
                    self._status_lan = None
                else:
                    failures.append("LAN status close timed out")
            except Exception:
                failures.append("LAN status close failed")
        moto = self._moto
        if moto is not None:
            # A missing remote disconnect acknowledgement does not imply a
            # leaked local socket; local close below must independently finish.
            with suppress(Exception):
                await _bounded_cleanup(moto.async_send_disconnect())
            await asyncio.sleep(_DISCONNECT_GRACE_SECONDS)
            try:
                if await _bounded_cleanup(moto.async_close()):
                    self._moto = None
                else:
                    failures.append("signaling close timed out")
            except Exception:
                failures.append("signaling close failed")
        self._control = None
        self._video = None
        self._audio = None
        if failures:
            raise NativeRelayPlaybackError(
                "Native transport cleanup incomplete: " + ", ".join(failures)
            )

    async def abort(self) -> None:
        """Drop transports when the camera will not acknowledge playback stop."""
        self._playback_started = False
        self._playback_paused = False
        self._stream_id = None
        await self.close()

    async def _wait_for_answer(self) -> SdpAnswer:
        assert self._answer is not None
        retry_offer, self._retry_offer = self._retry_offer, None
        loop = asyncio.get_running_loop()
        started = retry_offer[0] if retry_offer else loop.time()
        deadline = started + _ANSWER_TIMEOUT_SECONDS
        try:
            async with asyncio.timeout_at(deadline):
                if retry_offer:
                    await self._resend_offer(started, deadline, retry_offer[1])
                return await asyncio.shield(self._answer)
        except TimeoutError as err:
            raise NativeRelayPlaybackError(
                f"Timed out waiting for a usable native IMM answer ({self.transport_diagnostics})"
            ) from err

    async def _resend_offer(
        self, started: float, deadline: float, send_offer: Callable[[], Awaitable[None]]
    ) -> None:
        assert self._answer is not None
        loop = asyncio.get_running_loop()
        for offset in _OFFER_RETRY_OFFSETS:
            retry_at = started + offset
            now = loop.time()
            # Never bunch up missed sends after a slow publish or event-loop stall.
            if retry_at <= now or retry_at >= deadline:
                continue
            await asyncio.wait({self._answer}, timeout=retry_at - now)
            if self._answer.done() or loop.time() >= deadline:
                return
            if self._ended is not None and self._ended.done():
                raise self._ended.result()
            self._offer_retries += 1
            try:
                await send_offer()
            except TuyaIpcP2pError as err:
                self._offer_status = "retry_publish_failed"
                raise NativeRelayPlaybackError(
                    f"Native MQTT offer retry failed ({self.transport_diagnostics})"
                ) from err

    def _on_answer(self, sdp: str) -> None:
        answer = self._answer
        if answer is None or answer.done():
            return
        try:
            answer.set_result(parse_answer(sdp))
        except TuyaIpcP2pError as err:
            self._answer_status = "invalid_sdp"
            answer.set_exception(NativeRelayPlaybackError(
                f"Invalid native IMM answer: {err} ({self.transport_diagnostics})"
            ))
        else:
            self._answer_status = "accepted"

    def _on_local_candidate(self, candidate: str) -> None:
        moto = self._moto
        if moto is None:
            return
        task = asyncio.create_task(self._send_candidate(moto, candidate))
        self._candidate_tasks.add(task)
        task.add_done_callback(self._candidate_tasks.discard)

    async def _send_candidate(self, moto: MotoClient | Lan302Client, candidate: str) -> None:
        try:
            await moto.async_send_candidate(candidate)
        except NativeLanSignalError:
            self._on_signaling_failure("candidate send failed", transport="LAN")
        except TuyaIpcP2pError:
            pass

    def _on_remote_candidate(self, candidate: str) -> None:
        del candidate

    def _on_disconnect(self, close_reason: int) -> None:
        error = NativeRelayPlaybackError(
            f"Camera refused native session (close_reason={close_reason})"
        )
        if self._answer is not None and not self._answer.done():
            self._answer.set_exception(error)
        self._fail_transport(error)

    def _on_signaling_failure(self, reason: str, *, transport: str = "MQTT") -> None:
        error = NativeRelayPlaybackError(f"Native {transport} signaling stopped ({reason})")
        if self._answer is not None and not self._answer.done():
            self._answer.set_exception(error)
        self._fail_transport(error)

    def _on_direct_segment(self, segment: bytes) -> None:
        session = self._session
        if session is not None:
            session.input_segment(segment)

    def _send_control(self, payload: bytes) -> None:
        self._require_connected()
        self._send_control_plain(payload)

    def _send_control_plain(self, payload: bytes) -> None:
        control = self._control
        offer = self._require_offer()
        if control is None:
            raise NativeRelayPlaybackError("Native control conversation is unavailable")
        control.send(encrypt_record(offer.aes_key, payload))

    def _on_control_record(self, record: bytes, receive_key: bytes) -> None:
        self._control_records += 1
        try:
            message = decrypt_record(receive_key, record)
            commands = self._control_decoder.feed(message)
        except (TuyaIpcP2pError, NativeSimpleError):
            self._control_decode_failures += 1
            return
        for command in commands:
            classified = f"{command.high_command}/{command.low_command}"
            self._control_commands[classified] = (
                self._control_commands.get(classified, 0) + 1
            )
            self._control_payloads.setdefault(
                classified,
                command.payload[:64].hex(),
            )
            self._put_nowait(self._control_messages, command, "control")

    def _on_media_record(self, record: bytes, receive_key: bytes) -> None:
        try:
            plaintext = decrypt_record(receive_key, record)
            frames = self._parser.feed(plaintext)
        except (TuyaIpcP2pError, ValueError) as err:
            failure = NativeRelayPlaybackError(
                "Camera sent invalid native playback media"
            )
            self._fail_transport(failure)
            raise failure from err
        for frame in frames:
            self._put_nowait(self._media_frames, frame, "media")

    def _on_rtp_record(
        self, conversation: int, record: bytes, receive_key: bytes
    ) -> None:
        """Decode type-4 exact-read framing and account for its RTP payloads."""
        try:
            plaintext = decrypt_record(receive_key, record)
            packets = self._rtp_parsers[conversation].feed(plaintext)
        except (TuyaIpcP2pError, NativeSimpleRtpError):
            return
        if conversation not in self._raw_media_samples:
            self._raw_media_samples[conversation] = (
                len(plaintext),
                "",
            )
        for packet in packets:
            self._media_stream_packets[packet.stream_id] = (
                self._media_stream_packets.get(packet.stream_id, 0) + 1
            )
            if packet.codec_info:
                self._rtp_codec_info[conversation] = (
                    self._rtp_codec_info.get(conversation, 0) + 1
                )
            try:
                rtp = parse_rtp_packet(packet.payload)
            except NativeSimpleRtpError:
                continue
            unit_type = rtp.payload[0] & 0x1F if rtp.payload else -1
            key = (conversation, rtp.payload_type, unit_type)
            self._rtp_packets[key] = self._rtp_packets.get(key, 0) + 1
            if (
                conversation == _AUDIO_CONVERSATION
                and rtp.payload_type in SIMPLE_AUDIO_CODECS
            ):
                try:
                    audio_payload = simple_audio_payload(rtp)
                except NativeSimpleRtpError as err:
                    self._fail_transport(NativeRelayPlaybackError(str(err)))
                    continue
                sample_rate, channels, bit_width = packet.audio_format_indices
                if sample_rate >= 10 or channels >= 2 or bit_width >= 2:
                    self._fail_transport(NativeRelayPlaybackError("Camera audio format indices are invalid"))
                    continue
                if rtp.payload_type == 99 and bit_width != 1:
                    self._fail_transport(NativeRelayPlaybackError("Native PCM playback requires 16-bit camera samples"))
                    continue
                self._audio_frame_number += 1
                timestamp = packet.camera_timestamp or rtp.timestamp
                self._put_nowait(
                    self._media_frames,
                    NativePlaybackStreamFrame(
                        frame_type=3,
                        has_codec_info=True,
                        payload_length=len(audio_payload),
                        field_8=timestamp >> 32,
                        field_12=timestamp & 0xFFFFFFFF,
                        field_16=0,
                        field_20=0,
                        field_24=0,
                        field_28=self._audio_frame_number,
                        codec_info=NativePlaybackCodecInfo(
                            codec=SIMPLE_AUDIO_CODECS[rtp.payload_type],
                            sample_rate_index=sample_rate,
                            channels_index=channels,
                            bit_width_index=bit_width,
                            field_4=0,
                            field_5=0,
                            raw=b"",
                        ),
                        payload=audio_payload,
                        timestamp_scale=1000 if packet.camera_timestamp else (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 96000)[sample_rate],
                    ),
                    "media",
                )
                continue
            if (
                conversation != _VIDEO_CONVERSATION
                or rtp.payload_type != _VIDEO_RTP_PAYLOAD_TYPE
            ):
                continue
            try:
                access_units = self._h264.push(rtp, presentation_timestamp=packet.camera_timestamp or rtp.timestamp)
            except NativeSimpleRtpError:
                continue
            for access_unit, timestamp in zip(access_units, self._h264.completed_timestamps, strict=True):
                self._video_frame_number += 1
                self._put_nowait(
                    self._media_frames,
                    NativePlaybackStreamFrame(
                        frame_type=1,
                        has_codec_info=False,
                        payload_length=len(access_unit),
                        field_8=timestamp >> 32,
                        field_12=timestamp & 0xFFFFFFFF,
                        field_16=0,
                        field_20=0,
                        field_24=0,
                        field_28=self._video_frame_number,
                        codec_info=None,
                        payload=access_unit,
                        timestamp_scale=1000 if packet.camera_timestamp else 90000,
                    ),
                    "media",
                )

    async def _receive_or_transport_failure(self, queue: asyncio.Queue[Any]) -> Any:
        failure = self._ended
        if failure is None:
            raise NativeRelayPlaybackError("Native IMM transport is unavailable")
        receive_task = asyncio.create_task(queue.get())
        failure_task = asyncio.ensure_future(asyncio.shield(failure))
        try:
            done, _ = await asyncio.wait(
                (receive_task, failure_task), return_when=asyncio.FIRST_COMPLETED
            )
            if failure_task in done:
                raise failure_task.result()
            return receive_task.result()
        finally:
            for task in (receive_task, failure_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(receive_task, failure_task, return_exceptions=True)

    async def _wait_for_playback_responses(
        self, stream_id: int, *, start_command: tuple[int, int] = (7, 0)
    ) -> None:
        """Wait for playback video and audio acknowledgements in either order."""
        pending = {start_command, (7, 4)}
        try:
            async with asyncio.timeout(_CONTROL_RESPONSE_TIMEOUT_SECONDS):
                while pending:
                    command = await self._receive_or_transport_failure(
                        self._control_messages
                    )
                    key = command.high_command, command.low_command
                    if (
                        command.request_id == stream_id
                        and command.control == 1
                        and key in pending
                    ):
                        pending.remove(key)
        except TimeoutError as err:
            missing = ",".join(f"{high}/{low}" for high, low in sorted(pending))
            raise NativeRelayPlaybackError(
                f"Camera did not acknowledge playback commands: {missing}"
            ) from err

    async def _wait_for_control_response(
        self, request_id: int, expected: tuple[int, int]
    ) -> None:
        """Wait for one matching native control acknowledgement."""
        try:
            async with asyncio.timeout(_CONTROL_RESPONSE_TIMEOUT_SECONDS):
                while True:
                    command = await self._receive_or_transport_failure(
                        self._control_messages
                    )
                    if (
                        command.request_id == request_id
                        and command.control == 1
                        and (command.high_command, command.low_command) == expected
                    ):
                        return
        except TimeoutError as err:
            raise NativeRelayPlaybackError(
                "Camera did not acknowledge playback stop command: "
                f"{expected[0]}/{expected[1]}"
            ) from err

    def _fail_transport(self, error: NativeRelayPlaybackError) -> None:
        if self._ended is not None and not self._ended.done():
            self._ended.set_result(error)

    def _media_sequences(self) -> tuple[int, int] | str:
        if self._video is None or self._audio is None:
            return "none"
        return self._video.receive_sequence, self._audio.receive_sequence

    @staticmethod
    def _put_nowait(queue: asyncio.Queue[Any], value: Any, label: str) -> None:
        try:
            queue.put_nowait(value)
        except asyncio.QueueFull as err:
            raise NativeRelayPlaybackError(
                f"Native playback {label} queue is full"
            ) from err

    def _allocate_request_id(self) -> int:
        request_id = self._next_request_id
        if request_id > 0xFFFF:
            raise NativeRelayPlaybackError("Native request IDs are exhausted")
        self._next_request_id += 1
        return request_id

    def _require_connected(self) -> None:
        if self._closed or not self._connected or self._control is None:
            raise NativeRelayPlaybackError("Playback session is not connected")

    def _require_offer(self) -> SdpOffer:
        if self._offer is None:
            raise NativeRelayPlaybackError("Native IMM offer is unavailable")
        return self._offer

    def _require_answer(self) -> SdpAnswer:
        answer = self._answer
        if answer is None or not answer.done() or answer.cancelled():
            raise NativeRelayPlaybackError("Native IMM answer is unavailable")
        try:
            return answer.result()
        except Exception as err:
            raise NativeRelayPlaybackError("Native IMM answer failed") from err

def _stream_config(config: NativeCameraSessionConfig, local: P2pSession) -> StreamConfig:
    try:
        camera_info = json.loads(config.config_json)
    except json.JSONDecodeError as err:
        raise NativeRelayPlaybackError("Camera config JSON is invalid") from err
    if not isinstance(camera_info, dict):
        raise NativeRelayPlaybackError("Camera config is not an object")
    try:
        return StreamConfig.from_json(
            camera_info, config.dev_id, config.local_key, local_session=local,
        )
    except (KeyError, TypeError, ValueError, TuyaIpcP2pError) as err:
        raise NativeRelayPlaybackError(
            "Camera config has no complete native IMM playback session"
        ) from err


def _mqtt_identity(
    app_session: dict[str, Any], region: str, app_profile_id: str
) -> MqttIdentity:
    """Build the selected Tuya consumer app signaling identity."""
    config = NativeAppMqttConfig.from_saved_session(
        app_session, region, app_profile_id
    )
    return MqttIdentity(
        host=config.host,
        port=config.port,
        client_id=config.client_id,
        username=config.username,
        password=config.password,
    )
