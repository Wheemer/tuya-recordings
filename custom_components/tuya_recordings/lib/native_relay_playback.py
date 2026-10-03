"""APK-native SD playback with cloud signaling and direct LAN media."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from datetime import date
from typing import Any

from ..vendor.tuya_ipc_p2p_sdk import MqttIdentity, StreamConfig
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
from .native_playback_stream import (
    NativePlaybackCodecInfo,
    NativePlaybackStreamFrame,
    NativePlaybackStreamParser,
)
from .native_session import NativeCameraSessionConfig
from .native_simple_rtp import (
    NativeH264RtpDepacketizer,
    NativeSimpleMediaParser,
    NativeSimpleRtpError,
    parse_rtp_packet,
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
_G711U_RTP_PAYLOAD_TYPE = 99
_NATIVE_PCM_S16LE_CODEC = 0xFFFE
_DISCONNECT_GRACE_SECONDS = 0.3
_CLOSE_STEP_TIMEOUT_SECONDS = 2.0


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

    Construction performs no I/O. ``connect`` sends one MQTT signaling offer,
    waits for the camera to nominate the LAN ICE pair, authenticates conversation
    zero, and prepares one playback conversation. MQTT carries signaling only;
    no relay, reconnect, or retry path exists.
    """

    def __init__(
        self,
        config: NativeCameraSessionConfig,
        app_session: dict[str, Any],
    ) -> None:
        self._native_config = config.validate()
        if self._native_config.p2p_type != 4:
            raise NativeRelayPlaybackError(
                "Native relay playback currently supports P2P type 4 cameras"
            )
        self._stream_config = _stream_config(config)
        self._mqtt_identity = _mqtt_identity(app_session, config.app_region)
        self._uid = config.app_uid
        if not self._uid:
            raise NativeRelayPlaybackError("Smart Life account UID is unavailable")
        self._local_cid = self._stream_config.p2p_session.session_id
        self._control_messages: asyncio.Queue[NativeSimpleCommand] = asyncio.Queue(
            maxsize=_CONTROL_QUEUE_LIMIT
        )
        self._media_frames: asyncio.Queue[NativePlaybackStreamFrame] = asyncio.Queue(
            maxsize=_MEDIA_QUEUE_LIMIT
        )
        self._parser = NativePlaybackStreamParser()
        self._control_decoder = NativeSimpleCommandDecoder()
        self._moto: MotoClient | None = None
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
        return self._local_cid

    @property
    def transport_diagnostics(self) -> str:
        """Return non-secret diagnostics for the current bounded transaction."""
        session = self._session
        ice = self._ice
        session_status = session.diagnostics if session is not None else "unavailable"
        ice_status = ice.diagnostics if ice is not None else "unavailable"
        commands = (
            ",".join(
                f"{command}={count}"
                for command, count in sorted(self._control_commands.items())
            )
            or "none"
        )
        samples = (
            ",".join(
                f"{conversation}:{length}:{prefix}"
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
            "transport=direct-ice, signaling=mqtt, relay=disabled, "
            f"connected={self._connected}, "
            f"session=({session_status}), "
            f"ice=({ice_status}), control=(records={self._control_records},"
            f"decode_failures={self._control_decode_failures},commands={commands}), "
            f"media_samples=({samples}), rtp=({rtp}), "
            f"media_stream_packets={self._media_stream_packets or 'none'}, "
            f"sequences=({self._preview_sequences or 'none'}->"
            f"{self._media_sequences()}), "
            f"codec_info={self._rtp_codec_info or 'none'}, "
            f"response_payloads={self._control_payloads or 'none'}"
        )

    async def connect(self, *, media: bool = True) -> None:
        """Negotiate once through MQTT, then require direct LAN media."""
        if self._closed or self._moto is not None:
            raise NativeRelayPlaybackError("Playback session cannot be connected")
        loop = asyncio.get_running_loop()
        self._answer = loop.create_future()
        self._ended = loop.create_future()
        config = self._stream_config
        p2p = config.p2p_session
        rendezvous_id = (
            f"{config.device_id}{int(time.time())}"
            f"{random_alphanumeric(_RENDEZVOUS_SUFFIX_LENGTH)}"
        )
        token = config.relay_token.with_session_id(rendezvous_id)
        trace_id = f"{p2p.trace_id}_{config.device_id}_{int(time.time() * 1000)}"
        self._offer = build_offer(
            self._uid,
            p2p.session_id,
            int(time.time()),
            p2p.ice_ufrag,
            p2p.ice_password,
            p2p.aes_key,
        )
        self._moto = MotoClient(
            identity=self._mqtt_identity,
            uid=self._uid,
            device_id=config.device_id,
            session_id=p2p.session_id,
            local_key=config.local_key,
            on_answer=self._on_answer,
            on_candidate=self._on_remote_candidate,
            on_disconnect=self._on_disconnect,
            protocol_version=self._native_config.mqtt_protocol_version,
        )
        try:
            await self._moto.async_connect()
            with suppress(TuyaIpcP2pError):
                await self._moto.async_send_sig_query()
            await self._moto.async_send_offer(
                self._offer.sdp,
                config.ice_servers_as_json(),
                trace_id,
                token.as_json(),
                config.log_config_as_json(),
            )
            self._ice = IceResponder(
                p2p.ice_password,
                self._on_local_candidate,
                send_key=self._offer.aes_key,
                on_segment=self._on_direct_segment,
            )
            await self._ice.async_gather()
            self._session = DirectSession(self._ice.send_segment)
            answer = await self._wait_for_answer()
            self._ice.set_receive_key(answer.aes_key)
            await self._ice.async_wait_for_nomination(
                _DIRECT_NOMINATION_TIMEOUT_SECONDS
            )
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
        except BaseException:
            await self.close()
            raise

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
        """Discard completed frames while preserving continuous RTP state."""
        self._media_frames = asyncio.Queue(maxsize=_MEDIA_QUEUE_LIMIT)
        self._parser = NativePlaybackStreamParser()
        self._rtp_packets.clear()
        self._rtp_codec_info.clear()
        self._media_stream_packets.clear()
        self._raw_media_samples.clear()
        self._video_frame_number = 0
        self._audio_frame_number = 0

    async def close(self) -> None:
        """Close every transport once and release the camera-side session."""
        if self._closed:
            return
        if self._connected and self._playback_started:
            with suppress(Exception):
                await _bounded_cleanup(self.stop())
        self._closed = True
        self._connected = False
        for task in list(self._candidate_tasks):
            task.cancel()
        if self._candidate_tasks:
            with suppress(Exception):
                await _bounded_cleanup(
                    asyncio.gather(*self._candidate_tasks, return_exceptions=True)
                )
        self._candidate_tasks.clear()
        if self._answer is not None and not self._answer.done():
            self._answer.cancel()
        if self._ice is not None:
            self._ice.close()
            self._ice = None
        session, self._session = self._session, None
        if session is not None:
            with suppress(Exception):
                await _bounded_cleanup(session.async_close())
        moto, self._moto = self._moto, None
        if moto is not None:
            with suppress(TuyaIpcP2pError):
                await _bounded_cleanup(moto.async_send_disconnect())
            await asyncio.sleep(_DISCONNECT_GRACE_SECONDS)
            with suppress(Exception):
                await _bounded_cleanup(moto.async_close())
        self._control = None
        self._video = None
        self._audio = None

    async def abort(self) -> None:
        """Drop transports when the camera will not acknowledge playback stop."""
        self._playback_started = False
        self._playback_paused = False
        self._stream_id = None
        await self.close()

    async def _wait_for_answer(self) -> SdpAnswer:
        assert self._answer is not None
        try:
            async with asyncio.timeout(_ANSWER_TIMEOUT_SECONDS):
                return await asyncio.shield(self._answer)
        except TimeoutError as err:
            raise NativeRelayPlaybackError(
                f"Camera sent no native IMM answer ({self.transport_diagnostics})"
            ) from err

    def _on_answer(self, sdp: str) -> None:
        answer = self._answer
        if answer is None or answer.done():
            return
        try:
            answer.set_result(parse_answer(sdp))
        except TuyaIpcP2pError as err:
            answer.set_exception(err)

    def _on_local_candidate(self, candidate: str) -> None:
        moto = self._moto
        if moto is None:
            return
        task = asyncio.create_task(self._send_candidate(moto, candidate))
        self._candidate_tasks.add(task)
        task.add_done_callback(self._candidate_tasks.discard)

    @staticmethod
    async def _send_candidate(moto: MotoClient, candidate: str) -> None:
        with suppress(TuyaIpcP2pError):
            await moto.async_send_candidate(candidate)

    def _on_remote_candidate(self, candidate: str) -> None:
        del candidate

    def _on_disconnect(self, close_reason: int) -> None:
        error = NativeRelayPlaybackError(
            f"Camera refused native session (close_reason={close_reason})"
        )
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
                plaintext[:64].hex(),
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
                and rtp.payload_type == _G711U_RTP_PAYLOAD_TYPE
            ):
                self._audio_frame_number += 1
                self._put_nowait(
                    self._media_frames,
                    NativePlaybackStreamFrame(
                        frame_type=3,
                        has_codec_info=True,
                        payload_length=len(rtp.payload),
                        field_8=0,
                        field_12=rtp.timestamp,
                        field_16=0,
                        field_20=0,
                        field_24=0,
                        field_28=self._audio_frame_number,
                        codec_info=NativePlaybackCodecInfo(
                            # These cameras expose payload type 99, but their
                            # playback channel delivers the SDK's decoded
                            # 16-bit PCM callback bytes, not G.711 codewords.
                            codec=_NATIVE_PCM_S16LE_CODEC,
                            sample_rate_index=0,
                            channels_index=0,
                            bit_width_index=1,
                            field_4=0,
                            field_5=0,
                            raw=b"",
                        ),
                        payload=rtp.payload,
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
                access_units = self._h264.push(rtp)
            except NativeSimpleRtpError:
                continue
            for access_unit in access_units:
                self._video_frame_number += 1
                self._put_nowait(
                    self._media_frames,
                    NativePlaybackStreamFrame(
                        frame_type=1,
                        has_codec_info=False,
                        payload_length=len(access_unit),
                        field_8=0,
                        field_12=rtp.timestamp,
                        field_16=0,
                        field_20=0,
                        field_24=0,
                        field_28=self._video_frame_number,
                        codec_info=None,
                        payload=access_unit,
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

def _stream_config(config: NativeCameraSessionConfig) -> StreamConfig:
    try:
        camera_info = json.loads(config.config_json)
    except json.JSONDecodeError as err:
        raise NativeRelayPlaybackError("Camera config JSON is invalid") from err
    if not isinstance(camera_info, dict):
        raise NativeRelayPlaybackError("Camera config is not an object")
    try:
        return StreamConfig.from_json(camera_info, config.dev_id, config.local_key)
    except (KeyError, TypeError, ValueError, TuyaIpcP2pError) as err:
        raise NativeRelayPlaybackError(
            "Camera config has no complete native IMM playback session"
        ) from err


def _mqtt_identity(app_session: dict[str, Any], region: str) -> MqttIdentity:
    """Build the short-lived Smart Life signaling identity."""
    config = NativeAppMqttConfig.from_saved_session(app_session, region)
    return MqttIdentity(
        host=config.host,
        port=config.port,
        client_id=config.client_id,
        username=config.username,
        password=config.password,
    )
