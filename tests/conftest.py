"""Python tests are offline by default; loopback fixtures are the only exception."""

import ipaddress
import socket

import homeassistant  # noqa: F401  # Initialize HA schema compatibility.
import pytest


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch):
    attempts = []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto
    original_lookup = socket.getaddrinfo

    def check_host(host):
        if host in (None, "localhost", b"localhost"):
            return
        try:
            value = host.decode("ascii") if isinstance(host, bytes) else host
            if ipaddress.ip_address(value).is_loopback:
                return
        except (ValueError, TypeError, UnicodeError):
            pass
        attempts.append(True)
        raise RuntimeError("External networking is forbidden in Tuya Recordings tests")

    def check_address(address):
        if isinstance(address, tuple):
            check_host(address[0])

    def connect(sock, address):
        check_address(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check_address(address)
        return original_connect_ex(sock, address)

    def sendto(sock, data, *args):
        check_address(args[-1])
        return original_sendto(sock, data, *args)

    def lookup(host, *args, **kwargs):
        check_host(host)
        return original_lookup(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    yield
    assert not attempts, "A test attempted external networking, even if its error was caught"
