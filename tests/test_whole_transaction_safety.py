"""Whole-transaction safety regressions; no camera or broker access."""
import asyncio
import json
import socket
import struct
import subprocess
import threading
from datetime import date
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

import pytest

from test_native_relay_playback import APP_SESSION, camera_config
from test_native_relay_playback import ANSWER_KEY
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.crypto import encrypt_record
from custom_components.tuya_recordings.lib import native_relay_playback as playback
from custom_components.tuya_recordings.lib import native_relay_runtime as runtime
from custom_components.tuya_recordings.lib.native_session import NativePlaybackRequest
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.models.relay_token import RelayToken
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport.ice_responder import IceResponder


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def blocked(*a, **kw):
        raise AssertionError("Audit probes forbid all real network I/O")
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, blocked)


def test_direct_session_must_not_require_optional_tcp_relay_token():
    config = camera_config()
    info = json.loads(config.config_json)
    info["p2pConfig"].pop("tcpRelay")
    config = replace(config, config_json=json.dumps(info), token=json.dumps(info["p2pConfig"]))
    playback.NativeRelayPlaybackSession(config, APP_SESSION)


def test_negotiated_security_four_must_not_enter_l3_only_transport():
    config = camera_config()
    info = json.loads(config.config_json)
    info["p2pConfig"].update({"securityLevel": 4, "skill": {"security_ability": 16}})
    config = replace(config, config_json=json.dumps(info), token=json.dumps(info["p2pConfig"]))
    assert config.p2p_security_level == 4
    with pytest.raises(playback.NativeRelayPlaybackError, match="security"):
        playback.NativeRelayPlaybackSession(config, APP_SESSION)


def test_relay_token_must_preserve_both_apk_endpoint_lists():
    value = RelayToken.from_json({
        "urls": ["tcp4:127.0.0.1:1001"],
        "urlsEx": ["tcp4:127.0.0.1:1002"],
        "username": "1900000000:fixture", "credential": "a" * 32,
        "sessionId": "fixture",
    })
    assert value.urls == ["tcp4:127.0.0.1:1001", "tcp4:127.0.0.1:1002"]


def test_lan_only_policy_must_not_accept_public_media_candidates():
    responder = IceResponder("fixture", lambda _: None)
    responder.add_remote_candidate("a=candidate:1 1 udp 1234 1.1.1.1 2345 typ srflx")
    assert not responder._remote_candidates


def test_public_authenticated_nomination_is_still_not_lan():
    from test_ice_authentication import request
    responder = IceResponder("secret", lambda _: None, local_ufrag="local")
    sent = []
    responder._transport = SimpleNamespace(sendto=lambda *args: sent.append(args))
    responder.datagram_received(request(), ("1.1.1.1", 2345))
    assert not sent
    assert not responder._nominated.is_set()


@pytest.mark.parametrize("channel,stream,pt,body", [
    (1, 0x20007, 96, b"\x65video"),
    (2, 0x20008, 99, b"\x00\x00\xff\x7f"),
])
def test_apk_filters_keep_current_task_video_and_exact_request_audio(channel, stream, pt, body):
    session = playback.NativeRelayPlaybackSession(camera_config(), APP_SESSION)
    session._stream_id = 0x20008
    session._playback_started = True
    rtp = struct.pack(">BBHII", 0x80, 0x80 | pt, 1, 8000, 1) + body
    framed = struct.pack("<6I", stream, 0, 0, 0, 0, 0) + struct.pack("<I", len(rtp)) + rtp
    session._on_rtp_record(channel, encrypt_record(ANSWER_KEY, framed), ANSWER_KEY)
    assert not session._media_frames.empty()


def test_interactive_video_stall_must_expire_after_initial_video(monkeypatch):
    cancel = threading.Event()
    delivered = []

    class Sink:
        def __init__(self, *a, **kw): pass
        def started(self, *a): pass
        def finished(self, *a): pass
        def frame(self, value): delivered.append(value)

    class Camera:
        count = 0
        async def start(self, **kw): pass
        async def stop(self): pass
        async def receive_frame(self):
            await asyncio.sleep(0.003)
            self.count += 1
            if self.count >= 20:
                cancel.set()
            return SimpleNamespace(is_audio=self.count > 1, codec_info=None)

    monkeypatch.setattr(runtime, "NativePlaybackEventSink", Sink)
    monkeypatch.setattr(runtime.NativeRelayRuntimeSession, "_convert_frame", staticmethod(lambda f, *a: f))
    session = runtime.NativeRelayRuntimeSession(camera_config(), APP_SESSION, cancel_event=cancel, first_frame_timeout=0.01)
    with pytest.raises(runtime.NativeRelayRuntimeError, match="video"):
        asyncio.run(session._interactive_connected(
            Camera(), NativePlaybackRequest(100, 110), lambda _: None, __import__("queue").Queue(),
        ))


def test_received_idr_without_decoder_config_must_not_disable_startup_deadline(monkeypatch):
    from custom_components.tuya_recordings.lib.native_frames import NativePlaybackFrame, NativePlaybackFrameType
    from custom_components.tuya_recordings.lib.native_mux import NativePlaybackInteractiveBrowserMuxer
    cancel = threading.Event()
    chunks = []
    mux = NativePlaybackInteractiveBrowserMuxer(chunks.append)
    packet = NativePlaybackFrame(NativePlaybackFrameType.MEDIA_CODEC, b"\x00\x00\x00\x01\x65fixture", {
        "avChannel": 0, "frameNo": 1, "codecId": 1, "isKeyFrame": True,
        "timestamp": 1000, "timestampScale": 1000,
    })

    class Camera:
        count = 0
        async def start(self, **kw): pass
        async def stop(self): pass
        async def receive_frame(self):
            await asyncio.sleep(0.003)
            self.count += 1
            if self.count >= 20:
                cancel.set()
            return SimpleNamespace(is_audio=False, codec_info=None)

    monkeypatch.setattr(runtime.NativeRelayRuntimeSession, "_convert_frame", staticmethod(lambda f, *a: packet))
    session = runtime.NativeRelayRuntimeSession(camera_config(), APP_SESSION, cancel_event=cancel, first_frame_timeout=0.01)
    try:
        with pytest.raises(runtime.NativeRelayRuntimeError, match="video"):
            asyncio.run(session._interactive_connected(
                Camera(), NativePlaybackRequest(100, 110), mux.handle_event, __import__("queue").Queue(),
            ))
    finally:
        mux.cleanup()


@pytest.mark.parametrize("channel,stream,pt,body", [
    (1, 0x10001, 96, b"\x65old-preview"),
    (2, 0x20007, 99, b"\x00\x00\xff\x7f"),
])
def test_apk_task_and_request_filters_must_drop_stale_media(channel, stream, pt, body):
    session = playback.NativeRelayPlaybackSession(camera_config(), APP_SESSION)
    session._stream_id = 0x20008
    session._playback_started = True
    rtp = struct.pack(">BBHII", 0x80, 0x80 | pt, 1, 8000, 1) + body
    framed = struct.pack("<6I", stream, 0, 0, 0, 0, 0) + struct.pack("<I", len(rtp)) + rtp
    session._on_rtp_record(channel, encrypt_record(ANSWER_KEY, framed), ANSWER_KEY)
    assert session._media_frames.empty()


def test_month_reply_must_require_the_response_control_flag():
    from custom_components.tuya_recordings.lib.native_simple import NativeSimpleCommand, NativeSimpleError, decode_recording_month_reply
    command = NativeSimpleCommand(8, 0, 3, 0, bytes(16))
    with pytest.raises(NativeSimpleError):
        decode_recording_month_reply(command, request_id=8, month=date(2026, 11, 1))


def test_browser_aac_alignment_must_respect_different_video_and_audio_clock_units():
    source = (Path(__file__).resolve().parents[1] / 'custom_components/tuya_recordings/frontend/camera-player.js').read_text()
    expression = source.split('const syncAac = () => ', 1)[1].split('  const release = () =>', 1)[0]
    # Execute the actual private function with two clocks that both mean 1s.
    script = '''
    const aacElement = {currentTime:0, readyState:1, paused:false, pause(){}};
    const firstAacTimestamp=8000, firstVideoTimestamp=90000, audioTimestampScaleHint=8000, videoTimestampScaleHint=90000;
    const video={currentTime:0, paused:false, readyState:4, playbackRate:1};
    const streamStarting=false, audioStalled=false;
    const debug=()=>{}, status={};
    const syncAac = () => ''' + expression + '\nsyncAac(); console.log(aacElement.currentTime);'
    observed = float(subprocess.check_output(['node', '-'], input=script.encode(), timeout=5))
    assert observed == 0


def test_cancelling_bounded_cleanup_must_not_abandon_its_child_task():
    async def run():
        child = None
        entered = asyncio.Event()
        async def cleanup():
            nonlocal child
            child = asyncio.current_task()
            entered.set()
            await asyncio.Event().wait()
        parent = asyncio.create_task(playback._bounded_cleanup(cleanup()))
        await entered.wait()
        parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)
        try:
            assert child.done()
        finally:
            child.cancel()
            await asyncio.gather(child, return_exceptions=True)
    asyncio.run(run())


def test_capability_exchange_must_not_advertise_an_unimplemented_opus_decoder():
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.control import session_sequence
    from custom_components.tuya_recordings.lib.native_simple_rtp import SIMPLE_AUDIO_CODECS
    packet = session_sequence('a' * 32)[2]
    declared = json.loads(packet[20:].rstrip(b'\0'))['data']['capabilities']
    # Active decoder IDs: PCM=0xfffe, G711=132/133, AAC=4. No Opus handler.
    assert not declared.get('opus_decode') or any(
        value not in {0xfffe, 132, 133, 4} for value in SIMPLE_AUDIO_CODECS.values()
    )


def test_backend_close_must_close_its_gateway_http_session(monkeypatch):
    from custom_components.tuya_recordings.lib import native_setup
    from custom_components.tuya_recordings.const import CONF_DEVICE_LOCAL_KEYS, CONF_NATIVE_APP_SESSION
    closed = []
    class Gateway:
        def __init__(self, **kw):
            self.session = SimpleNamespace(close=lambda: closed.append(True))
        def call_with_saved_session(self, *a):
            pytest.fail('Lifecycle probe must perform no HTTP request')
    monkeypatch.setattr(native_setup, 'NativeAppGatewayClient', Gateway)
    backend = native_setup.build_recordings_backend({
        CONF_NATIVE_APP_SESSION: APP_SESSION,
        CONF_DEVICE_LOCAL_KEYS: {'fixture': 'a' * 16},
    })
    backend.close()
    assert closed == [True]
