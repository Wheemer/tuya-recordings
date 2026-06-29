"""WebRTC and Tuya P2P message helpers."""

from __future__ import annotations

import ipaddress
import time
from typing import Any


class WebRTCProbeSummary:
    def __init__(self) -> None:
        self.ice_state = ""
        self.connection_state = ""
        self.tracks = 0
        self.first_rtp = 0
        self.result_bytes = 0
        self.remote_candidates = 0
        self.remote_candidates_added = 0
        self.mqtt_messages: dict[str, int] = {}
        self.helper_events: dict[str, int] = {}

    def add_helper_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if not event_type:
            return
        self.helper_events[event_type] = self.helper_events.get(event_type, 0) + 1
        if event_type == "iceState":
            self.ice_state = str(event.get("state") or "")
        elif event_type == "connectionState":
            self.connection_state = str(event.get("state") or "")
        elif event_type == "track":
            self.tracks += 1
        elif event_type == "firstRTP":
            self.first_rtp += 1
        elif event_type == "result":
            self.result_bytes = max(self.result_bytes, int(event.get("bytes") or 0))

    def add_mqtt_message(self, message_type: str | None) -> None:
        if not message_type:
            return
        self.mqtt_messages[message_type] = self.mqtt_messages.get(message_type, 0) + 1

    def describe(self) -> str:
        helper = ",".join(f"{key}:{value}" for key, value in sorted(self.helper_events.items())) or "none"
        mqtt_seen = ",".join(f"{key}:{value}" for key, value in sorted(self.mqtt_messages.items())) or "none"
        return (
            f"ice={self.ice_state or 'unknown'}, connection={self.connection_state or 'unknown'}, "
            f"tracks={self.tracks}, first_rtp={self.first_rtp}, result_bytes={self.result_bytes}, "
            f"remote_candidates={self.remote_candidates}, added={self.remote_candidates_added}, "
            f"mqtt={mqtt_seen}, helper_events={helper}"
        )


def ice_servers(config: dict[str, Any]) -> list[dict[str, Any]]:
    servers: list[dict[str, Any]] = []
    for item in (config.get("p2pConfig") or {}).get("ices") or []:
        urls = item.get("urls") or item.get("url")
        if not urls:
            continue
        server: dict[str, Any] = {"urls": urls}
        if item.get("username"):
            server["username"] = item["username"]
        if item.get("credential"):
            server["credential"] = item["credential"]
        servers.append(server)
    return servers


def mqtt_message_type(payload: dict[str, Any]) -> str | None:
    data = payload.get("data") or {}
    return data.get("type") or (data.get("header") or {}).get("type")


def mqtt_message_body(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") or {}
    msg = data.get("msg")
    return msg if isinstance(msg, dict) else data


def mqtt_session_id(payload: dict[str, Any]) -> str | None:
    data = payload.get("data") or {}
    return data.get("sessionid") or (data.get("header") or {}).get("sessionid")


def strip_sdp_candidates(sdp: str) -> str:
    lines = []
    for line in sdp.splitlines():
        if line.startswith("a=candidate:") or line.startswith("a=end-of-candidates"):
            continue
        lines.append(line)
    return "\r\n".join(lines) + "\r\n"


def filter_local_candidate_list(candidates: list[str]) -> list[str]:
    private_candidates: list[tuple[tuple[int, str], str]] = []
    for candidate in candidates:
        if " typ host " not in candidate or " 1 udp " not in candidate.lower():
            continue
        parts = candidate.split()
        if len(parts) < 5:
            continue
        try:
            address = ipaddress.ip_address(parts[4])
        except ValueError:
            continue
        if isinstance(address, ipaddress.IPv4Address) and address.is_private:
            private_candidates.append((candidate_priority(address), candidate))
    if private_candidates:
        return [candidate for _, candidate in sorted(private_candidates)[:1]]

    primary = [candidate for candidate in candidates if " typ host " in candidate and " 1 udp " in candidate.lower()]
    return primary[:1] if primary else candidates


def browser_relay_candidate_list(candidates: list[str]) -> list[str]:
    browser_like = [
        candidate
        for candidate in candidates
        if (" typ srflx " in candidate or " typ relay " in candidate) and " 1 udp " in candidate.lower()
    ]
    return browser_like or filter_local_candidate_list(candidates)


def normalize_outgoing_candidate(candidate: str) -> str:
    return candidate.strip().removeprefix("a=").strip()


def p2p_envelope(
    msid: str,
    dev_id: str,
    moto_id: str,
    session_id: str,
    protocol_version: str,
    protocol: int,
    msg_type: str,
    msg: dict[str, Any],
    tid: str = "",
) -> dict[str, Any]:
    return {
        "protocol": protocol,
        "pv": "2.2",
        "t": int(time.time()),
        "data": {
            "header": {
                "from": msid,
                "to": dev_id,
                "sub_dev_id": "",
                "sessionid": session_id,
                "moto_id": moto_id,
                "type": msg_type,
                "tid": tid,
            },
            "msg": msg,
        },
    }


def filter_webrtc_candidates(sdp: str) -> str:
    lines = sdp.splitlines()
    candidate_ips: list[ipaddress.IPv4Address] = []
    for line in lines:
        if not line.startswith("a=candidate:"):
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            address = ipaddress.ip_address(parts[4])
        except ValueError:
            continue
        if isinstance(address, ipaddress.IPv4Address) and address.is_private:
            candidate_ips.append(address)

    if not candidate_ips:
        return sdp

    keep_ip = sorted(candidate_ips, key=candidate_priority)[0]
    filtered = [line for line in lines if not line.startswith("a=candidate:") or f" {keep_ip} " in line]
    return "\n".join(filtered) + "\n"


def candidate_priority(address: ipaddress.IPv4Address) -> tuple[int, str]:
    text = str(address)
    if text.startswith("192.168."):
        return (0, text)
    if text.startswith("10."):
        return (1, text)
    if text.startswith("172."):
        return (2, text)
    return (3, text)
