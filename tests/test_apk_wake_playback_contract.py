"""Requirements transcribed from the hash-verified Smart Life native SDK.

Expected wire bytes below are not built by the integration's encoders.
See server analysis/wake-playback-contract-start.log and
analysis/wake-playback-contract-camera.log for the native call sites.
"""

import asyncio
import json
import struct
from types import SimpleNamespace

import pytest

from test_native_relay_playback import session, answer_sdp, ANSWER_KEY
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.crypto import encrypt_record
from custom_components.tuya_recordings.lib.native_simple import (
    NativeSimpleCommand,
    NativeSimpleError,
)
from custom_components.tuya_recordings.lib import native_relay_playback as native


def connected():
    value = session()
    value._connected = True
    value._control = object()
    value._playback_started = True
    value._stream_id = 0x00020008
    value._next_request_id = 9
    value._next_task_id = 3
    value._ended = asyncio.get_running_loop().create_future()
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,low,expected_id", [
    ("pause", 1, 9), ("resume", 2, 0x00020009), ("stop", 3, 9),
])
async def test_apk_control_requests_use_fresh_ids(operation, low, expected_id):
    value = connected()
    value._playback_paused = operation == "resume"
    sent = []
    # A late response to the old stream must not satisfy the new request.
    value._control_messages.put_nowait(NativeSimpleCommand(0x00020008, 1, 7, low, struct.pack("<ii", 0, 1)))

    def camera(packet):
        sent.append(packet)
        request, high, command = struct.unpack_from("<I4xHH", packet, 4)
        if command == low:
            expected = struct.pack("<IIIHHI5i", 0x12345678, expected_id, 0, 7, low,
                                   20, 0, low, 0, 0, 0)
            assert packet == expected
            value._control_messages.put_nowait(NativeSimpleCommand(expected_id, 1, high, command, struct.pack("<ii", 0, 1)))
        else:
            assert operation == "stop"
            assert packet == struct.pack("<IIIHHI2i", 0x12345678, 10, 0, 7, 5, 8, 0, 5)

    value._send_control = camera
    await getattr(value, operation)()
    assert value._control_messages.empty()
    assert len(sent) == (2 if operation == "stop" else 1)
    if operation == "resume":
        assert value._stream_id == expected_id and not value._playback_paused
    elif operation == "pause":
        assert value._stream_id == 0x00020008 and value._playback_paused
    else:
        assert value._stream_id is None and not value._playback_started


@pytest.mark.asyncio
async def test_apk_invalid_seek_preserves_current_playback():
    value = connected()
    marker = object()
    value._media_frames.put_nowait(marker)
    sent = []
    value._send_control = sent.append
    value._video = object()
    value._audio = object()
    with pytest.raises(NativeSimpleError, match="bounds"):
        await value.seek(start=100, end=110, play_time=120)
    assert not sent
    assert value._stream_id == 0x00020008
    assert value._playback_started
    assert value._media_frames.get_nowait() is marker
    assert value._next_request_id == 9 and value._next_task_id == 3


@pytest.mark.asyncio
async def test_apk_seek_allocates_a_new_task_and_rejects_old_video():
    value = connected()
    value._answer = asyncio.get_running_loop().create_future()
    value._answer.set_result(native.parse_answer(answer_sdp()))

    class Media:
        def set_message_handler(self, handler):
            self.handler = handler

    value._video = Media()
    value._audio = Media()
    sent = []

    def camera(packet):
        sent.append(packet)
        request, high, low = struct.unpack_from("<I4xHH", packet, 4)
        assert request == 0x00030009
        if low == 0:
            value._control_messages.put_nowait(NativeSimpleCommand(request, 1, high, low, struct.pack("<ii", 0, 1)))

    value._send_control = camera
    await value.seek(start=100, end=200, play_time=150)
    assert len(sent) == 2 and value._task_id == 3 and value._stream_id == 0x00030009
    assert value._next_task_id == 4
    rtp = struct.pack(">BBHII", 0x80, 0xe0, 1, 90000, 1) + b"\x65old-video"
    old = struct.pack("<I5II", 0x00020008, 0, 0, 0, 0, 0, len(rtp)) + rtp
    value._video.handler(encrypt_record(ANSWER_KEY, old))
    assert value._media_frames.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [(7, 0), (7, 21), (100, 20), (7, 1), (6, 3)])
@pytest.mark.parametrize("status", [0, -1, None])
async def test_apk_negative_or_truncated_control_reply_is_not_success(monkeypatch, command, status):
    value = connected()
    monkeypatch.setattr(native, "_CONTROL_RESPONSE_TIMEOUT_SECONDS", 0.02)
    payload = struct.pack("<i", 0)
    if status is not None:
        payload += struct.pack("<i", status)
    value._control_messages.put_nowait(NativeSimpleCommand(9, 1, *command, payload))
    with pytest.raises(native.NativeRelayPlaybackError, match="rejected|truncated"):
        if command in {(7, 0), (7, 21), (100, 20)}:
            await value._wait_for_playback_responses(9, start_command=command)
        else:
            await value._wait_for_control_response(9, command)


@pytest.mark.asyncio
async def test_apk_audio_open_has_no_completion_callback(monkeypatch):
    value = connected()
    monkeypatch.setattr(native, "_CONTROL_RESPONSE_TIMEOUT_SECONDS", 0.02)
    value._control_messages.put_nowait(NativeSimpleCommand(9, 1, 7, 0, struct.pack("<ii", 0, 1)))
    await value._wait_for_playback_responses(9)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [(7, 0), (7, 21), (100, 20), (7, 1), (7, 2), (7, 3), (6, 3)])
async def test_apk_simple_camera_success_result_is_after_channel(command):
    # JNI passes GetCameraType (0 for ThingCameraSimple) to BaseCallBack.
    # Encryption changes the command, not this callback's response layout.
    value = connected()
    value._control_messages.put_nowait(NativeSimpleCommand(9, 1, *command, struct.pack("<ii", 0, 1)))
    if command in {(7, 0), (7, 21), (100, 20)}:
        await value._wait_for_playback_responses(9, start_command=command)
    else:
        await value._wait_for_control_response(9, command)


@pytest.mark.asyncio
@pytest.mark.parametrize("preview", [False, True])
async def test_apk_stop_queues_both_commands_before_completion(monkeypatch, preview):
    value = connected()
    monkeypatch.setattr(native, "_CONTROL_RESPONSE_TIMEOUT_SECONDS", 0.02)
    high = 6 if preview else 7
    sent = []

    def camera(packet):
        request, actual_high, low = struct.unpack_from("<I4xHH", packet, 4)
        assert actual_high == high
        sent.append((request, low))
        if low == 5:
            assert sent == [(9, 3), (10, 5)]
            value._control_messages.put_nowait(NativeSimpleCommand(9, 1, high, 3, struct.pack("<ii", 0, 1)))

    value._send_control = camera
    value._send_control_plain = camera
    if preview:
        await value._stop_preview()
    else:
        await value.stop()
    assert sent == [(9, 3), (10, 5)]


@pytest.mark.asyncio
async def test_apk_resume_admits_new_audio_identity_not_previous_audio():
    value = connected()
    value._playback_paused = True

    def camera(packet):
        request = int.from_bytes(packet[4:8], "little")
        value._control_messages.put_nowait(NativeSimpleCommand(request, 1, 7, 2, struct.pack("<ii", 0, 1)))

    value._send_control = camera
    await value.resume()
    rtp = struct.pack(">BBHII", 0x80, 0xe3, 1, 8000, 1) + b"\x00\x00\xff\x7f"
    for stream in (0x00020008, 0x00020009):
        record = struct.pack("<I5II", stream, 0, 0, 0, 0, 0, len(rtp)) + rtp
        value._on_rtp_record(2, encrypt_record(ANSWER_KEY, record), ANSWER_KEY)
        if stream == 0x00020008:
            assert value._media_frames.empty()
    frame = value._media_frames.get_nowait()
    assert frame.is_audio and frame.payload == b"\x00\x00\xff\x7f"
    assert value._media_frames.empty()


@pytest.mark.parametrize("status", [-1, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
def test_apk_sd_gate_allows_only_ready_or_full_cards(status):
    # CameraPlaybackPanelPresenter.handleSDState accepts precisely 1 or 3.
    value = session()
    if status in {1, 3}:
        value._accept_sd_status_report(status)
        assert value._sd_status_prepared
    else:
        with pytest.raises(native.NativeRelayPlaybackError):
            value._accept_sd_status_report(status)
        assert not value._sd_status_prepared


@pytest.mark.asyncio
async def test_apk_candidate_preserves_the_offer_trace_id():
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling.moto_client import MotoClient
    from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.models import MqttIdentity

    value = MotoClient(
        identity=MqttIdentity("broker.invalid", 8883, "fixture", "user", "pass"),
        uid="account", device_id="camera", session_id="session", local_key="0123456789abcdef",
        on_answer=lambda _: None, on_candidate=lambda _: None, on_disconnect=lambda _: None,
        moto_id="moto",
    )
    sent = []

    async def publish(protocol, body):
        assert protocol == 302
        sent.append(body)

    value._async_publish = publish
    await value.async_send_offer("sdp", [], "apk-trace", {}, {})
    await value.async_send_candidate("candidate")
    assert sent[1] == {
        "header": {"type": "candidate", "from": "account", "to": "camera",
                   "sessionid": "session", "moto_id": "moto", "trace_id": "apk-trace"},
        "msg": {"candidate": "candidate"},
    }


def test_apk_command_candidate_uses_prepared_offer_trace_not_saved_config():
    value = session()
    sent = []
    value._command = SimpleNamespace(queue=sent.append)
    value._prepare_command_offer("sdp", [], "fresh-http-trace", {}, {})
    value._queue_command_candidate("candidate")
    offer, candidate = json.loads(value._command_offer), json.loads(sent[0])
    assert candidate["header"] == {
        "type": "candidate", "from": "account-uid", "to": "camera-device",
        "sessionid": value.local_cid, "moto_id": value._stream_config.moto_id,
        "path": "relay", "trace_id": "fresh-http-trace",
    }
    assert candidate["header"]["trace_id"] == offer["header"]["trace_id"]
    assert candidate["msg"] == {"candidate": "candidate"}
