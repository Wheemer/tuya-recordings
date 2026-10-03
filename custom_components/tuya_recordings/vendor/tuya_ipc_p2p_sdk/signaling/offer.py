"""The SDP offer, which negotiates ``AES/KCP`` rather than DTLS-SRTP."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SdpOffer:
    """The offer sent to the device, and the keys it commits the session to."""

    sdp: str
    ice_ufrag: str
    ice_password: str
    aes_key: bytes
    session_id: str


def build_offer(
    uid: str,
    session_id: str,
    epoch_seconds: int,
    ice_ufrag: str,
    ice_password: str,
    aes_key: bytes,
) -> SdpOffer:
    """
    Build the offer for one session.

    The caller supplies the per-connection session and media values generated
    by the native ``connect_v3`` path.
    """
    lines = (
        "v=0",
        f"o=- {epoch_seconds} 1 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "a=group:BUNDLE imm0",
        f"a=msid-semantic: WMS {session_id}",
        "m=application 9 imm 6001",
        "c=IN IP4 0.0.0.0",
        "a=rtcp:9 IN IP4 0.0.0.0",
        f"a=ice-ufrag:{ice_ufrag}",
        f"a=ice-pwd:{ice_password}",
        "a=ice-options:trickle",
        f"a=aes-key:{aes_key.hex()}",
        "a=mid:imm0",
        "a=rtpmap:6001 AES/KCP 330",
        f"a=ssrc:0 cname:{uid}",
        "",
    )
    return SdpOffer(
        sdp="\r\n".join(lines),
        ice_ufrag=ice_ufrag,
        ice_password=ice_password,
        aes_key=aes_key,
        session_id=session_id,
    )
