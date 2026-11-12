"""APK-native SD playback with cloud signaling and direct LAN media."""

from __future__ import annotations

import asyncio
import json
import struct
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
from ..vendor.tuya_ipc_p2p_sdk.models.relay_token import RelayToken
from ..vendor.tuya_ipc_p2p_sdk.signaling.relay_frames import relay_candidate_frame, relay_offer_frame
from ..vendor.tuya_ipc_p2p_sdk.control import (
    auth_credential,
    authentication_sequence,
)
from ..vendor.tuya_ipc_p2p_sdk.crypto import (
    decrypt_record,
    encrypt_record,
    random_alphanumeric,
)
from ..vendor.tuya_ipc_p2p_sdk.exceptions import TuyaIpcP2pError, TuyaIpcP2pProtocolError
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
from .native_session import NativeCameraSessionConfig, NativeTokenRoute, native_session_api_request
from .native_command_signaling import NativeCommandSignaling
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
    encode_thumbnail_request,
    playback_stream_id,
)
from .native_v4 import NativeV4CatalogPage

_ANSWER_TIMEOUT_SECONDS = 20.0
_SD_STATUS_TIMEOUT_SECONDS = 30.0
# Absolute elapsed thresholds from the APK's native connection-state timer.
_OFFER_RETRY_OFFSETS = (1.5, 3.0, 6.0, 12.0)
_DIRECT_NOMINATION_TIMEOUT_SECONDS = 10.0
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
WAKE_SLOT_TIMEOUT_SECONDS = 61.0


def _consume_cleanup_result(task: asyncio.Task[Any]) -> None:
    """Consume completion from a cleanup task that outlived its deadline."""
    with suppress(BaseException):
        task.result()


async def _bounded_cleanup(operation: Any) -> bool:
    """Run one transport cleanup step without allowing it to wedge playback."""
    task = asyncio.ensure_future(operation)
    task.add_done_callback(_consume_cleanup_result)
    try:
        done, _ = await asyncio.wait({task}, timeout=_CLOSE_STEP_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.wait({task}, timeout=_CLOSE_STEP_TIMEOUT_SECONDS)
        raise
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
    playback conversation. Native command rendezvous is signaling-only;
    bounded foreground MQTT resends never enable relay media.
    """

    def __init__(
        self,
        config: NativeCameraSessionConfig,
        app_session: dict[str, Any],
        *,
        call_api: Any = None,
    ) -> None:
        self._native_config = config.validate()
        if self._native_config.p2p_security_level != 3:
            raise NativeRelayPlaybackError("Native security level is unsupported by this transport")
        self._call_api = call_api
        if self._native_config.p2p_type != 4:
            raise NativeRelayPlaybackError(
                "Native relay playback currently supports P2P type 4 cameras"
            )
        self._http_initialized = False
        self._command = None
        self._command_task = None
        self._command_offer = None
        self._command_trace_id = ""
        self._command_token = None
        self._command_candidates = []
        self._command_status = "not_started"
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
        self._answer_lan: Lan302Client | None = None
        self._answer_mqtt: MotoClient | None = None
        self._lan_answers_alive = False
        self._mqtt_answers_alive = False
        self._answer_source = "none"
        self._direct_control_ready = False
        self._duplicate_answers = 0
        self._answer_channel_failures: dict[str, str] = {}
        self._lan_answer_attempted = False
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
        self._connector_wake_allowed = False
        self._connector_wake_sent = False
        self._connector_wake_status = "not_requested"
        self._retry_mqtt_offer = False
        self._wake_status = "not_requested"
        self._sd_status_query = "not_requested"
        self._sd_status_report_route = "none"
        self._sd_status_send_route = "not_selected"
        self._sd_status_route_basis = "not_selected"
        self._sd_status_dispatch = "not_requested"
        self._sd_status_lan_details = "not_attempted"
        self._sd_status_prepared = False
        self._sd_wake_reserved = False
        self._sd_lan_prechecked = False
        self._sd_lan_attempts = 0
        self._sd_lan_connect_failures = 0
        self._stop_incomplete = False
        self._playback_responses: list[str] = []
        self._playback_bounds: tuple[int, int] | None = None
        self._fragment_end = "not_received"
        self._sd_status_signaling_details = "not_used"
        self._sd_status_deadline: float | None = None
        self._session: DirectSession | None = None
        self._ice: IceResponder | None = None
        self._remote_candidates: list[str] = []
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
        self._media_drops: dict[str, int] = {}
        self._rtp_codec_info: dict[int, int] = {}
        self._media_stream_packets: dict[int, int] = {}
        self._h264 = NativeH264RtpDepacketizer()
        self._video_frame_number = 0
        self._audio_frame_number = 0
        self._control_payloads: dict[str, str] = {}
        self._next_request_id = 8
        self._task_id = 2
        self._next_task_id = 2
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
        lan_answers = self._answer_lan or (self._moto if self._signaling_path == "lan" else None)
        wake_status = self._wake_status
        if wake_status == "sent_unconfirmed" and (
            self._sd_status_query == "report_received"
            or getattr(self._moto, "low_power_wake_confirmed", False)
        ):
            wake_status = "confirmed"
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
            f"control_bootstrap={'authenticated_lan' if self._direct_control_ready else 'sdp_answer' if self._answer_status == 'accepted' else 'waiting'}, "
            f"session=({session_status}), "
            f"ice=({ice_status}), control=(records={self._control_records},"
            f"decode_failures={self._control_decode_failures},commands={commands}), "
            f"media_samples=({samples}), rtp=({rtp}), "
            f"media_stream_packets={self._media_stream_packets or 'none'}, "
            f"playback_media=(started={self._playback_started}, task={self._task_id}, "
            f"stream={self._stream_id}, video_frames={self._video_frame_number}, "
            f"audio_frames={self._audio_frame_number}, drops={self._media_drops or 'none'}, "
            f"buffered={{{', '.join(f'{key}: {value.buffered_bytes}' for key, value in sorted(self._rtp_parsers.items()))}}}), "
            f"sequences=({self._media_sequences()}), "
            f"codec_info={self._rtp_codec_info or 'none'}, "
            f"signaling_details=({getattr(self._moto, 'diagnostics', 'unavailable')}), "
            f"command_signaling=({getattr(self._command, 'diagnostics', self._command_status)}), "
            f"sd_lan_details=({getattr(self._status_lan, 'diagnostics', self._sd_status_lan_details)}), "
            f"sd_preparation_signaling=({self._sd_status_signaling_details}), "
            f"answer_channels=(mqtt={self._mqtt_answers_alive}, lan={self._lan_answers_alive}, "
            f"source={self._answer_source}, duplicates={self._duplicate_answers}, "
            f"failures={self._answer_channel_failures}), "
            f"lan_answer_details=({getattr(lan_answers, 'diagnostics', 'not_open')}), "
            f"lan={self._lan_status}, offer={self._offer_status}, "
            "offer_delivery=unconfirmed, "
            f"answer={self._answer_status}, "
            f"offer_retries={self._offer_retries}, "
            f"http_initialized={self._http_initialized}, phase={self._connect_phase}, "
            f"mode={self._connect_mode}, elapsed_seconds={elapsed}, "
            f"wake_scope_enabled={self._on_demand_catalog}, "
            f"offer_resend_enabled={self._on_demand_catalog or self._retry_mqtt_offer}, "
            f"app_profile={config.app_profile_id}, bootstrap_api_version={api_version}, "
            f"p2p_type={config.p2p_type}, p2p_policy={config.p2p_policy}, "
            f"mqtt_pv={config.mqtt_protocol_version or 'unknown'}, "
            f"preconnect={bool(config.preconnect)}, "
            f"offer_preconnect={config.signaling_preconnect}, "
            f"token_selection={self._http_token_reason() or 'app_token'}, "
            f"token_details=({config.token_route_diagnostics}), "
            "session_identity=generated_local, "
            f"security_level={config.p2p_security_level}, "
            f"token_route={self._token_route}, "
            f"wake_capability={config.wake_capability}, wake_direct={config.wake_direct}, "
            f"wake_status={wake_status}, sd_low_power_wake={config.sd_low_power_wake}, "
            f"sd_status_query={self._sd_status_query}, connector_wake={self._connector_wake_status}, "
            f"sd_status_report_route={self._sd_status_report_route}, "
            f"sd_status_send_route={self._sd_status_send_route}, "
            f"sd_status_route_basis={self._sd_status_route_basis}, "
            f"sd_status_lan_attempts={self._sd_lan_attempts}, "
            f"sd_status_lan_connect_failures={self._sd_lan_connect_failures}, "
            f"playback_responses={','.join(self._playback_responses) or 'none'}, "
            f"fragment_end={self._fragment_end}, "
            f"sd_status_inventory_route={'mqtt' if config.sd_status_mqtt_dp else 'http' if config.sd_status_http_dp else 'none'}, "
            f"sd_status_online_report={getattr(self._moto, 'device_online', None)}, "
            f"sd_status_dispatch={getattr(self._moto, 'dp_query_dispatch', self._sd_status_dispatch) if self._sd_status_send_route == 'mqtt' else self._sd_status_dispatch}"
        )

    async def connect(
        self, *, media: bool = True, allow_wake: bool = False, retry_offer: bool = False,
    ) -> None:
        """Negotiate LAN-first, then require direct LAN media."""
        if self._closed or self._moto is not None:
            raise NativeRelayPlaybackError("Playback session cannot be connected")
        self._connect_started = time.monotonic()
        self._on_demand_catalog = allow_wake and not media
        self._connector_wake_allowed = allow_wake or retry_offer
        self._retry_mqtt_offer = retry_offer
        self._connect_mode = "media" if media else "metadata"
        self._connect_phase = "select_transport"
        loop = asyncio.get_running_loop()
        self._answer = loop.create_future()
        self._ended = loop.create_future()
        if self._connector_wake_allowed and CAMERA_COMMANDS.wake_report_on_cooldown(self._native_config.dev_id):
            self._connector_wake_status = "report_timeout_cooldown"
            error = NativeRelayPlaybackError("Camera wake is on SD-status timeout cooldown")
            self._annotate_connect_failure(error)
            await self.close()
            raise error
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
        token = config.relay_token
        if token is not None and not self._http_initialized:
            token = token.with_session_id(rendezvous_id)
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
                token.as_json() if token is not None else {},
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
            self._session = DirectSession(self._ice.send_segment)
            self._start_command_signaling(
                tcp=media or self._on_demand_catalog or self._retry_mqtt_offer,
            )
            await self._ice.async_gather()
            self._connect_phase = "wait_connection"
            answer = await self._wait_for_connection()
            if self._command is not None:
                if not await _bounded_cleanup(self._command.finish_bootstrap()):
                    raise NativeRelayPlaybackError("Native signaling TCP cleanup timed out")
            if self._ended.done():
                raise self._ended.result()
            receive_key = self._receive_key()
            self._ice.set_receive_key(receive_key)
            if answer is not None:
                self._ice.set_remote_credentials(answer.ice_ufrag, answer.ice_password)
            for candidate in (*self._remote_candidates, *(answer.candidates if answer is not None else ())):
                self._ice.add_remote_candidate(candidate)
            self._remote_candidates.clear()
            self._connect_phase = "ice_nomination"
            await self._ice.async_wait_for_nomination(
                _DIRECT_NOMINATION_TIMEOUT_SECONDS
            )
            if self._ended.done():
                raise self._ended.result()
            self._connect_phase = "native_control"
            self._control = self._session.control
            self._control.set_message_handler(
                lambda record: self._on_control_record(record, receive_key)
            )
            credential = auth_credential(config.device_password, config.local_key)
            if media:
                # The APK's SD model connects without starting live preview.
                # Its readers wait for media from StartPlayBack, not vice versa.
                self._video = self._session.conversation(_VIDEO_CONVERSATION)
                self._audio = self._session.conversation(_AUDIO_CONVERSATION)
                self._video.set_message_handler(
                    lambda record: self._on_rtp_record(
                        _VIDEO_CONVERSATION, record, receive_key
                    )
                )
                self._audio.set_message_handler(
                    lambda record: self._on_rtp_record(
                        _AUDIO_CONVERSATION, record, receive_key
                    )
                )
            for packet in authentication_sequence(credential):
                self._send_control_plain(packet)
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
        self._validate_direct_token(self._native_config.p2p_config)
        self._prepare_command_offer(sdp, ice_servers, trace_id, tcp_token, log_config)
        if self._native_config.p2p_policy == 1 and not self._http_initialized:
            config = self._native_config
            if (not self._sd_status_prepared and config.sd_low_power_wake is True
                    and config.wake_direct and (self._on_demand_catalog or self._connector_wake_allowed)):
                # The APK presenter prepares SD status before connectDevice,
                # including a subsequent native LAN signaling connection.
                await self._connect_mqtt(self._stream_config.p2p_session.session_id)
                status_moto = self._moto
                self._sd_status_signaling_details = getattr(status_moto, "diagnostics", "unavailable")
                if getattr(status_moto, "low_power_wake_confirmed", False):
                    self._wake_status = "confirmed"
                self._answer_mqtt = status_moto
                self._moto = None
            lan = self._answer_lan or Lan302Client(
                uid=self._uid,
                device_id=self._stream_config.device_id,
                session_id=self._stream_config.p2p_session.session_id,
                local_key=self._stream_config.local_key,
                on_answer=lambda sdp: self._on_answer(sdp, source="lan"),
                on_candidate=self._on_remote_candidate,
                on_disconnect=self._on_disconnect,
                preconnect=self._native_config.signaling_preconnect,
                security_level=self._native_config.p2p_security_level,
                moto_id=self._stream_config.moto_id,
                on_failure=lambda reason: self._on_signaling_failure(reason, transport="LAN"),
            )
            if self._answer_lan is not None:
                self._answer_lan = None
                lan.configure_offer_metadata(
                    moto_id=self._stream_config.moto_id,
                    security_level=self._native_config.p2p_security_level,
                    preconnect=self._native_config.signaling_preconnect,
                )
            self._moto = lan
            self._signaling_path = "lan"
            self._lan_status = "connecting"
            self._connect_phase = "lan_connect"
            try:
                await lan.async_connect()
                self._lan_status = "socket_open"
                self._lan_answers_alive = True
                if self._answer is not None and self._answer_mqtt is None and config.mqtt_protocol_version:
                    await self._connect_auxiliary_mqtt(self._stream_config.p2p_session.session_id)
                self._offer_status = "sending"
                self._connect_phase = "lan_offer"
                _LOGGER.debug("Native SD offer selected: %s", self.transport_diagnostics)
                await lan.async_send_offer(sdp, ice_servers, trace_id, tcp_token, log_config)
                self._offer_status = "sent"
                self._lan_answers_alive = True
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
            relay = self._stream_config.relay_token
            tcp_token = relay.as_json() if relay is not None else {}
            log_config = self._stream_config.log_config_as_json()
        if not self._http_initialized:
            self._token_route = "app_token"
        self._prepare_command_offer(sdp, ice_servers, trace_id, tcp_token, log_config)
        if self._moto is None:
            if self._answer_mqtt is not None:
                self._moto, self._answer_mqtt = self._answer_mqtt, None
            else:
                await self._connect_mqtt(self._stream_config.p2p_session.session_id)
        if self._ended is not None and self._ended.done():
            raise self._ended.result()
        self._signaling_path = "mqtt"
        self._connect_phase = "mqtt_offer"
        self._offer_status = "sending"
        _LOGGER.debug("Native SD offer selected: %s", self.transport_diagnostics)
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

    def _prepare_command_offer(self, sdp, ice_servers, trace_id, tcp_token, log_config) -> None:
        if self._stream_config is None:
            return
        self._command_token = tcp_token
        self._command_trace_id = trace_id
        self._command_offer = relay_offer_frame(
            self._uid, self._stream_config.device_id, self.local_cid, trace_id,
            sdp, ice_servers, tcp_token, log_config, self._stream_config.moto_id,
            self._native_config.signaling_preconnect,
            self._native_config.p2p_security_level,
        )

    def _start_command_signaling(self, *, tcp: bool) -> None:
        if self._answer.done() or self._command_offer is None:
            return
        if self._native_config.p2p_security_level != 3:
            self._command_status = "security_mode_unavailable"
            return
        token = RelayToken.from_json(self._command_token) if self._command_token else None
        self._command = NativeCommandSignaling(
            token=token, device_id=self._stream_config.device_id, uid=self._uid,
            session_id=self.local_cid, key=self._offer.aes_key,
            transmit_direct=self._ice.send_segment, on_signal=self._on_command_signal,
        )
        attach = getattr(self._session, "set_signaling_handler", None)
        if callable(attach):
            attach(self._command.input_direct)
        self._command.queue(self._command_offer)
        for candidate in self._command_candidates:
            self._queue_command_candidate(candidate)

        async def run() -> None:
            try:
                await self._command.start(
                    lambda: self._ice.async_wait_for_nomination(_ANSWER_TIMEOUT_SECONDS), tcp=tcp,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._command_status = type(error).__name__
        self._command_task = asyncio.create_task(run())

    def _on_command_signal(self, payload: dict) -> None:
        kind, message = payload["header"]["type"], payload["msg"]
        if kind == "answer" and isinstance(message.get("sdp"), str):
            self._on_answer(message["sdp"], source="command")
        elif kind == "candidate" and isinstance(message.get("candidate"), str):
            self._on_remote_candidate(message["candidate"])
        elif kind == "disconnect" and type(message.get("close_reason")) is int:
            self._on_disconnect(message["close_reason"])

    def _queue_command_candidate(self, candidate: str) -> None:
        self._command.queue(relay_candidate_frame(
            self._uid, self._stream_config.device_id, self.local_cid,
            self._command_trace_id, candidate, self._stream_config.moto_id,
        ))

    async def _close_failed_lan(self, lan: Lan302Client) -> None:
        if not await _bounded_cleanup(lan.async_close()):
            raise NativeRelayPlaybackError("Native LAN cleanup timed out")
        self._moto = None
        self._lan_answers_alive = False

    async def _connect_mqtt(self, session_id: str) -> None:
        if self._answer_mqtt is not None:
            self._moto, self._answer_mqtt = self._answer_mqtt, None
            return
        if not self._native_config.mqtt_protocol_version:
            raise NativeRelayPlaybackError(
                "Camera MQTT protocol version is unknown; refresh the camera inventory"
            )
        self._signaling_path = "mqtt"
        self._connect_phase = "mqtt_subscribe"
        self._moto = self._new_mqtt_client(session_id)
        await self._moto.async_connect()
        self._mqtt_answers_alive = True
        await self._maybe_send_wake(self._moto)
        config = self._native_config
        if (config.sd_low_power_wake is True and config.wake_direct
                and (self._on_demand_catalog or self._connector_wake_allowed)
                and not self._sd_status_prepared):
            raise NativeRelayPlaybackError(
                f"Camera SD status was not verified ({self.transport_diagnostics})"
            )
        await self._maybe_send_connector_wake(self._moto)
        await self._ensure_lan_answer_listener()

    def _new_mqtt_client(self, session_id: str) -> MotoClient:
        return MotoClient(
            identity=self._mqtt_identity,
            uid=self._uid,
            device_id=self._native_config.dev_id,
            session_id=session_id,
            local_key=self._native_config.local_key,
            on_answer=lambda sdp: self._on_answer(sdp, source="mqtt"),
            on_candidate=self._on_remote_candidate,
            on_disconnect=self._on_disconnect,
            protocol_version=self._native_config.mqtt_protocol_version,
            preconnect=self._native_config.signaling_preconnect,
            security_level=self._native_config.p2p_security_level,
            moto_id=self._stream_config.moto_id if self._stream_config else "",
            on_failure=self._on_signaling_failure,
        )
    async def _connect_auxiliary_mqtt(self, session_id: str) -> None:
        """Subscribe for answers without making broker availability a LAN gate."""
        mqtt = self._new_mqtt_client(session_id)
        self._answer_mqtt = mqtt
        try:
            await mqtt.async_connect()
        except (TuyaIpcP2pError, OSError, TimeoutError) as error:
            self._mqtt_answers_alive = False
            self._answer_channel_failures["MQTT"] = type(error).__name__
            if not await _bounded_cleanup(mqtt.async_close()):
                raise NativeRelayPlaybackError("MQTT answer listener cleanup timed out") from error
            self._answer_mqtt = None
        else:
            self._mqtt_answers_alive = True

    async def _ensure_lan_answer_listener(self) -> None:
        config = self._native_config
        if (self._answer is None or self._answer_lan is not None or self._lan_answer_attempted
                or not config.wake_direct
                or not (config.lan_signaling_capable or config.sd_status_lan_dp)):
            return
        self._lan_answer_attempted = True
        lan = Lan302Client(
            uid=self._uid, device_id=config.dev_id, session_id=self.local_cid,
            local_key=config.local_key, on_answer=lambda sdp: self._on_answer(sdp, source="lan"),
            on_candidate=self._on_remote_candidate, on_disconnect=self._on_disconnect,
            preconnect=False, security_level=config.p2p_security_level,
            on_failure=lambda reason: self._on_signaling_failure(reason, transport="LAN"),
        )
        self._answer_lan = lan
        try:
            await lan.async_connect()
            await lan.async_listen()
        except (NativeLanSignalError, OSError) as error:
            self._answer_channel_failures["LAN"] = type(error).__name__
            if not await _bounded_cleanup(lan.async_close()):
                raise NativeRelayPlaybackError("LAN answer listener cleanup timed out") from error
            self._answer_lan = None
        else:
            self._lan_answers_alive = True

    async def _maybe_send_connector_wake(self, moto: MotoClient) -> None:
        """Match InternalThingSmartCameraP2P's pre-connect direct wake."""
        config = self._native_config
        if not self._connector_wake_allowed:
            return
        if self._connector_wake_sent:
            self._connector_wake_status = "shared_sd_wake"
            return
        if self._sd_status_prepared:
            self._connector_wake_status = "sd_ready"
            return
        if config.wake_capability not in {"low_power", "remote", "low_power_and_remote", "wireless"}:
            self._connector_wake_status = "not_eligible"
            return
        if not config.wake_direct:
            self._connector_wake_status = "target_unconfirmed"
            return
        if CAMERA_COMMANDS.wake_report_on_cooldown(config.dev_id):
            self._connector_wake_status = "report_timeout_cooldown"
            raise NativeRelayPlaybackError("Camera wake is on SD-status timeout cooldown")
        if not CAMERA_COMMANDS.reserve_wake(config.dev_id):
            self._connector_wake_status = "cooldown"
            return
        self._connect_phase = "connector_wake"
        try:
            await moto.async_send_wake()
        except TuyaIpcP2pError:
            self._connector_wake_status = "send_failed"
        else:
            self._connector_wake_sent = True
            self._connector_wake_status = "sent_unconfirmed"

    async def _maybe_send_wake(self, moto: MotoClient) -> None:
        if self._sd_status_prepared:
            return
        config = self._native_config
        dp = config.sd_status_lan_dp or config.sd_status_mqtt_dp or config.sd_status_http_dp
        eligible = (
            (self._on_demand_catalog or self._connector_wake_allowed)
            and config.sd_low_power_wake is True and config.wake_direct
        )
        if eligible and dp.isascii() and dp.isdecimal():
            # One device listener survives wake, connection, dispatch, socket
            # teardown and fallback. Nested same-task operations borrow it.
            try:
                with moto.dp_report_listener(dp) as report:
                    if (not self._connector_wake_sent
                            and not CAMERA_COMMANDS.wake_report_on_cooldown(config.dev_id)
                            and CAMERA_COMMANDS.wake_delay(config.dev_id) > 0):
                        # A reachable LAN camera may already be ready. Preserve
                        # that fast path without publishing another wake.
                        if config.sd_status_lan_dp.isascii() and config.sd_status_lan_dp.isdecimal():
                            self._sd_status_deadline = asyncio.get_running_loop().time() + _SD_STATUS_TIMEOUT_SECONDS
                            if await self._try_lan_sd_status(config.sd_status_lan_dp, self._sd_status_deadline, moto):
                                self._wake_status = "not_needed_lan_ready"
                                return
                            self._sd_lan_prechecked = True
                            self._sd_status_deadline = None
                        await self._wait_for_sd_wake_slot(report)
                    # Waiting for a safety reservation is not a missing SD
                    # report. Start the APK's report deadline after that wait.
                    self._sd_status_deadline = asyncio.get_running_loop().time() + _SD_STATUS_TIMEOUT_SECONDS
                    async with asyncio.timeout_at(self._sd_status_deadline):
                        await self._perform_sd_wake(moto, report)
            except TimeoutError:
                raise self._missing_sd_status_report() from None
            finally:
                self._sd_status_deadline = None
        else:
            await self._perform_sd_wake(moto)

    async def _wait_for_sd_wake_slot(self, report: asyncio.Future[int]) -> None:
        """Wait for one allowed wake; never query a sleeping peer instead."""
        dev_id = self._native_config.dev_id
        self._connect_phase = "wait_wake_cooldown"
        self._wake_status = "waiting_cooldown"
        # The reservation is at most one minute. A concurrent reservation must
        # not turn this foreground operation into an unbounded retry loop.
        try:
            async with asyncio.timeout(WAKE_SLOT_TIMEOUT_SECONDS):
                while not report.done():
                    if CAMERA_COMMANDS.reserve_wake(dev_id):
                        self._sd_wake_reserved = True
                        return
                    await asyncio.wait((report,), timeout=CAMERA_COMMANDS.wake_delay(dev_id))
        except TimeoutError as err:
            self._wake_status = "cooldown_wait_timeout"
            raise NativeRelayPlaybackError(
                f"Camera wake cooldown did not become available ({self.transport_diagnostics})"
            ) from err

    async def _perform_sd_wake(self, moto: MotoClient, report: asyncio.Future[int] | None = None) -> None:
        config = self._native_config
        if not (self._on_demand_catalog or self._connector_wake_allowed):
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
        send_wake = not self._connector_wake_sent and (self._sd_wake_reserved or CAMERA_COMMANDS.reserve_wake(config.dev_id))
        self._sd_wake_reserved = False
        self._connect_phase = "mqtt_wake"
        self._wake_status = "sent_unconfirmed" if self._connector_wake_sent else "cooldown"
        try:
            if send_wake:
                self._wake_status = "sending"
                await moto.async_send_playback_wake()
        except TuyaIpcP2pError:
            self._wake_status = "send_failed"
        else:
            if send_wake:
                self._connector_wake_sent = True
                self._wake_status = "sent_unconfirmed"
                # getDp selects the live route after justSendAwakeDevice.
                # A failed cooldown probe describes the sleeping peer, not
                # whether LAN can dispatch after this newly published wake.
                self._sd_lan_prechecked = False
        # Wake publication is not the SD query's completion callback. The APK
        # still dispatches getDp; throttling a repeat wake must not bypass it.
        if report is not None and report.done():
            self._sd_status_report_route = "mqtt"
            self._accept_sd_status_report(report.result())
            return
        deadline = self._sd_status_deadline or asyncio.get_running_loop().time() + _SD_STATUS_TIMEOUT_SECONDS
        if not self._sd_lan_prechecked and config.sd_status_lan_dp.isascii() and config.sd_status_lan_dp.isdecimal():
            if await self._try_lan_sd_status(config.sd_status_lan_dp, deadline, moto):
                return
            if report is not None and report.done():
                self._sd_status_report_route = "mqtt"
                self._accept_sd_status_report(report.result())
                return
        dp = config.sd_status_mqtt_dp or config.sd_status_http_dp
        use_mqtt = bool(config.sd_status_mqtt_dp)
        online = getattr(moto, "device_online", None)
        self._sd_status_route_basis = "inventory"
        if dp and config.sd_status_mqtt_capable and type(online) is bool:
            use_mqtt = online
            self._sd_status_route_basis = "mqtt_online_report"
        if self._sd_status_query == "lan_send_failed":
            route = config.sd_status_lan_error_route
            if route not in {"mqtt", "http"}:
                return
            dp = config.sd_status_lan_dp
            use_mqtt = route == "mqtt"
            self._sd_status_route_basis = "lan_send_error"
        if not dp.isascii() or not dp.isdecimal():
            if self._sd_status_query != "lan_send_failed":
                self._sd_status_query = "route_or_schema_unconfirmed"
            return
        self._connect_phase = "mqtt_sd_status" if use_mqtt else "http_sd_status"
        self._sd_status_query = "waiting_for_report"
        self._sd_status_send_route = "mqtt" if use_mqtt else "http"
        self._sd_status_dispatch = "sending"
        try:
            async with asyncio.timeout_at(deadline):
                if use_mqtt:
                    status = await moto.async_query_direct_dp(dp)
                else:
                    status = await moto.async_wait_for_dp_report(dp, partial(self._send_http_sd_status, dp))
        except TimeoutError:
            raise self._missing_sd_status_report() from None
        except (TuyaIpcP2pError, NativeGatewayError) as error:
            self._sd_status_query = "send_failed"
            self._sd_status_dispatch = "failed"
            raise NativeRelayPlaybackError(
                f"Camera SD status query failed ({self.transport_diagnostics})"
            ) from error
        else:
            self._sd_status_report_route = "mqtt"
            self._accept_sd_status_report(status)

    def _accept_sd_status_report(self, status: int) -> None:
        # Both playback presenters stop before connecting for these states.
        blocked = {2: "card_error", 4: "formatting", 5: "no_card", 7: "loading", 8: "unavailable"}
        if type(status) is int and status in {1, 3}:
            self._sd_status_query = "report_received"
            self._sd_status_prepared = True
            return
        reason = blocked.get(status, "not_ready")
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

    async def _try_lan_sd_status(self, dp: str, deadline: float, moto: MotoClient) -> bool:
        """Return false only when LAN could not dispatch the query."""
        config = self._native_config
        self._sd_lan_attempts += 1
        self._lan_answer_attempted = True
        lan = Lan302Client(
            uid=self._uid, device_id=config.dev_id, session_id=self.local_cid,
            local_key=config.local_key, on_answer=lambda sdp: self._on_answer(sdp, source="lan"),
            on_candidate=self._on_remote_candidate, on_disconnect=self._on_disconnect,
            preconnect=False, security_level=config.p2p_security_level,
            on_failure=lambda reason: self._on_signaling_failure(reason, transport="LAN"),
        )
        self._connect_phase = "lan_sd_status"
        self._sd_status_send_route = "lan"
        self._sd_status_route_basis = "inventory_lan"
        self._sd_status_dispatch = "sending"
        self._sd_status_query = "waiting_for_report"
        self._status_lan = lan
        retained = False
        try:
            async with asyncio.timeout_at(deadline):
                try:
                    await lan.async_connect()
                except (NativeLanUnavailableError, OSError):
                    self._sd_lan_connect_failures += 1
                    self._sd_status_query = "lan_unavailable"
                    self._sd_status_dispatch = "not_sent"
                    return False
                # The APK's query listener is device-wide: LAN dispatch may
                # produce a report on the already-subscribed MQTT channel.
                with moto.dp_report_listener(dp) as mqtt_report:
                    query = None if mqtt_report.done() else asyncio.create_task(lan.async_query_direct_dp(dp))
                    try:
                        done, _ = await asyncio.wait(
                            (query, mqtt_report) if query is not None else (mqtt_report,),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if mqtt_report in done:
                            status = mqtt_report.result()
                            self._sd_status_report_route = "mqtt"
                            # A device-wide report proves readiness, not that
                            # the concurrent LAN dispatch completed.
                            self._sd_status_dispatch = (
                                "report_received_delivery_unconfirmed" if query is not None
                                else "not_needed_report_available"
                            )
                        else:
                            self._sd_status_dispatch = "sent"
                            try:
                                status = query.result()
                            except NativeLanDpReportError:
                                self._sd_status_query = "lan_report_failed_waiting_mqtt"
                                status = await mqtt_report
                                self._sd_status_report_route = "mqtt"
                            else:
                                self._sd_status_report_route = "lan"
                    finally:
                        if query is not None:
                            query.cancel()
                            await asyncio.gather(query, return_exceptions=True)
        except NativeLanSignalError:
            self._sd_status_query = "lan_send_failed"
            self._sd_status_dispatch = "failed"
            return False
        except TimeoutError:
            raise self._missing_sd_status_report() from None
        else:
            self._accept_sd_status_report(status)
            if self._answer is not None:
                await lan.async_listen()
                self._lan_answers_alive = True
                self._answer_lan = lan
                self._status_lan = None
                retained = True
        finally:
            self._sd_status_lan_details = getattr(lan, "diagnostics", "unavailable")
            if not retained:
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
        self._sd_status_dispatch = "sent"

    def _requires_http_token(self) -> bool:
        """Match ordinary connect_v3 token selection, never a timeout retry."""
        return self._http_token_reason() is not None

    @staticmethod
    def _validate_direct_token(token: dict[str, Any]) -> None:
        if NativeTokenRoute.from_token(token).cloud_session:
            raise NativeRelayPlaybackError(
                "Native token selects a server-managed session; "
                "the APK's separate server-managed transport is not implemented"
            )

    def _http_token_reason(self) -> str | None:
        route = self._native_config.token_route
        if route.cloud_p2p:
            return "http_cloud_p2p"
        if not route.expiry_numeric:
            return "http_missing_expiry"
        if route.expiry <= int(time.time()):
            return "http_expired_token"
        return None

    async def _initialize_http_offer(self, trace_id: str) -> None:
        """Run native state zero once, before the ordinary MQTT offer state."""
        self._validate_direct_token(self._native_config.p2p_config)
        if self._http_initialized:
            return
        self._token_route = self._http_token_reason() or "http_bootstrap_unavailable"
        self._connect_phase = "http_prepare"
        _LOGGER.debug("Native SD token selected: %s", self.transport_diagnostics)
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
        self._validate_direct_token(token)
        if not isinstance(remote, dict) or remote.get("sessionId") != session_id:
            raise NativeRelayPlaybackError("Native HTTP offer returned a different session")
        merged = {**self._native_config.p2p_config, **token,
                  "session": {**self._native_config.p2p_config.get("session", {}), **remote}}
        # Native route fields belong to the fresh token, not the old token's
        # optional connection-data merge.
        for field in ("expire", "skill"):
            merged[field] = token.get(field, remote.get(field))
        merged["isCloudSession"] = token.get("isCloudSession")
        # An omitted flag in a fresh native token resets to false. Do not retain
        # an expired token's true value while merging optional connection data.
        merged["preconnect"] = replace(
            self._native_config, token=json.dumps(token),
        ).signaling_preconnect
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
                preconnect=self._native_config.signaling_preconnect,
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
        """Collect the event day, serializing bounded legacy page requests."""
        self._require_connected()
        if self._playback_started:
            raise NativeRelayPlaybackError(
                "Event catalog cannot run after playback starts"
            )
        items = []
        totals = None
        # Application safety bounds, not SDK timing guarantees. The worker's
        # configurable command deadline may cancel the entire collection sooner.
        collection_timeout_seconds = 15.0
        max_pages = 1024
        max_items = 86400
        # V1 carries a zero-based page ID. V2 has no wire page selector and
        # returns its complete JSON list in one callback.
        async with asyncio.timeout(collection_timeout_seconds):
            for page_index in range(max_pages):
                request_id = self._allocate_request_id()
                self._send_control(
                    encode_event_catalog_request(
                        request_id=request_id,
                        channel=0,
                        day=day,
                        page=page_index,
                        v2=self._native_config.event_catalog_v2,
                    )
                )
                while True:
                    command = await self._receive_or_transport_failure(self._control_messages)
                    if command.request_id != request_id:
                        continue
                    try:
                        page = decode_event_catalog_reply(
                            command,
                            request_id=request_id,
                            v2=self._native_config.event_catalog_v2,
                        )
                    except NativeSimpleError as err:
                        raise NativeRelayPlaybackError(
                            f"Camera returned an invalid event catalog reply: {err}"
                        ) from err
                    break
                current_totals = (page.total_pages, page.total_files)
                if (
                    not 1 <= page.total_pages <= max_pages
                    or not 0 <= page.total_files <= max_items
                    or page.page != page_index + 1
                    or (totals is not None and current_totals != totals)
                ):
                    raise NativeRelayPlaybackError("Invalid event catalog pagination metadata")
                totals = current_totals
                items.extend(page.items)
                if len(items) > page.total_files:
                    raise NativeRelayPlaybackError("Event catalog exceeds reported total")
                if page.page == page.total_pages:
                    if len(items) != page.total_files:
                        raise NativeRelayPlaybackError("Incomplete event catalog")
                    return NativeV4CatalogPage(
                        page.response_id, page.status, page.error_message,
                        1, len(items), 1, tuple(items),
                    )
                if not page.items:
                    raise NativeRelayPlaybackError("Event catalog pagination made no progress")
        raise NativeRelayPlaybackError("Event catalog exceeded pagination limit")

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
        if self._stop_incomplete:
            raise NativeRelayPlaybackError("Previous playback stop did not complete; close the transport")
        del speed
        stream_id = playback_stream_id(task_id=self._next_task_id, request_id=self._next_request_id)
        if self._video is None or self._audio is None:
            raise NativeRelayPlaybackError("Native media conversations are unavailable")
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
        receive_key = self._receive_key()
        self._allocate_request_id()
        self._task_id = self._next_task_id
        self._next_task_id += 1
        self._reset_media_state()
        self._stream_id = stream_id
        self._playback_bounds = (start, end)
        self._playback_responses.clear()
        self._fragment_end = "not_received"
        self._playback_started = True
        self._playback_paused = False
        self._video.set_message_handler(
            lambda record: self._on_rtp_record(_VIDEO_CONVERSATION, record, receive_key)
        )
        self._audio.set_message_handler(
            lambda record: self._on_rtp_record(_AUDIO_CONVERSATION, record, receive_key)
        )
        _LOGGER.debug(
            "Native SD playback command: command=%s/%s stream=%s task=%s "
            "start=%s end=%s position=%s",
            *start_command, stream_id, self._task_id, start, end,
            start if play_time is None else play_time,
        )
        self._send_control(start_payload)
        self._send_control(encode_audio_open(stream_id=stream_id, channel=0))
        await self._wait_for_playback_responses(
            stream_id,
            start_command=start_command,
        )
        _LOGGER.debug(
            "Native SD playback acknowledged: stream=%s task=%s video_frames=%s audio_frames=%s",
            stream_id, self._task_id, self._video_frame_number, self._audio_frame_number,
        )

    async def pause(self) -> None:
        """Pause playback on the active SDK stream."""
        if self._stop_incomplete:
            raise NativeRelayPlaybackError("Previous playback stop did not complete; close the transport")
        self._require_connected()
        if not self._playback_started or self._playback_paused:
            return
        self._require_stream_id()
        request_id = self._allocate_request_id()
        self._send_control(encode_playback_pause(stream_id=request_id, channel=0))
        await self._wait_for_control_response(request_id, (7, 1))
        self._playback_paused = True

    async def resume(self) -> None:
        """Resume playback on the active SDK stream."""
        if self._stop_incomplete:
            raise NativeRelayPlaybackError("Previous playback stop did not complete; close the transport")
        self._require_connected()
        if not self._playback_started or not self._playback_paused:
            return
        self._require_stream_id()
        stream_id = playback_stream_id(task_id=self._task_id, request_id=self._allocate_request_id())
        self._stream_id = stream_id
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
        if self._stop_incomplete:
            raise NativeRelayPlaybackError("Previous playback stop did not complete; close the transport")
        self._require_connected()
        stream_id = self._stream_id
        if stream_id is None:
            raise NativeRelayPlaybackError("Active playback has no stream ID")
        request_id = self._allocate_request_id()
        media_stop, _ = encode_playback_stop(stream_id=request_id, channel=0)
        self._stop_incomplete = True
        self._send_control(media_stop)
        # AsyncSendCommand queues both operations; its immediate return is
        # not the camera's completion callback.
        _, playback_stop = encode_playback_stop(
            stream_id=request_id, channel=0, audio_request_id=self._allocate_request_id(),
        )
        self._send_control(playback_stop)
        await self._wait_for_control_response(request_id, (7, 3))
        self._stop_incomplete = False
        self._playback_started = False
        self._playback_paused = False
        self._stream_id = None

    def _require_stream_id(self) -> int:
        stream_id = self._stream_id
        if stream_id is None:
            raise NativeRelayPlaybackError("Active playback has no stream ID")
        return stream_id

    def _reset_media_state(self) -> None:
        """Discard every decoder-bound state before a new playback stream."""
        self._media_frames = asyncio.Queue(maxsize=_MEDIA_QUEUE_LIMIT)
        self._parser = NativePlaybackStreamParser()
        self._rtp_packets.clear()
        self._media_drops.clear()
        self._rtp_codec_info.clear()
        self._media_stream_packets.clear()
        self._raw_media_samples.clear()
        self._h264 = NativeH264RtpDepacketizer()
        self._video_frame_number = 0
        self._audio_frame_number = 0

    async def close(self) -> None:
        """Close every transport once and release the camera-side session."""
        if (self._closed and self._session is None and self._moto is None
                and self._status_lan is None and self._answer_lan is None
                and self._answer_mqtt is None and self._ice is None and not self._candidate_tasks
                and self._command is None and self._command_task is None):
            return
        if self._connected and self._playback_started and not self._stop_incomplete:
            with suppress(Exception):
                await _bounded_cleanup(self.stop())
        self._closed = True
        self._connected = False
        self._lan_answers_alive = False
        self._mqtt_answers_alive = False
        failures = []
        if self._command_task is not None:
            self._command_task.cancel()
            if not await _bounded_cleanup(asyncio.gather(self._command_task, return_exceptions=True)):
                failures.append("native command task cleanup timed out")
            else:
                self._command_task = None
        if self._command is not None:
            try:
                if not await _bounded_cleanup(self._command.async_close()):
                    failures.append("native command cleanup timed out")
                else:
                    self._command = None
            except Exception:
                failures.append("native command cleanup failed")
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
        for attribute in ("_answer_lan", "_answer_mqtt"):
            channel = getattr(self, attribute)
            if channel is not None:
                try:
                    if await _bounded_cleanup(channel.async_close()):
                        setattr(self, attribute, None)
                    else:
                        failures.append(f"{attribute} close timed out")
                except Exception:
                    failures.append(f"{attribute} close failed")
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

    async def _wait_for_connection(self) -> SdpAnswer | None:
        """Native a81b8 completes on ready ICE; SDP reception is independent."""
        answer_task = asyncio.create_task(self._wait_for_answer())
        direct_task = asyncio.create_task(self._ice.async_wait_for_nomination(_ANSWER_TIMEOUT_SECONDS))
        try:
            done, _ = await asyncio.wait((answer_task, direct_task), return_when=asyncio.FIRST_COMPLETED)
            if answer_task in done:
                return answer_task.result()
            if direct_task.exception() is None and getattr(self._ice, "is_nominated", False) is True:
                if self._ended.done():
                    raise self._ended.result()
                if self._answer.done():
                    return self._answer.result()
                self._direct_control_ready = True
                _LOGGER.debug("Native LAN control ready before SDP answer: %s", self.transport_diagnostics)
                return None
            return await answer_task
        finally:
            for task in (answer_task, direct_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(answer_task, direct_task, return_exceptions=True)

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
                f"Timed out waiting for a usable native IMM answer or authenticated LAN nomination ({self.transport_diagnostics})"
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
                # APK ab52c replays the outgoing cache, populated by both
                # the offer and aba90's local-candidate callback. TCP PUSHes
                # are not repeated; only the existing MQTT retry is used.
                for candidate in tuple(self._command_candidates):
                    if (self._closed or self._answer.done()
                            or loop.time() >= deadline):
                        return
                    if self._ended is not None and self._ended.done():
                        raise self._ended.result()
                    await self._moto.async_send_candidate(candidate)
            except TuyaIpcP2pError as err:
                self._offer_status = "retry_publish_failed"
                raise NativeRelayPlaybackError(
                    f"Native MQTT offer retry failed ({self.transport_diagnostics})"
                ) from err

    def _on_answer(self, sdp: str, *, source: str = "unknown") -> None:
        answer = self._answer
        if self._closed or answer is None:
            return
        if answer.done():
            self._duplicate_answers += 1
            return
        try:
            parsed = parse_answer(sdp)
            if self._direct_control_ready and parsed.aes_key != self._require_offer().aes_key:
                raise TuyaIpcP2pProtocolError("Late SDP answer conflicts with established native session key")
            answer.set_result(parsed)
        except TuyaIpcP2pError as err:
            self._answer_status = "invalid_sdp"
            answer.set_exception(NativeRelayPlaybackError(
                f"Invalid native IMM answer: {err} ({self.transport_diagnostics})"
            ))
            if self._direct_control_ready:
                self._fail_transport(answer.exception())
        else:
            self._answer_status = "accepted"
            self._answer_source = source

    def _on_local_candidate(self, candidate: str) -> None:
        if candidate not in self._command_candidates and len(self._command_candidates) < 8:
            self._command_candidates.append(candidate)
            if self._command is not None:
                self._queue_command_candidate(candidate)
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
        if self._closed or len(candidate) > 1024:
            return
        if self._ice is not None:
            self._ice.add_remote_candidate(candidate)
        elif candidate not in self._remote_candidates and len(self._remote_candidates) < 64:
            self._remote_candidates.append(candidate)

    def _on_disconnect(self, close_reason: int) -> None:
        error = NativeRelayPlaybackError(
            f"Camera refused native session (close_reason={close_reason})"
        )
        if self._answer is not None and not self._answer.done():
            self._answer.set_exception(error)
        self._fail_transport(error)

    def _on_signaling_failure(self, reason: str, *, transport: str = "MQTT") -> None:
        if self._closed:
            return
        self._answer_channel_failures[transport] = reason
        if self._connected and self._direct_control_ready:
            if transport == "MQTT":
                self._mqtt_answers_alive = False
            else:
                self._lan_answers_alive = False
            return
        if transport == "MQTT":
            self._mqtt_answers_alive = False
            if self._lan_answers_alive or (
                self._command is not None and self._command.accepting_signals
            ):
                return
        else:
            self._lan_answers_alive = False
            if self._mqtt_answers_alive or (
                self._command is not None and self._command.accepting_signals
            ):
                return
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
            if command.control == 1 and command.high_command == 7:
                if command.low_command in (0, 21) and self._stream_id is not None:
                    if command.request_id >> 16 == self._stream_id >> 16:
                        status = (
                            int.from_bytes(command.payload[4:8], "little", signed=True)
                            if len(command.payload) >= 8 else "truncated"
                        )
                        self._playback_responses.append(f"{classified}:{status}")
                        del self._playback_responses[:-8]
                elif command.low_command == 6:
                    if len(command.payload) < 16:
                        self._fragment_end = "truncated"
                    else:
                        bounds = struct.unpack_from("<ii", command.payload, 8)
                        self._fragment_end = "matched_range" if bounds == self._playback_bounds else "other_range"
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
        except TuyaIpcP2pError:
            self._count_media_drop("decrypt")
            return
        except NativeSimpleRtpError:
            self._count_media_drop("framing")
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
            if self._playback_started and self._stream_id is not None:
                # ThingPlayTask admits video by task, audio by full request ID.
                if (conversation == _VIDEO_CONVERSATION and packet.stream_id >> 16 != self._task_id
                        or conversation == _AUDIO_CONVERSATION and packet.stream_id != self._stream_id):
                    self._count_media_drop(f"stale_task_ch{conversation}")
                    continue
            if packet.codec_info:
                self._rtp_codec_info[conversation] = (
                    self._rtp_codec_info.get(conversation, 0) + 1
                )
            try:
                rtp = parse_rtp_packet(packet.payload)
            except NativeSimpleRtpError:
                self._count_media_drop("rtp_header")
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
                self._count_media_drop(f"unsupported_ch{conversation}_pt{rtp.payload_type}")
                continue
            try:
                access_units = self._h264.push(rtp, presentation_timestamp=packet.camera_timestamp or rtp.timestamp)
            except NativeSimpleRtpError:
                self._count_media_drop("h264_depacketize")
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

    def _count_media_drop(self, reason: str) -> None:
        self._media_drops[reason] = self._media_drops.get(reason, 0) + 1

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
        """Wait for StartPlayBack's callback; audio-open has no callback."""
        pending = {start_command}
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
                        self._validate_operation_response(command)
                        pending.remove(key)
        except TimeoutError as err:
            missing = ",".join(f"{high}/{low}" for high, low in sorted(pending))
            raise NativeRelayPlaybackError(
                f"Camera did not acknowledge playback commands: {missing} "
                f"({self.transport_diagnostics})"
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
                        self._validate_operation_response(command)
                        return
        except TimeoutError as err:
            raise NativeRelayPlaybackError(
                "Camera did not acknowledge playback stop command: "
                f"{expected[0]}/{expected[1]}"
            ) from err

    @staticmethod
    def _validate_operation_response(command: NativeSimpleCommand) -> None:
        # ThingCameraSimple has device type 0, including encrypted playback.
        # The SDK's extended offset is selected by device type, not command.
        offset = 4
        if len(command.payload) < offset + 4:
            raise NativeRelayPlaybackError("Camera control response is truncated")
        result = int.from_bytes(command.payload[offset:offset + 4], "little", signed=True)
        if result != 1:
            raise NativeRelayPlaybackError(
                f"Camera rejected command {command.high_command}/{command.low_command} (result={result})"
            )

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

    def _receive_key(self) -> bytes:
        # Native level-3 a2f4c initializes channel contexts from the local
        # session key before any answer is received. Never synthesize SDP.
        if self._direct_control_ready:
            return self._require_offer().aes_key
        return self._require_answer().aes_key

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
