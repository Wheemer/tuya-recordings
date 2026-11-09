"""Native command signaling uses a loopback peer, never a physical camera."""

import asyncio
import json
import struct
from dataclasses import replace
from datetime import date

import pytest

from test_native_relay_playback import APP_SESSION, ANSWER_KEY, OFFER_KEY, StatusBroker, answer_sdp, camera_config
from custom_components.tuya_recordings.lib import native_relay_playback as playback
from custom_components.tuya_recordings.lib.native_command_signaling import (
    COMMAND_CONVERSATION,
    NativeCommandSignaling,
)
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.crypto import decrypt_record, encrypt_record
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.crypto import hmac_sha1
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.models.relay_token import RelayToken
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling.handshake_signer import (
    HandshakeSigner, authorization_field,
)
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport.kcp_conversation import KcpConversation
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport.kcp_segment import (
    CMD_PUSH, build_segment, parse_segments,
)
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport.relay_framing import (
    FRAME_HANDSHAKE, FRAME_MEDIA, assemble_handshake_frame, media_frame,
    parse_handshake_frame, unwrap_media_frame, keepalive_frame,
)


def token(port=1443):
    return RelayToken.from_json({
        "urls": [f"tcp4:127.0.0.1:{port}"], "username": "1900000000:fixture",
        "credential": "0123456789abcdef0123456789abcdef", "sessionId": "rendezvous",
    })


@pytest.mark.asyncio
async def test_signaling_tries_secondary_endpoint_only_after_dial_failure():
    async with Peer("answer") as peer:
        value = token(peer.port).raw.copy()
        value["urls"] = ["tcp4:127.0.0.1:0"]
        value["urlsEx"] = [f"tcp4:127.0.0.1:{peer.port}"]
        peer.key = OFFER_KEY
        c = NativeCommandSignaling(
            token=RelayToken.from_json(value), device_id="camera-device", uid="account-uid",
            session_id="session", key=OFFER_KEY, transmit_direct=lambda _: None,
            on_signal=lambda _: None,
        )
        ready = asyncio.Event()
        task = asyncio.create_task(c.start(ready.wait, tcp=True))
        try:
            async with asyncio.timeout(1):
                while not c._tcp_ready:
                    await asyncio.sleep(0.001)
            assert c._connection._endpoint_attempts == 2
            assert peer.opened == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await c.async_close()


def message(kind="answer", sid="session", **data):
    return {
        "header": {"from": "camera", "to": "user", "sessionid": sid, "type": kind},
        "msg": data or {"sdp": answer_sdp()},
    }


def record(value):
    payload = json.dumps(value).encode()
    return struct.pack(">HH", 1, len(payload)) + payload + bytes(-len(payload) % 4)


def f6_attribute(kind, value):
    return struct.pack(">HH", kind, len(value)) + value + bytes(-len(value) % 4)


def test_f6_scans_attributes_and_uses_the_last_native_data_attribute():
    ack = build_segment(COMMAND_CONVERSATION, 0x52, 0, 512, 0, 0, 1)
    push = build_segment(COMMAND_CONVERSATION, CMD_PUSH, 0, 512, 0, 0, 0, b"answer")
    body = (
        f6_attribute(5, b"not a KCP attribute")
        + f6_attribute(7, ack + hmac_sha1(OFFER_KEY, ack))
        + f6_attribute(7, push + hmac_sha1(OFFER_KEY, push))
        + f6_attribute(9, b"end")
    )
    assert unwrap_media_frame(OFFER_KEY, body) == push


@pytest.mark.parametrize("tail", [b"\x00", b"\x00\x07\x00\x20short", b"\x00\x05\x00\x03abc"])
def test_f6_does_not_silently_ignore_a_truncated_trailing_attribute(tail):
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.exceptions import TuyaIpcP2pProtocolError
    frame = media_frame(OFFER_KEY, build_segment(COMMAND_CONVERSATION, 0x52, 0, 512, 0, 0, 1))
    with pytest.raises(TuyaIpcP2pProtocolError, match="truncated"):
        unwrap_media_frame(OFFER_KEY, frame[4:] + tail)


def test_f6_valid_first_ack_does_not_mask_invalid_last_data_tag():
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.exceptions import TuyaIpcP2pProtocolError
    ack = build_segment(COMMAND_CONVERSATION, 0x52, 0, 512, 0, 0, 1)
    body = f6_attribute(7, ack + hmac_sha1(OFFER_KEY, ack)) + f6_attribute(7, ack + bytes(20))
    with pytest.raises(TuyaIpcP2pProtocolError, match="tag mismatch"):
        unwrap_media_frame(OFFER_KEY, body)


def test_f6_unknown_attribute_is_not_mistaken_for_native_data():
    ack = build_segment(COMMAND_CONVERSATION, 0x52, 0, 512, 0, 0, 1)
    assert unwrap_media_frame(OFFER_KEY, f6_attribute(5, ack + hmac_sha1(OFFER_KEY, ack))) == b""


def test_f6_empty_last_data_attribute_does_not_fall_back_to_previous_data():
    ack = build_segment(COMMAND_CONVERSATION, 0x52, 0, 512, 0, 0, 1)
    body = f6_attribute(7, ack + hmac_sha1(OFFER_KEY, ack)) + f6_attribute(7, b"")
    assert unwrap_media_frame(OFFER_KEY, body) == b""


def command(on_signal, direct=lambda _: None):
    return NativeCommandSignaling(
        token=token(), device_id="camera", uid="user", session_id="session",
        key=OFFER_KEY, transmit_direct=direct, on_signal=on_signal,
    )


@pytest.mark.asyncio
async def test_command_record_alignment_fragmentation_binding_and_media_firewall():
    received, writes = [], []
    c = command(received.append)
    peer = KcpConversation(COMMAND_CONVERSATION, c._input_tcp)
    try:
        framed = record(message()) + record(message("candidate", candidate="candidate:1"))
        for part in (framed[:3], framed[3:17], framed[17:]):
            peer.send(encrypt_record(OFFER_KEY, part))
        assert len(received) == 2
        peer.send(encrypt_record(OFFER_KEY, record(message(sid="foreign"))))
        assert c._mismatches == 1
        assert len(received) == 2
        c._input_tcp(build_segment(1, CMD_PUSH, 0, 512, 0, 0, 0, b"video"))
        c._input_tcp(build_segment(0, CMD_PUSH, 0, 512, 0, 0, 0, b"recording control"))
        assert c._blocked == 2
        c._direct = writes.append
        c._direct_ready = True
        c._tcp_ready = True
        c._connection.write_segment = writes.append
        c.queue(json.dumps(message("candidate", candidate="x" * 2500)).encode())
        encrypted = [s.data for raw in writes for s in parse_segments(raw) if s.command == CMD_PUSH]
        plain = b"".join(decrypt_record(OFFER_KEY, value) for value in encrypted)
        assert len(encrypted) > 1
        assert len(plain) % 4 == 0
        assert struct.unpack_from(">H", plain)[0] == 1
        assert all(len(decrypt_record(OFFER_KEY, value)) <= 1300 for value in encrypted)
    finally:
        await peer.async_close()
        await c.async_close()


@pytest.mark.asyncio
@pytest.mark.parametrize("plain", [
    b"\x00\x01\xff\xff", b"\x00\x01\x00\x00",
    b"\x00\x01\x00\x01x\x00\x00\x00",
])
async def test_bad_records_fail_bounded_without_dispatch(plain):
    received = []
    c = command(received.append)
    try:
        c._receive(encrypt_record(OFFER_KEY, plain))
        assert c._invalid == 1
        assert not c._buffer and not received
    finally:
        await c.async_close()


@pytest.mark.asyncio
async def test_large_coalesced_records_keep_the_fixed_receive_buffer():
    received = []
    c = command(received.append)
    stream = record(message("candidate", candidate="x" * 2400)) * 2
    try:
        for offset in range(0, len(stream), 1300):
            c._receive(encrypt_record(OFFER_KEY, stream[offset:offset + 1300]))
            assert len(c._buffer) <= 4092
        assert len(received) == 2
        assert c._invalid == 0
    finally:
        await c.async_close()


class Peer:
    """Actual encrypted TCP framing and KCP; no production media router."""

    def __init__(self, mode="answer"):
        self.mode = mode
        self.tasks = set()
        self.offers = []
        self.opened = 0
        self.closed = asyncio.Event()
        self.errors = []
        self.batched_frames = 0

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_):
        self.server.close()
        await self.server.wait_closed()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        assert not self.errors

    async def read(self, reader):
        header = await reader.readexactly(4)
        return header[0], await reader.readexactly(int.from_bytes(header[2:4], "big"))

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.opened += 1
        t = token(self.port)
        key = t.credential[:16].encode()
        buffer = bytearray()
        channels = {}
        pending_ack = None
        def send(raw):
            nonlocal pending_ack
            segment = parse_segments(raw)[0]
            if self.mode == "batched_answer" and segment.conversation == COMMAND_CONVERSATION:
                if segment.command == 0x52:
                    pending_ack = raw
                    return
                if segment.command == CMD_PUSH:
                    assert pending_ack is not None
                    body = b""
                    for packet in (pending_ack, raw):
                        tagged = packet + hmac_sha1(self.key, packet)
                        body += struct.pack(">HH", 7, len(tagged)) + tagged + bytes(-len(tagged) % 4)
                    writer.write(struct.pack(">BBH", 0xF6, 0, len(body)) + body)
                    self.batched_frames += 1
                    return
            writer.write(media_frame(self.key, raw))
        channels[COMMAND_CONVERSATION] = KcpConversation(COMMAND_CONVERSATION, send)

        def incoming(value):
            buffer.extend(decrypt_record(self.key, value))
            while len(buffer) >= 4:
                _, length = struct.unpack_from(">HH", buffer)
                size = 4 + length + (-length % 4)
                if len(buffer) < size:
                    return
                payload = json.loads(buffer[4:4 + length])
                del buffer[:size]
                if payload["header"]["type"] != "offer":
                    continue
                self.offers.append(payload)
                if self.mode in {"silent", "candidate_resend"}:
                    continue
                if self.mode in {"nondata_answer", "batched_answer"}:
                    for body in (b"", f6_attribute(9, b"nondata"), f6_attribute(7, b"")):
                        writer.write(struct.pack(">BBH", 0xF6, 0, len(body)) + body)
                answer = message(sid=payload["header"]["sessionid"])
                answer["header"].update({"from": "camera-device", "to": "account-uid"})
                if self.mode == "foreign":
                    answer["header"]["sessionid"] = "wrong-session"
                for channel in (0, 1, 2):
                    send(build_segment(channel, CMD_PUSH, 0, 512, 0, 0, 0, b"blocked"))
                channels[COMMAND_CONVERSATION].send(encrypt_record(self.key, record(answer)))

        channels[COMMAND_CONVERSATION].set_message_handler(incoming)
        try:
            magic, wire = await self.read(reader)
            assert magic == FRAME_HANDSHAKE
            request = parse_handshake_frame(key, wire)
            if self.mode == "stall_handshake":
                await reader.read()
                return
            offset = 8
            rendezvous = None
            while offset + 4 <= len(wire):
                kind, length = struct.unpack_from(">HH", wire, offset)
                if kind == 0:
                    break
                if kind == 3:
                    rendezvous = wire[offset + 4:offset + 4 + length].decode()
                offset += 4 + length
            assert rendezvous
            signer = HandshakeSigner(t.credential, t.expire_timestamp, "camera-device", rendezvous, "account-uid")
            random = authorization_field(request["authorization"], "random")
            writer.write(assemble_handshake_frame(
                1, key, bytes(16), t.session_id.encode(), t.username.encode(),
                json.dumps({"authorization": f"signature={signer.device_signature(random)},random=peer"}).encode(),
            ))
            _, wire = await self.read(reader)
            ack = parse_handshake_frame(key, wire)
            assert authorization_field(ack["authorization"], "signature") == signer.ack_signature(signer.device_signature(random), "peer")
            writer.write(assemble_handshake_frame(
                3, key, bytes(16), t.session_id.encode(), t.username.encode(),
                (b'{"method":"complete","statuscode":403}' if self.mode == "rejected"
                 else b'{"method":"complete","statuscode":200}'),
            ))
            if self.mode in {"rejected", "eof_ready"}:
                return
            if self.mode == "heartbeat":
                for _ in range(5):
                    await asyncio.sleep(0.06)
                    writer.write(keepalive_frame())
            while True:
                magic, body = await self.read(reader)
                if magic == FRAME_MEDIA:
                    for segment in parse_segments(unwrap_media_frame(self.key, body)):
                        assert segment.conversation == COMMAND_CONVERSATION
                        if self.mode == "batched_answer" and segment.command == CMD_PUSH:
                            pending_ack = build_segment(COMMAND_CONVERSATION, 0x52, 0, 512,
                                segment.timestamp, segment.sequence, segment.sequence + 1)
                        channels[COMMAND_CONVERSATION].input(segment)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        except Exception as error:
            self.errors.append(error)
        finally:
            for channel in channels.values():
                await channel.async_close()
            writer.close()
            await writer.wait_closed()
            self.closed.set()
            self.tasks.discard(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["smart_life", "tuya_smart"])
@pytest.mark.parametrize("mode,media", [
    (mode, media) for mode in ("answer", "batched_answer", "nondata_answer", "candidate_resend", "silent", "foreign", "stall_handshake", "eof_ready", "rejected")
    for media in (False, True)
] + [("background", False), ("udp_answer", True)])
async def test_tcp_command_answer_then_catalog_uses_only_lan(monkeypatch, profile, mode, media):
    async with Peer("batched_answer" if mode == "udp_answer" else mode) as peer:
        config = camera_config()
        source = json.loads(config.config_json)
        source["p2pConfig"]["tcpRelay"] = token(peer.port).raw
        config = replace(config, token=json.dumps(source["p2pConfig"]), config_json=json.dumps(source), app_profile_id=profile)
        config = replace(config, sd_low_power_wake=True, wake_direct=True,
                         sd_status_mqtt_dp="110", sd_status_mqtt_capable=True,
                         sd_status_lan_dp="")
        from custom_components.tuya_recordings.lib.commands import CameraCommandQueue
        monkeypatch.setattr(playback, "CAMERA_COMMANDS", CameraCommandQueue())
        session = playback.NativeRelayPlaybackSession(config, APP_SESSION)
        peer.key = session._local.aes_key
        events = []
        udp_transport = None
        udp_remote = None
        def deliver(raw):
            if udp_transport is None:
                session._on_direct_segment(raw)
            else:
                from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.crypto import hmac_sha1
                udp_transport.sendto(raw + hmac_sha1(ANSWER_KEY, raw), udp_remote)
        lan_peer = KcpConversation(0, deliver)
        lan_media = {n: KcpConversation(n, deliver) for n in (1, 2)}
        discovered = False
        media_sequence = 0
        playback_requests = []

        def control(value):
            nonlocal discovered, media_sequence
            if not discovered and media:
                discovered = True
                deliver(build_segment(1, 0x52, 0, 512, 0, 0, 0))
            plain = decrypt_record(peer.key, value)
            if len(plain) < 20:
                return
            magic, request, _, high, low, length = struct.unpack_from("<IIIHHI", plain)
            if magic == 0x12345678 and high in (6, 7):
                if high == 7:
                    playback_requests.append((request, low))
                # Audio open/close has no SDK completion callback.
                if low not in (4, 5):
                    reply = struct.pack("<IIIHHIii", magic, request, 1, high, low, 8, 0, 1)
                    lan_peer.send(encrypt_record(ANSWER_KEY, reply))
                if (high, low) == (7, 4):
                    media_sequence += 1
                    for channel, pt, payload in ((1, 96, b"\x65video"), (2, 99, b"\x00\x00\xff\x7f")):
                        head = struct.pack("<I5I", request, 0, 0, 0, 0, 0)
                        rtp = struct.pack(">BBHII", 0x80, 0x80 | pt, media_sequence, 8000 * media_sequence, 1) + payload
                        lan_media[channel].send(encrypt_record(ANSWER_KEY, head + struct.pack("<I", len(rtp)) + rtp))
                return
            if magic == 0x12345678 and (high, low) == (3, 1) and length == len(plain) - 20:
                events.append("LAN catalog")
                payload = struct.pack("<9I", 0, 2026, 11, 8, 1, 0, 100, 110, 0)
                reply = struct.pack("<IIIHHI", 0x12345678, request, 1, 3, 1, len(payload)) + payload
                lan_peer.send(encrypt_record(ANSWER_KEY, reply))
        lan_peer.set_message_handler(control)

        class Ice:
            diagnostics = "fixture direct"
            def __init__(self, *a, **k): self.candidate_callback = a[1]
            async def async_gather(self):
                if mode == "candidate_resend":
                    self.candidate_callback("candidate:1 1 udp 2130706431 192.0.2.9 12345 typ host")
            def set_receive_key(self, value): events.append("LAN key")
            def set_remote_credentials(self, *a): pass
            def add_remote_candidate(self, value): pass
            async def async_wait_for_nomination(self, timeout):
                if not session._answer.done():
                    await asyncio.shield(session._answer)
            def send_segment(self, raw):
                for segment in parse_segments(raw):
                    if segment.conversation == 0:
                        lan_peer.input(segment)
                    elif segment.conversation in lan_media:
                        lan_media[segment.conversation].input(segment)
            def close(self): pass

        class Mqtt(StatusBroker):
            def __init__(self, **kw): self.candidates = []
            async def async_connect(self): pass
            async def async_send_wake(self):
                events.append("wake")
            async def async_query_direct_dp(self, dp):
                assert dp == "110"
                assert events == ["wake"]
                events.append("SD status")
                self._dp_query[1].set_result(1)
                return await self._dp_query[1]
            async def async_send_offer(self, *a):
                events.append("offer")
            async def async_send_candidate(self, candidate):
                nonlocal udp_remote
                if mode == "candidate_resend":
                    self.candidates.append(candidate)
                    if len(self.candidates) == 2:
                        assert peer.offers and session._command._channel.pending_count == 0
                        assert session._command._last_ack.startswith("TCP:")
                        assert self.candidates[0] == candidate
                        session._on_answer(answer_sdp(), source="mqtt")
                if udp_transport is not None:
                    from test_ice_authentication import request
                    fields = candidate.split()
                    udp_remote = (fields[4], int(fields[5]))
                    udp_transport.sendto(request(password=session._local.ice_password,
                        username=f"{session._local.ice_ufrag}:camera".encode()), udp_remote)
            async def async_send_disconnect(self): pass
            async def async_close(self): pass
        monkeypatch.setattr(playback, "IceResponder", Ice)
        if mode == "udp_answer":
            from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport import ice_responder
            from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.crypto import hmac_sha1
            class CameraUdp(asyncio.DatagramProtocol):
                def datagram_received(self, data, address):
                    if data[:2] == b"\x01\x01":
                        return
                    assert address == udp_remote
                    assert data[-20:] == hmac_sha1(peer.key, data[:-20])
                    for segment in parse_segments(data[:-20]):
                        if segment.conversation == 0:
                            lan_peer.input(segment)
                        elif segment.conversation in lan_media:
                            lan_media[segment.conversation].input(segment)
            udp_transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                CameraUdp, local_addr=("127.0.0.1", 0))
            monkeypatch.setattr(ice_responder, "local_address", lambda: "127.0.0.1")
            monkeypatch.setattr(playback, "IceResponder", ice_responder.IceResponder)
        monkeypatch.setattr(playback, "MotoClient", Mqtt)
        monkeypatch.setattr(playback, "_ANSWER_TIMEOUT_SECONDS", 0.3)
        monkeypatch.setattr(playback, "_DISCONNECT_GRACE_SECONDS", 0)
        if mode == "candidate_resend":
            monkeypatch.setattr(playback, "_OFFER_RETRY_OFFSETS", (0.08, 0.12, 0.16, 0.2))
        try:
            if mode in {"answer", "batched_answer", "nondata_answer", "udp_answer", "candidate_resend"}:
                await session.connect(media=media, allow_wake=True, retry_offer=True)
                if mode == "candidate_resend":
                    assert session._answer_source == "mqtt" and session._offer_retries == 1
                else:
                    assert session._answer_source == "command"
                    assert session._command._blocked == 3
                    assert "source=TCP, tcp_attempts=1" in session._command.diagnostics
                await asyncio.wait_for(peer.closed.wait(), 1)
                page = await session.query_recordings(date(2026, 11, 8))
                assert len(page.items) == 1
                assert events == (["wake", "SD status", "offer", "offer", "LAN key", "LAN catalog"] if mode == "candidate_resend"
                                  else ["wake", "SD status", "offer", "LAN catalog"] if mode == "udp_answer"
                                  else ["wake", "SD status", "offer", "LAN key", "LAN catalog"])
                if mode == "udp_answer":
                    assert session._ice._nominated.is_set()
                assert session._sd_status_query == "report_received"
                assert peer.opened == len(peer.offers) == 1
                if mode in {"batched_answer", "udp_answer"}:
                    assert peer.batched_frames == 1
                    assert session._command._connection._multi_attribute_frames == 1
                    assert "last_data_attributes=2" in session._command.diagnostics
                if mode in {"nondata_answer", "batched_answer", "udp_answer"}:
                    assert session._command._connection._empty_data_frames == 3
                    assert session._command._connection._decode_failures == 0
                assert not session._command._tcp_ready
                assert session._command._channel._retransmit_task is None
                if media:
                    await session.start(start=100, end=110)
                    async with asyncio.timeout(1):
                        video = await session.receive_frame()
                        audio = await session.receive_frame()
                    assert video.payload == b"\x00\x00\x00\x01\x65video"
                    assert audio.payload == b"\x00\x00\xff\x7f"
                    await session.pause()
                    await session.resume()
                    await session.seek(start=100, end=110, play_time=105)
                    async with asyncio.timeout(1):
                        assert (await session.receive_frame()).payload == video.payload
                        assert (await session.receive_frame()).payload == audio.payload
                    await session.stop()
                    # Preview stop consumes 8/9, catalog 10, then each SDK
                    # command allocates a fresh low word; only seek changes task.
                    assert playback_requests == [
                        (0x0002000b, 0), (0x0002000b, 4), (12, 1),
                        (0x0002000d, 2), (0x0003000e, 0), (0x0003000e, 4),
                        (15, 3), (16, 5),
                    ]
            else:
                with pytest.raises(playback.NativeRelayPlaybackError, match="Timed out"):
                    await session.connect(media=media, retry_offer=mode != "background")
                assert events == (["offer"] if mode == "background" else ["wake", "SD status", "offer"])
                assert peer.opened == (0 if mode == "background" else 1)
        finally:
            await session.close()
            await lan_peer.async_close()
            for channel in lan_media.values():
                await channel.async_close()
            if udp_transport is not None:
                udp_transport.close()
        assert session._command is None and session._command_task is None


@pytest.mark.asyncio
async def test_native_command_receives_over_lan_without_sending_push_over_ice():
    received = []
    peer = None
    c = command(
        received.append,
        lambda raw: [peer.input(segment) for segment in parse_segments(raw)],
    )
    peer = KcpConversation(
        COMMAND_CONVERSATION,
        lambda raw: [c.input_direct(segment) for segment in parse_segments(raw)],
    )
    def respond(value):
        plain = decrypt_record(OFFER_KEY, value)
        _, length = struct.unpack_from(">HH", plain)
        assert json.loads(plain[4:4 + length])["header"]["path"] == "relay"
        peer.send(encrypt_record(OFFER_KEY, record(message())))
    peer.set_message_handler(respond)
    async def ready():
        return None
    try:
        offer = message("offer")
        offer["header"]["path"] = "relay"
        await c.start(ready, tcp=False)
        c.queue(json.dumps(offer).encode())
        assert not received
        peer.send(encrypt_record(OFFER_KEY, record(message())))
        assert len(received) == 1
        assert "source=LAN, tcp_attempts=0" in c.diagnostics
        assert c._connection._writer is None
        with pytest.raises(RuntimeError, match="cannot be restarted"):
            await c.start(ready, tcp=False)
        await c.finish_bootstrap()
        assert c._channel._retransmit_task is None
    finally:
        await c.async_close()
        await peer.async_close()


@pytest.mark.asyncio
async def test_ack_only_is_not_a_signaling_answer_and_reports_pending_segments():
    received = []
    c = command(received.append)
    try:
        c._input_source = "TCP"
        segment = parse_segments(build_segment(COMMAND_CONVERSATION, 0x52, 0, 512, 0, 0, 0))[0]
        c._input(segment)
        assert received == []
        assert "signals=0" in c.diagnostics
        assert "last_ack=TCP:sn=0:una=0" in c.diagnostics
        assert "pending_segments=0" in c.diagnostics
    finally:
        await c.async_close()


@pytest.mark.asyncio
async def test_cancelled_native_command_handshake_joins_every_task():
    async with Peer("stall_handshake") as peer:
        c = NativeCommandSignaling(
            token=token(peer.port), device_id="camera-device", uid="account-uid",
            session_id="session", key=OFFER_KEY, transmit_direct=lambda _: None,
            on_signal=lambda _: pytest.fail("No answer expected during cancellation"),
        )
        peer.key = OFFER_KEY
        ready = asyncio.Event()
        task = asyncio.create_task(c.start(ready.wait, tcp=True))
        try:
            async with asyncio.timeout(1):
                while not peer.opened:
                    await asyncio.sleep(0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await c.async_close()
        await asyncio.wait_for(peer.closed.wait(), 1)
        assert not c._tasks
        assert c._connection._writer is None
        assert c._connection._reader_task is None
        assert c._connection._keepalive_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["silent", "heartbeat"])
async def test_native_tcp_heartbeat_deadline_is_refreshed_and_never_reconnects(monkeypatch, mode):
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport import relay_connection
    monkeypatch.setattr(relay_connection, "_KEEPALIVE_TIMEOUT_SECONDS", 0.25, raising=False)
    async with Peer(mode) as peer:
        peer.key = OFFER_KEY
        c = NativeCommandSignaling(
            token=token(peer.port), device_id="camera-device", uid="account-uid",
            session_id="session", key=OFFER_KEY, transmit_direct=lambda _: None,
            on_signal=lambda _: pytest.fail("Heartbeat is not a signaling answer"),
        )
        ready = asyncio.Event()
        task = asyncio.create_task(c.start(ready.wait, tcp=True))
        try:
            async with asyncio.timeout(1):
                while not c._tcp_ready:
                    await asyncio.sleep(0.001)
            if mode == "heartbeat":
                await asyncio.sleep(0.35)
                assert c._tcp_ready
                assert c._connection._received_frames[0xF5] == 5
            async with asyncio.timeout(1):
                while c._connection._failure != "heartbeat_timeout":
                    await asyncio.sleep(0.001)
            assert not c._tcp_ready
            assert c._connection._phase == "timed_out"
            assert c._tcp_attempts == peer.opened == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await c.async_close()


@pytest.mark.asyncio
async def test_command_retransmits_back_off_within_the_owner_deadline(monkeypatch):
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport import kcp_conversation
    now = 0
    sent = []
    monkeypatch.setattr(kcp_conversation, "_now_milliseconds", lambda: now)
    channel = KcpConversation(COMMAND_CONVERSATION, lambda _: sent.append(now), backoff=True)
    try:
        channel.send(b"fixture")
        for now in range(100, 20001, 100):
            channel._retransmit_once()
        assert sent == [0, 300, 900, 2100, 4500, 9300, 18900]
        assert not channel.closed
        channel.discard_pending()
        assert channel._retransmit_task is None
    finally:
        await channel.async_close()


@pytest.mark.parametrize("alter", ["missing", "tampered"])
def test_native_tcp_handshake_rejects_missing_or_invalid_mac(alter):
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.exceptions import TuyaIpcP2pProtocolError
    key = b"0123456789abcdef"
    frame = assemble_handshake_frame(1, key, bytes(16), b"sid", b"user", b'{"statuscode":200}')[4:]
    if alter == "missing":
        frame = frame[:-36]
    else:
        frame = frame[:-1] + bytes([frame[-1] ^ 1])
    with pytest.raises(TuyaIpcP2pProtocolError, match="MAC"):
        parse_handshake_frame(key, frame)


@pytest.mark.asyncio
async def test_command_cleanup_failure_still_closes_other_transports():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    s = playback.NativeRelayPlaybackSession(camera_config(), APP_SESSION)
    command_close = AsyncMock(side_effect=RuntimeError("fixture cleanup failure"))
    direct_close, mqtt_close, ice_close = AsyncMock(), AsyncMock(), Mock()
    s._command = SimpleNamespace(async_close=command_close)
    s._session = SimpleNamespace(async_close=direct_close)
    s._moto = SimpleNamespace(async_close=mqtt_close, async_send_disconnect=AsyncMock())
    s._ice = SimpleNamespace(close=ice_close)
    with pytest.raises(playback.NativeRelayPlaybackError, match="native command cleanup failed"):
        await s.close()
    direct_close.assert_awaited_once()
    mqtt_close.assert_awaited_once()
    ice_close.assert_called_once()
    assert s._session is None and s._moto is None and s._ice is None
    # Failed ownership remains visible so the runtime cannot start a replacement.
    assert s._command is not None
    s._command = None


@pytest.mark.asyncio
async def test_command_answer_remains_available_after_other_channels_fail():
    from types import SimpleNamespace
    s = playback.NativeRelayPlaybackSession(camera_config(), APP_SESSION)
    s._answer = asyncio.get_running_loop().create_future()
    s._ended = asyncio.get_running_loop().create_future()
    s._command = SimpleNamespace(accepting_signals=True)
    s._on_signaling_failure("fixture MQTT EOF", transport="MQTT")
    s._on_signaling_failure("fixture LAN EOF", transport="LAN")
    assert not s._answer.done()
    s._on_command_signal(message(sid=s.local_cid))
    assert s._answer_source == "command"
    s._command = None
    await s.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,reason", [("rejected", "handshake_rejected"), ("eof_ready", "peer_eof")])
async def test_tcp_closure_reason_is_preserved_without_reconnect(mode, reason):
    async with Peer(mode) as peer:
        c = NativeCommandSignaling(
            token=token(peer.port), device_id="camera-device", uid="account-uid",
            session_id="session", key=OFFER_KEY, transmit_direct=lambda _: None,
            on_signal=lambda _: pytest.fail("Rejected connection must not yield an answer"),
        )
        peer.key = OFFER_KEY
        ready = asyncio.Event()
        task = asyncio.create_task(c.start(ready.wait, tcp=True))
        try:
            async with asyncio.timeout(1):
                while reason not in c._connection.diagnostics or c._tcp_ready:
                    await asyncio.sleep(0.001)
            assert c._state != "tcp_signaling_ready"
            assert "signals=0" in c.diagnostics
            assert peer.opened == 1
            if mode == "rejected":
                assert "handshake_status=403" in c.diagnostics
            else:
                assert "read_stage=header, read_expected=4, read_partial=0" in c.diagnostics
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await c.async_close()


@pytest.mark.asyncio
async def test_apk_command_push_routing_and_tcp_sequence_wrap():
    c = command(lambda _: None)
    tcp, lan = [], []
    c._connection.write_segment = tcp.append
    c._direct = lan.append
    c._tcp_ready = c._direct_ready = True
    try:
        for sequence in (0xFFFFFFFF, 0xFFFFFFFF, 0, 0, 1):
            c._write(build_segment(COMMAND_CONVERSATION, CMD_PUSH, 0, 512, 0, sequence, 0, b"x"))
        assert [s.sequence for raw in tcp for s in parse_segments(raw)] == [0xFFFFFFFF, 0, 1]
        assert lan == []
        ack = build_segment(COMMAND_CONVERSATION, 0x52, 0, 512, 0, 0, 0)
        c._write(ack)
        assert tcp[-1] == lan[-1] == ack
        assert "tcp_duplicate_pushes_suppressed=2" in c.diagnostics
    finally:
        await c.async_close()


@pytest.mark.asyncio
async def test_direct_command_segment_remains_visible_in_routing_diagnostics():
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport.direct_session import DirectSession
    session = DirectSession(lambda _: None)
    c = command(lambda _: None)
    session.set_signaling_handler(c.input_direct)
    try:
        session.input_segment(build_segment(COMMAND_CONVERSATION, 0x52, 0, 512, 0, 0, 0))
        assert f"{COMMAND_CONVERSATION}:0x52=1" in session.diagnostics
        assert "packets=LAN:0x52=1" in c.diagnostics
        assert "signals=0" in c.diagnostics
    finally:
        await session.async_close()
        await c.async_close()


def test_wake_confirmation_does_not_regress_after_offer_resends():
    session = playback.NativeRelayPlaybackSession(camera_config(), APP_SESSION)
    session._wake_status = "sent_unconfirmed"
    session._sd_status_query = "report_received"
    assert "wake_status=confirmed" in session.transport_diagnostics


@pytest.mark.asyncio
async def test_command_listener_is_attached_before_ice_gather_receives_a_packet(monkeypatch):
    from unittest.mock import AsyncMock
    s = playback.NativeRelayPlaybackSession(camera_config(), APP_SESSION)
    observed = []

    class Ice:
        diagnostics = "fixture early LAN command"
        def __init__(self, *a, **kw):
            self.deliver = kw["on_segment"]
        async def async_gather(self):
            assert s._session is not None and s._command is not None
            self.deliver(build_segment(COMMAND_CONVERSATION, 0x52, 0, 512, 0, 0, 0))
            observed.append(s._session.diagnostics)
            s._on_answer(answer_sdp(), source="fixture")
        def set_receive_key(self, key): pass
        def set_remote_credentials(self, *args): pass
        def add_remote_candidate(self, value): pass
        async def async_wait_for_nomination(self, timeout): pass
        def send_segment(self, value): pass
        def close(self): pass

    class Mqtt:
        def __init__(self, **kw): pass
        async_connect = AsyncMock()
        async_send_offer = AsyncMock()
        async_send_candidate = AsyncMock()
        async_send_disconnect = AsyncMock()
        async_close = AsyncMock()

    monkeypatch.setattr(playback, "IceResponder", Ice)
    monkeypatch.setattr(playback, "MotoClient", Mqtt)
    monkeypatch.setattr(NativeCommandSignaling, "start", AsyncMock())
    monkeypatch.setattr(playback, "_DISCONNECT_GRACE_SECONDS", 0)
    try:
        await s.connect(media=False)
        assert s._connected
        assert f"{COMMAND_CONVERSATION}:0x52=1" in observed[0]
    finally:
        await s.close()
