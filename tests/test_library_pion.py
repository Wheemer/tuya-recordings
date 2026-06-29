from __future__ import annotations

import subprocess

from custom_components.tuya_recordings.lib import pion


def test_create_webrtc_offer_filters_helper_sdp(tmp_path):
    helper_path = tmp_path / "pion_offer"
    helper_path.write_text("helper", encoding="utf-8")

    class FakeProcessModule:
        @staticmethod
        def run(command, **kwargs):
            sdp = "\n".join(
                [
                    "v=0",
                    "a=candidate:1 1 udp 1 172.17.0.1 10000 typ host",
                    "a=candidate:2 1 udp 1 192.168.0.108 10001 typ host",
                ]
            )
            return subprocess.CompletedProcess(command, 0, sdp, "")

    offer = pion.create_webrtc_offer(helper_path, process_module=FakeProcessModule)

    assert "192.168.0.108" in offer
    assert "172.17.0.1" not in offer


def test_start_pion_helper_sets_recording_environment(tmp_path):
    helper_path = tmp_path / "pion_offer"
    helper_path.write_text("helper", encoding="utf-8")
    captured = {}

    class FakeProcessModule:
        PIPE = subprocess.PIPE

        @staticmethod
        def Popen(command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]
            return object()

    result = pion.start_pion_helper(
        helper_path,
        {"p2pConfig": {"ices": [{"urls": "stun:example"}]}},
        query_timeout=25,
        playback_timeout=45,
        h264_output=tmp_path / "clip.h264",
        process_module=FakeProcessModule,
    )

    assert result is not None
    assert captured["command"][-1] == "45s"
    assert captured["env"]["TUYA_RECORDINGS_ICE_SERVERS"] == '[{"urls": "stun:example"}]'
    assert captured["env"]["TUYA_RECORDINGS_H264_OUTPUT"].endswith("clip.h264")
    assert captured["env"]["TUYA_RECORDINGS_FORCE_STUN_CONTROLLED"] == "1"
