"""One native session consumes both APK answer channels without extra offers."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from test_native_relay_playback import APP_SESSION, answer_sdp, camera_config
from custom_components.tuya_recordings.lib import native_relay_playback as playback_module
from custom_components.tuya_recordings.lib.native_lan_signaling import Lan302Client


@pytest.mark.asyncio
@pytest.mark.parametrize("answer_route", ["mqtt", "lan", "both", "silence"])
async def test_lan_offer_keeps_mqtt_answer_subscription_and_closes_both(monkeypatch, answer_route):
    config = replace(camera_config(), p2p_policy=1)
    playback = playback_module.NativeRelayPlaybackSession(config, APP_SESSION)
    playback._answer = asyncio.get_running_loop().create_future()
    playback._ended = asyncio.get_running_loop().create_future()
    calls = []
    callbacks = {}
    monkeypatch.setattr(playback_module, "_DISCONNECT_GRACE_SECONDS", 0)
    monkeypatch.setattr(playback_module, "_ANSWER_TIMEOUT_SECONDS", 0.02)

    class Broker:
        def __init__(self, **kwargs):
            callbacks["mqtt"] = kwargs["on_answer"]

        async def async_connect(self):
            calls.append("mqtt-subscribe")

        async def async_send_offer(self, *args):
            pytest.fail("A LAN offer must not be duplicated on MQTT")

        async def async_close(self):
            calls.append("mqtt-close")

    class Lan:
        def __init__(self, **kwargs):
            callbacks["lan"] = kwargs["on_answer"]

        async def async_connect(self):
            calls.append("lan-connect")

        async def async_send_offer(self, *args):
            assert "mqtt-subscribe" in calls
            calls.append("lan-offer")
            if answer_route != "silence":
                callbacks["lan" if answer_route == "lan" else "mqtt"](answer_sdp())
            if answer_route == "both":
                callbacks["lan"](answer_sdp())

        async def async_send_disconnect(self):
            calls.append("disconnect")

        async def async_close(self):
            calls.append("lan-close")

    monkeypatch.setattr(playback_module, "MotoClient", Broker)
    monkeypatch.setattr(playback_module, "Lan302Client", Lan)
    try:
        await playback._open_signaling("sdp", [], "trace", {}, {})
        assert playback._answer_mqtt is not None
        if answer_route == "silence":
            with pytest.raises(playback_module.NativeRelayPlaybackError, match="Timed out"):
                await playback._wait_for_answer()
        else:
            answer = await playback._wait_for_answer()
            assert answer.aes_key
            assert playback._answer_source == ("lan" if answer_route == "lan" else "mqtt")
            assert playback._duplicate_answers == int(answer_route == "both")
        assert calls.count("lan-offer") == 1
    finally:
        await playback.close()
    assert calls.count("mqtt-subscribe") == calls.count("mqtt-close") == 1
    assert calls.count("lan-connect") == calls.count("lan-close") == 1
    assert playback._answer_mqtt is None and playback._moto is None
    callbacks["mqtt"](answer_sdp())
    assert playback._answer_source != "unknown"


@pytest.mark.asyncio
async def test_passive_lan_reader_sends_nothing_and_rejects_foreign_session():
    received, sends, candidates, disconnects = [], [], [], []
    messages = iter([
        None,
        {"header": {"type": "answer", "sessionid": "old"}, "msg": {"sdp": "wrong"}},
        {"header": {"type": "answer"}, "msg": {"sdp": "missing"}},
        {"header": {"type": "answer", "sessionid": "current"}, "msg": {"sdp": "right"}},
        {"header": {"type": "candidate", "sessionid": "current"}, "msg": {"candidate": "ice"}},
        {"header": {"type": "disconnect", "sessionid": "current"}, "msg": {"close_reason": 7}},
    ])
    device = SimpleNamespace(
        version=3.5, set_socketTimeout=lambda _: None,
        receive=lambda: next(messages, None), send=lambda message: sends.append(message),
        close=lambda: None,
    )
    lan = Lan302Client(
        uid="account", device_id="camera", session_id="current",
        local_key="0123456789abcdef", on_answer=received.append,
        on_candidate=candidates.append, on_disconnect=disconnects.append,
        preconnect=False, security_level=3,
    )
    lan._device = device
    try:
        await lan.async_listen()
        reader = lan._reader
        await lan.async_listen()
        assert lan._reader is reader
        async with asyncio.timeout(1):
            while not disconnects:
                await asyncio.sleep(0.01)
        assert received == ["right"]
        assert candidates == ["ice"]
        assert disconnects == [7]
        assert lan._session_mismatches == 2
        await lan.async_send_disconnect()
        assert sends == []
    finally:
        await lan.async_close()
    assert lan._reader is None and lan._device is None


@pytest.mark.parametrize("first", ["LAN", "MQTT"])
def test_one_answer_channel_failure_does_not_abort_the_other(first):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        playback = playback_module.NativeRelayPlaybackSession(camera_config(), APP_SESSION)
        playback._answer = loop.create_future()
        playback._ended = loop.create_future()
        playback._mqtt_answers_alive = playback._lan_answers_alive = True
        playback._on_signaling_failure("fixture failure", transport=first)
        assert not playback._answer.done()
        assert not playback._ended.done()
        playback._on_signaling_failure("fixture failure", transport="MQTT" if first == "LAN" else "LAN")
        assert playback._answer.done() and playback._ended.done()
        playback._answer.exception()
        playback._ended.result()
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.mark.asyncio
async def test_failed_auxiliary_cleanup_retains_handle_for_retry(monkeypatch):
    playback = playback_module.NativeRelayPlaybackSession(camera_config(), APP_SESSION)
    monkeypatch.setattr(playback_module, "_DISCONNECT_GRACE_SECONDS", 0)
    fail = True

    async def close():
        if fail:
            raise OSError("fixture close failed")

    channel = SimpleNamespace(async_close=close)
    playback._answer_lan = channel
    with pytest.raises(playback_module.NativeRelayPlaybackError, match="cleanup incomplete"):
        await playback.close()
    assert playback._answer_lan is channel
    fail = False
    await playback.close()
    assert playback._answer_lan is None
