"""
The copy of the signaling that rides the relay itself.

After the handshake the client re-sends its offer and candidates over the relay
with ``"path": "relay"``. This is what binds the relay connection to the media
session; without it the device joins the rendezvous and never streams.
"""

from __future__ import annotations

from ..json_types import JsonObject, JsonValue, dump_json


def _header(
    message_type: str,
    uid: str,
    device_id: str,
    session_id: str,
    trace_id: str,
) -> JsonObject:
    """Build the header shared by every relay-tunnelled signaling frame."""
    return {
        "type": message_type,
        "from": uid,
        "to": device_id,
        "sessionid": session_id,
        "moto_id": "",
        "trace_id": trace_id,
        "path": "relay",
    }


def relay_offer_frame(
    uid: str,
    device_id: str,
    session_id: str,
    trace_id: str,
    sdp: str,
    ice_servers: JsonValue,
    tcp_token: JsonValue,
    log_config: JsonValue,
) -> bytes:
    """Build the tunnelled offer."""
    header = _header("offer", uid, device_id, session_id, trace_id)
    header.update(
        {
            "is_pre": 0,
            "p2p_skill": 1635,
            "security_level": 3,
        }
    )
    return dump_json(
        {
            "header": header,
            "msg": {
                "sdp": sdp,
                "preconnect": True,
                "token": ice_servers,
                "tcp_token": tcp_token,
                "log": log_config,
            },
        }
    )


def relay_candidate_frame(
    uid: str,
    device_id: str,
    session_id: str,
    trace_id: str,
    candidate: str,
) -> bytes:
    """Build one tunnelled ICE candidate."""
    return dump_json(
        {
            "header": _header("candidate", uid, device_id, session_id, trace_id),
            "msg": {"candidate": candidate},
        }
    )
