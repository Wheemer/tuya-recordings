"""Bounded foreground LAN discovery matching GwBroadcastMonitorService."""

import ipaddress
import json
import select
import socket
import struct
import time
from contextlib import ExitStack

import tinytuya
from tinytuya.scanner import get_ip_to_broadcast


def find_lan_device(device_id: str) -> dict:
    """Listen before sending command 37 to limited and subnet broadcasts."""
    with ExitStack() as stack:
        listeners = []
        for port in (6666, 6667, 7000):
            listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            stack.callback(listener.close)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind(("", port))
            except OSError:
                continue
            listeners.append(listener)
        if not listeners:
            raise OSError("No LAN discovery listener could be opened")
        senders = []
        for broadcast, address in get_ip_to_broadcast().items():
            try:
                local = ipaddress.ip_address(address)
            except ValueError:
                continue
            if local.version != 4 or local.is_loopback or local.is_unspecified:
                continue
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            stack.callback(sender.close)
            sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                sender.bind((address, 0))
            except OSError:
                continue
            data = json.dumps({"ip": address, "from": "app"}, separators=(",", ":")).encode()
            message = tinytuya.TuyaMessage(0, 37, None, data, 0, True, tinytuya.PREFIX_6699_VALUE, True)
            payload = tinytuya.pack_message(message, hmac_key=tinytuya.udpkey)
            senders.append((sender, payload, broadcast))
            try:
                sender.sendto(payload, ("255.255.255.255", 7000))
            except OSError:
                pass
        start = time.monotonic()
        subnet_sent = False
        received = 0
        while received < 512:
            elapsed = time.monotonic() - start
            if elapsed >= 6:
                break
            if not subnet_sent and elapsed >= 3:
                subnet_sent = True
                for sender, payload, broadcast in senders:
                    if broadcast == "255.255.255.255":
                        continue
                    try:
                        sender.sendto(payload, (broadcast, 7000))
                    except OSError:
                        pass
            delay = min(6 - elapsed, 0.25)
            ready, _, _ = select.select(listeners, (), (), delay)
            for listener in ready:
                raw, peer = listener.recvfrom(65536)
                received += 1
                try:
                    header = tinytuya.parse_header(raw)
                    if header.prefix == tinytuya.PREFIX_6699_VALUE:
                        packet = tinytuya.unpack_message(raw, hmac_key=tinytuya.udpkey, no_retcode=None)
                    else:
                        packet = tinytuya.unpack_message(raw)
                    if not packet.crc_good:
                        continue
                    data = json.loads(tinytuya.decrypt_udp(raw))
                    if not isinstance(data, dict) or data.get("gwId", data.get("devId")) != device_id:
                        continue
                    address = ipaddress.ip_address(data.get("ip", peer[0]))
                    version = str(data.get("version", ""))
                    if (address.version != 4 or not address.is_private or address.is_loopback
                            or address.is_unspecified or version not in {"3.1", "3.2", "3.3", "3.4", "3.5"}):
                        continue
                    return {"id": device_id, "ip": str(address), "version": version}
                except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, struct.error, tinytuya.DecodeError):
                    continue
        return {}
