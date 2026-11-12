import asyncio
import struct
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
        address = ("192.168.50.1", 1234)
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
        responder._remote = ("192.168.50.1", 1234)
        segment = b"segment"
        wire = segment + ice.hmac_sha1(key, segment)
        responder.datagram_received(wire, ("192.168.50.2", 1234))
        assert delivered == []
        responder.datagram_received(wire, responder._remote)
        assert delivered == [segment]
        assert "datagrams=2" in responder.diagnostics
        assert "ignored_endpoint=1" in responder.diagnostics
        assert "segments=1" in responder.diagnostics
    asyncio.run(run())


def test_remote_candidate_checks_are_authenticated_controlled_and_bounded():
    async def run():
        sent = []
        responder = ice.IceResponder("local-secret", lambda _: None, local_ufrag="local")
        responder._transport = SimpleNamespace(sendto=lambda *args: sent.append(args), close=lambda: None)
        candidate = "a=candidate:1 1 UDP 1234 192.168.50.1 2345 typ host"
        responder.add_remote_candidate(candidate)
        assert sent == []
        responder.set_remote_credentials("camera", "camera-secret")
        assert len(sent) == 1 and sent[0][1] == ("192.168.50.1", 2345)
        wire = sent[0][0]
        peer = ice.IceResponder("camera-secret", lambda _: None, local_ufrag="camera")
        assert peer._binding_request(wire) is False
        assert ice._attribute(0x8029, responder._tie_breaker) in wire
        assert not responder._nominated.is_set() and responder._remote is None
        responder.add_remote_candidate(candidate)
        for invalid in (
            candidate.replace(" 1 UDP ", " 2 UDP "),
            candidate.replace(" UDP ", " TCP "),
            candidate.replace("typ host", "typ relay"),
            candidate.replace("2345", "99999"),
            candidate.replace("192.168.50.1", "1.1.1.1"),
        ):
            responder.add_remote_candidate(invalid)
        assert len(sent) == 1
        for port in range(3000, 3100):
            responder.add_remote_candidate(f"a=candidate:1 1 udp 1234 192.168.50.1 {port} typ host")
        assert len(sent) == 64
        responder.close()
        responder.add_remote_candidate(candidate)
        responder.set_remote_credentials("camera", "camera-secret")
        assert len(sent) == 64 and responder._remote_candidates == set()
    asyncio.run(run())


def test_discard_diagnostics_are_bounded_redacted_and_do_not_accept_packets():
    async def run():
        delivered = []
        key = b"private-key-1234"
        responder = ice.IceResponder("private-password", lambda _: None, receive_key=key, on_segment=delivered.append)
        responder._transport = SimpleNamespace(sendto=lambda *args: None)
        responder._remote = ("192.168.50.1", 1234)
        payload = struct.pack("<IBBHIIII", 1, 81, 0, 512, 0, 0, 0, 16) + b"secret-video-data"
        wire = payload + ice.hmac_sha1(key, payload)
        responder.datagram_received(wire, ("192.168.50.1", 4321))
        assert "peer=same_host_other_port,authenticated=True,conversation=1,command=81" in responder.diagnostics
        responder.datagram_received(payload + bytes(20), responder._remote)
        assert "reason=authentication" in responder.diagnostics
        assert "authenticated=False" in responder.diagnostics
        responder.datagram_received(b"short", responder._remote)
        assert "reason=short,bytes=5" in responder.diagnostics
        responder._on_segment = None
        responder.datagram_received(wire, responder._remote)
        assert "reason=no_handler" in responder.diagnostics
        responder.datagram_received(wire, ("8.8.8.8", 1234))
        assert "reason=non_lan" in responder.diagnostics
        for _ in range(30):
            responder.datagram_received(wire, ("192.168.50.2", 4321))
        assert len(responder._discard_details) == 8
        assert delivered == []
        for secret in ("192.168.50", "8.8.8.8", "secret-video-data", "private-key", "private-password", payload.hex()):
            assert secret not in responder.diagnostics
        responder._on_segment = delivered.append
        responder.datagram_received(wire, responder._remote)
        assert delivered == [payload]
    asyncio.run(run())
