"""Native command signaling uses a loopback peer, never a physical camera."""

import asyncio
import json
import struct
from dataclasses import replace
from datetime import date

import pytest

from test_native_relay_playback import APP_SESSION, ANSWER_KEY, OFFER_KEY, answer_sdp, camera_config
from custom_components.tuya_recordings.lib import native_relay_playback as playback
from custom_components.tuya_recordings.lib.native_command_signaling import (
    COMMAND_CONVERSATION,
    NativeCommandSignaling,
)
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.crypto import decrypt_record, encrypt_record
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
    parse_handshake_frame, unwrap_media_frame,
)


def token(port=1443):
    return RelayToken.from_json({
        "urls": [f"tcp4:127.0.0.1:{port}"], "username": "1900000000:fixture",
        "credential": "0123456789abcdef0123456789abcdef", "sessionId": "rendezvous",
    })


def message(kind="answer", sid="session", **data):
    return {
        "header": {"from": "camera", "to": "user", "sessionid": sid, "type": kind},
        "msg": data or {"sdp": answer_sdp()},
    }


def record(value):
    payload = json.dumps(value).encode()
    return struct.pack(">HH", 1, len(payload)) + payload + bytes(-len(payload) % 4)


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
        def send(raw):
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
                if self.mode == "silent":
                    continue
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
                3, key, bytes(16), t.session_id.encode(), t.username.encode(), b'{"statuscode":200}',
            ))
            while True:
                magic, body = await self.read(reader)
                if magic == FRAME_MEDIA:
                    for segment in parse_segments(unwrap_media_frame(self.key, body)):
                        assert segment.conversation == COMMAND_CONVERSATION
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
    (mode, media) for mode in ("answer", "silent", "foreign", "stall_handshake")
    for media in (False, True)
] + [("background", False)])
async def test_tcp_command_answer_then_catalog_uses_only_lan(monkeypatch, profile, mode, media):
    async with Peer(mode) as peer:
        config = camera_config()
        source = json.loads(config.config_json)
        source["p2pConfig"]["tcpRelay"] = token(peer.port).raw
        config = replace(config, token=json.dumps(source["p2pConfig"]), config_json=json.dumps(source), app_profile_id=profile)
        session = playback.NativeRelayPlaybackSession(config, APP_SESSION)
        peer.key = session._local.aes_key
        events = []
        lan_peer = KcpConversation(0, session._on_direct_segment)
        lan_media = {n: KcpConversation(n, session._on_direct_segment) for n in (1, 2)}
        discovered = False

        def control(value):
            nonlocal discovered
            if not discovered and media:
                discovered = True
                session._on_direct_segment(build_segment(1, 0x52, 0, 512, 0, 0, 0))
            plain = decrypt_record(peer.key, value)
            if len(plain) < 20:
                return
            magic, request, _, high, low, length = struct.unpack_from("<IIIHHI", plain)
            if magic == 0x12345678 and high in (6, 7):
                reply = struct.pack("<IIIHHI", magic, request, 1, high, low, 0)
                lan_peer.send(encrypt_record(ANSWER_KEY, reply))
                if (high, low) == (7, 4):
                    for channel, pt, payload in ((1, 96, b"\x65video"), (2, 99, b"\x00\x00\xff\x7f")):
                        head = struct.pack("<I5I", request, 0, 0, 0, 0, 0)
                        rtp = struct.pack(">BBHII", 0x80, 0x80 | pt, 1, 8000, 1) + payload
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
            def __init__(self, *a, **k): pass
            async def async_gather(self): pass
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

        class Mqtt:
            def __init__(self, **kw): pass
            async def async_connect(self): pass
            async def async_send_offer(self, *a): pass
            async def async_send_candidate(self, *a): pass
            async def async_send_disconnect(self): pass
            async def async_close(self): pass
        monkeypatch.setattr(playback, "IceResponder", Ice)
        monkeypatch.setattr(playback, "MotoClient", Mqtt)
        monkeypatch.setattr(playback, "_ANSWER_TIMEOUT_SECONDS", 0.3)
        monkeypatch.setattr(playback, "_DISCONNECT_GRACE_SECONDS", 0)
        try:
            if mode == "answer":
                await session.connect(media=media, retry_offer=True)
                assert session._answer_source == "command"
                assert session._command._blocked == 3
                assert "source=TCP, tcp_attempts=1" in session._command.diagnostics
                await asyncio.wait_for(peer.closed.wait(), 1)
                page = await session.query_recordings(date(2026, 11, 8))
                assert len(page.items) == 1
                assert events == ["LAN key", "LAN catalog"]
                assert peer.opened == len(peer.offers) == 1
                assert not session._command._tcp_ready
                assert session._command._channel._retransmit_task is None
                if media:
                    await session.start(start=100, end=110)
                    async with asyncio.timeout(1):
                        video = await session.receive_frame()
                        audio = await session.receive_frame()
                    assert video.payload == b"\x00\x00\x00\x01\x65video"
                    assert audio.payload == b"\x00\x00\xff\x7f"
            else:
                with pytest.raises(playback.NativeRelayPlaybackError, match="Timed out"):
                    await session.connect(media=media, retry_offer=mode != "background")
                assert not events
                assert peer.opened == (0 if mode == "background" else 1)
        finally:
            await session.close()
            await lan_peer.async_close()
            for channel in lan_media.values():
                await channel.async_close()
        assert session._command is None and session._command_task is None


@pytest.mark.asyncio
async def test_native_command_can_bootstrap_over_lan_without_tcp():
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
        c.queue(json.dumps(offer).encode())
        await c.start(ready, tcp=False)
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
