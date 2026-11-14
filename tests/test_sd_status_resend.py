"""Bounded same-channel SD queries after fresh authenticated wake reports."""

import asyncio
from types import SimpleNamespace

import pytest

from test_low_power_connection import connection, make_client, receive
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling import moto_client
from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.signaling.envelope import decode_payload, encode_payload


def wake(client, stamp=100, **extra):
    connection(client, online=True, lowPowerConnect=False, lastConnectChangeTime=stamp, **extra)


@pytest.mark.asyncio
@pytest.mark.parametrize("race", [False, True])
async def test_one_resend_after_wake_with_same_listener_and_connection(race):
    client = make_client()
    sent = []
    listeners = []

    async def publish(topic, payload, **kwargs):
        protocol, data = decode_payload(client._key, payload)
        sent.append((protocol, data))
        listeners.append(client._dp_query[1])
        if len(sent) == 1:
            receive(client, {"devId": "camera-device", "dps": {"248": 1}}, 4)
            wake(client)
            if race:
                receive(client, {"devId": "camera-device", "dps": {"110": 1}}, 4)
        else:
            wake(client, 101)
            receive(client, {"devId": "camera-device", "dps": {"110": 1}}, 4)

    broker = SimpleNamespace(publish=publish)
    client._client = broker
    assert await client.async_query_direct_dp("110") == 1
    assert len(sent) == (1 if race else 2)
    assert all(protocol == 5 and data["dps"] == {"110": None} for protocol, data in sent)
    assert all(future is listeners[0] for future in listeners)
    assert client._client is broker
    assert client._dp_query is None and client._sd_query_wake_notice is None
    assert f"dp_query_resends={0 if race else 1}" in client.diagnostics


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["silence", "online", "old", "duplicate", "retained", "offline", "wrong_device"])
async def test_nonfresh_reports_do_not_resend_or_extend_deadline(monkeypatch, kind):
    monkeypatch.setattr(moto_client, "_DP_REPORT_TIMEOUT_SECONDS", 0.03)
    client = make_client()
    wake(client, 100)
    sent = []

    async def publish(*args, **kwargs):
        sent.append(args)
        if kind == "online":
            connection(client, online=True)
        elif kind in {"old", "duplicate"}:
            wake(client, 99 if kind == "old" else 100)
        elif kind == "retained":
            client._consume(encode_payload(client._key, 1, b"test", {
                "devId": "camera-device", "online": True, "lowPowerConnect": False,
                "lastConnectChangeTime": 101,
            }, 1, 1, "2.3"), retained=True)
        elif kind == "offline":
            wake(client, 101)
            connection(client, online=False)
        elif kind == "wrong_device":
            receive(client, {"devId": "other", "online": True, "lowPowerConnect": False,
                             "lastConnectChangeTime": 101})

    client._client = SimpleNamespace(publish=publish)
    with pytest.raises(TimeoutError):
        await client.async_query_direct_dp("110")
    assert len(sent) == 1
    assert client._dp_query is None and client._sd_query_wake_notice is None


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["timeout", "failure", "cancel"])
async def test_resend_preserves_deadline_and_cleans_up(monkeypatch, outcome):
    monkeypatch.setattr(moto_client, "_DP_REPORT_TIMEOUT_SECONDS", 0.04)
    client = make_client()
    sent = []

    async def publish(*args, **kwargs):
        sent.append(args)
        if len(sent) == 1:
            await asyncio.sleep(0.025)
            wake(client)
        elif outcome == "failure":
            raise moto_client.aiomqtt.MqttError("failed")
        elif outcome == "cancel":
            raise asyncio.CancelledError
        else:
            await asyncio.sleep(1)

    client._client = SimpleNamespace(publish=publish)
    error = {"timeout": TimeoutError, "failure": moto_client.TuyaIpcP2pConnectionError,
             "cancel": asyncio.CancelledError}[outcome]
    started = asyncio.get_running_loop().time()
    with pytest.raises(error):
        await client.async_query_direct_dp("110")
    assert asyncio.get_running_loop().time() - started < 0.15
    assert len(sent) == 2
    assert client._dp_query is None and client._sd_query_wake_notice is None
