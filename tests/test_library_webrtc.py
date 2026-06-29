from __future__ import annotations

from custom_components.tuya_recordings.lib.webrtc import (
    WebRTCProbeSummary,
    browser_relay_candidate_list,
    filter_webrtc_candidates,
    ice_servers,
    mqtt_message_body,
    mqtt_message_type,
    normalize_outgoing_candidate,
    p2p_envelope,
    strip_sdp_candidates,
)


def test_ice_servers_normalizes_tuya_config():
    config = {
        "p2pConfig": {
            "ices": [
                {"url": "stun:one"},
                {"urls": "turn:two", "username": "user", "credential": "pass"},
            ]
        }
    }

    assert ice_servers(config) == [
        {"urls": "stun:one"},
        {"urls": "turn:two", "username": "user", "credential": "pass"},
    ]


def test_p2p_envelope_sets_header_and_message():
    envelope = p2p_envelope("msid", "dev", "moto", "session", "4.0", 312, "recordQueryByDay", {"day": 28}, tid="tid")

    assert envelope["protocol"] == 312
    assert envelope["data"]["header"]["from"] == "msid"
    assert envelope["data"]["header"]["to"] == "dev"
    assert envelope["data"]["header"]["sessionid"] == "session"
    assert envelope["data"]["header"]["type"] == "recordQueryByDay"
    assert envelope["data"]["header"]["tid"] == "tid"
    assert envelope["data"]["msg"] == {"day": 28}


def test_mqtt_message_helpers_support_flat_and_header_payloads():
    flat = {"data": {"type": "answer", "sessionid": "s1", "sdp": "v=0"}}
    nested = {"data": {"header": {"type": "playbackStart"}, "msg": {"resCode": 0}}}

    assert mqtt_message_type(flat) == "answer"
    assert mqtt_message_body(flat)["sdp"] == "v=0"
    assert mqtt_message_type(nested) == "playbackStart"
    assert mqtt_message_body(nested) == {"resCode": 0}


def test_sdp_and_candidate_helpers_match_browser_shape():
    sdp = "v=0\r\na=extmap:1 urn:test\r\na=candidate:1 1 udp 1 192.168.1.22 555 typ host\r\na=end-of-candidates\r\n"

    assert strip_sdp_candidates(sdp) == "v=0\r\na=extmap:1 urn:test\r\n"
    assert filter_webrtc_candidates(sdp).count("a=candidate:") == 1
    assert normalize_outgoing_candidate("a=candidate:1 1 udp 1 1.2.3.4 555 typ srflx") == "candidate:1 1 udp 1 1.2.3.4 555 typ srflx"
    assert browser_relay_candidate_list(
        [
            "candidate:1 1 udp 1 192.168.1.2 555 typ host",
            "candidate:2 1 udp 1 8.8.8.8 555 typ srflx raddr 192.168.1.2",
        ]
    ) == ["candidate:2 1 udp 1 8.8.8.8 555 typ srflx raddr 192.168.1.2"]


def test_probe_summary_records_events():
    summary = WebRTCProbeSummary()

    summary.add_helper_event({"type": "iceState", "state": "connected"})
    summary.add_helper_event({"type": "connectionState", "state": "connected"})
    summary.add_helper_event({"type": "track"})
    summary.add_helper_event({"type": "result", "bytes": 123})
    summary.add_mqtt_message("candidate")

    text = summary.describe()
    assert "ice=connected" in text
    assert "connection=connected" in text
    assert "tracks=1" in text
    assert "result_bytes=123" in text
    assert "candidate:1" in text
