import asyncio
from types import SimpleNamespace

from custom_components.tuya_recordings.vendor.tuya_ipc_p2p_sdk.transport import ice_responder as ice


def request(*, nominate=True, password="secret", username=b"local:camera"):
    body = ice._attribute(6, username)
    if nominate:
        body += ice._attribute(0x25, b"")
    header = b"\x00\x01" + (len(body) + 24).to_bytes(2, "big") + bytes.fromhex("2112a442") + b"transaction!"
    signed = header + body
    return signed + ice._attribute(8, ice.hmac_sha1(password.encode(), signed))


def test_only_authenticated_use_candidate_nominates_pair():
    async def run():
        sent = []
        responder = ice.IceResponder("secret", lambda _: None, local_ufrag="local")
        responder._transport = SimpleNamespace(sendto=lambda *args: sent.append(args))
        address = ("192.0.2.1", 1234)
        for malformed in (request(password="wrong"), request(username=b"other:camera"), request()[:-1]):
            responder.datagram_received(malformed, address)
        assert not sent
        assert not responder._nominated.is_set()
        responder.datagram_received(request(nominate=False), address)
        assert len(sent) == 1
        assert not responder._nominated.is_set()
        responder.datagram_received(request(), address)
        assert responder._nominated.is_set()
        assert responder._remote == address
    asyncio.run(run())


def test_authenticated_media_from_other_address_is_not_delivered():
    async def run():
        delivered = []
        key = b"a" * 16
        responder = ice.IceResponder("secret", lambda _: None, receive_key=key, on_segment=delivered.append)
        responder._transport = SimpleNamespace(sendto=lambda *args: None)
        responder._remote = ("192.0.2.1", 1234)
        segment = b"segment"
        wire = segment + ice.hmac_sha1(key, segment)
        responder.datagram_received(wire, ("192.0.2.2", 1234))
        assert delivered == []
        responder.datagram_received(wire, responder._remote)
        assert delivered == [segment]
    asyncio.run(run())
