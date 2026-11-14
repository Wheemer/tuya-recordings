"""APK discovery wire requests, filtered replies and bounded cleanup."""

import json
import struct
import ipaddress
from types import SimpleNamespace

import pytest
import tinytuya

from custom_components.tuya_recordings.lib import native_lan_discovery as module


@pytest.mark.parametrize("reply_on", ["limited", "subnet", "silent"])
@pytest.mark.parametrize("legacy", [False, True])
def test_apk_discovery_requests_and_strict_identity(monkeypatch, reply_on, legacy, bind_denied=False):
    clock = [0.0]
    sent = []
    sockets = []
    queued = []
    returned = False

    def response(dev):
        data = json.dumps({"gwId": dev, "ip": "192.168.4.9", "version": "3.5"}).encode()
        if legacy:
            payload = tinytuya.AESCipher(tinytuya.udpkey).encrypt(data, use_base64=False)
            packet = tinytuya.TuyaMessage(1, 19, 0, struct.pack(">I", 0) + payload, 0, True, tinytuya.PREFIX_55AA_VALUE, None)
            return tinytuya.pack_message(packet)
        packet = tinytuya.TuyaMessage(1, 19, None, data, 0, True, tinytuya.PREFIX_6699_VALUE, True)
        return tinytuya.pack_message(packet, hmac_key=tinytuya.udpkey)

    class Socket:
        def __init__(self, *args):
            self.closed = False
            self.options = []
            sockets.append(self)
        def setsockopt(self, *args):
            if bind_denied and args[1] == module.socket.SO_BINDTODEVICE:
                raise PermissionError("interface binding denied")
            self.options.append(args)
        def bind(self, address):
            assert (module.socket.SOL_SOCKET, module.socket.SO_BINDTODEVICE, b"eth0\0") in self.options
            self.address = address
        def close(self): self.closed = True
        def sendto(self, payload, destination):
            nonlocal returned
            decoded = tinytuya.unpack_message(payload, hmac_key=tinytuya.udpkey, no_retcode=None)
            assert decoded.crc_good and decoded.cmd == 37
            assert json.loads(decoded.payload) == {"ip": "192.168.4.2", "from": "app"}
            sent.append((clock[0], destination))
            wanted = "255.255.255.255" if reply_on == "limited" else "192.168.4.255"
            if reply_on != "silent" and destination[0] == wanted and not returned:
                returned = True
                damaged = bytearray(response("camera"))
                damaged[-8] ^= 1
                queued.extend([b"invalid", bytes(damaged), (response("camera"), ("10.9.1.3", 6667)), response("wrong-camera"), response("camera")])
        def recvfrom(self, size):
            item = queued.pop(0)
            return item if isinstance(item, tuple) else (item, ("192.168.4.9", 6667))

    def ready(readers, _write, _error, timeout):
        clock[0] += timeout
        return ([readers[0]] if queued else []), [], []

    real = module.socket
    monkeypatch.setattr(module, "socket", SimpleNamespace(
        **{name: getattr(real, name) for name in ("AF_INET", "SOCK_DGRAM", "SOL_SOCKET", "SO_REUSEADDR", "SO_BROADCAST", "SO_BINDTODEVICE")},
        socket=Socket,
    ))
    monkeypatch.setattr(module.select, "select", ready)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module, "get_ip_to_broadcast", lambda: {"192.168.4.255": "192.168.4.2"})
    monkeypatch.setattr(module, "_lan_interface", lambda address: ("eth0", ipaddress.ip_network("192.168.4.0/24")))
    if bind_denied:
        with pytest.raises(PermissionError, match="interface binding denied"):
            module.find_lan_device("camera")
        assert not sent and all(sock.closed for sock in sockets)
        return
    found = module.find_lan_device("camera")
    assert found == ({} if reply_on == "silent" else {"id": "camera", "ip": "192.168.4.9", "version": "3.5"})
    assert sent[0] == (0, ("255.255.255.255", 7000))
    if reply_on != "limited":
        assert sent[1] == (3, ("192.168.4.255", 7000))
    assert len(sent) <= 2 and clock[0] <= 6
    assert all(sock.closed for sock in sockets)
    assert all(sock.address[0] not in ("", "0.0.0.0") for sock in sockets)


def test_discovery_fails_closed_when_interface_binding_is_denied(monkeypatch):
    test_apk_discovery_requests_and_strict_identity(monkeypatch, "limited", False, bind_denied=True)
