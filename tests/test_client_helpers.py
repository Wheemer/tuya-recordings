import json
import queue
from datetime import date, datetime, timezone

from custom_components.tuya_recordings.client import (
    TuyaRecordingsAuthError,
    TuyaRecordingsClient,
    _best_clip_match,
    _drain_helper_events,
    _filter_webrtc_candidates,
    _pion_helper_path,
    _normalize_clip,
    _strip_sdp_candidates,
    _browser_relay_candidate_list,
    _normalize_outgoing_candidate,
)
import custom_components.tuya_recordings.client as client_module
import custom_components.tuya_recordings.lib.ipc as ipc_module


def _fake_mp4() -> bytes:
    return b"\x00\x00\x00\x18ftypmp42" + (b"\x00" * 2048) + b"moov"


def test_filter_webrtc_candidates_prefers_lan_candidate():
    sdp = "\n".join(
        [
            "v=0",
            "a=candidate:1 1 udp 1 172.17.0.1 10000 typ host",
            "a=candidate:2 1 udp 1 192.168.0.108 10001 typ host",
            "a=candidate:3 1 udp 1 100.80.67.29 10002 typ host",
            "a=end-of-candidates",
        ]
    )

    filtered = _filter_webrtc_candidates(sdp)

    assert "192.168.0.108" in filtered
    assert "172.17.0.1" not in filtered
    assert "100.80.67.29" not in filtered


def test_filter_webrtc_candidates_leaves_sdp_without_private_ipv4_alone():
    sdp = "v=0\na=candidate:1 1 udp 1 2607:f2c0::1 10000 typ host\n"

    assert _filter_webrtc_candidates(sdp) == sdp


def test_strip_sdp_candidates_keeps_extmaps():
    sdp = "\n".join(
        [
            "v=0",
            "a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level",
            "a=candidate:1 1 udp 1 192.168.0.108 10001 typ host",
            "a=end-of-candidates",
        ]
    )

    filtered = _strip_sdp_candidates(sdp)

    assert "a=extmap:1" in filtered
    assert "a=candidate:" not in filtered
    assert "a=end-of-candidates" not in filtered


def test_browser_relay_candidate_list_prefers_srflx_and_relay():
    candidates = [
        "a=candidate:1 1 udp 2130706431 192.168.0.108 10001 typ host ufrag abc",
        "a=candidate:2 1 udp 1694498815 192.0.210.201 10002 typ srflx raddr 0.0.0.0 rport 10002 ufrag abc",
        "a=candidate:3 1 udp 16777215 15.204.74.43 10003 typ relay raddr 0.0.0.0 rport 10003 ufrag abc",
        "a=candidate:4 2 udp 16777215 15.204.74.43 10003 typ relay raddr 0.0.0.0 rport 10003 ufrag abc",
    ]

    filtered = _browser_relay_candidate_list(candidates)

    assert filtered == [candidates[1], candidates[2]]


def test_normalize_outgoing_candidate_keeps_browser_tail():
    candidate = "a=candidate:2 1 udp 1694498815 192.0.210.201 10002 typ srflx generation 0 ufrag abc network-cost 999"

    assert _normalize_outgoing_candidate(candidate).endswith("ufrag abc network-cost 999")


def test_normalize_clip_accepts_epoch_seconds():
    clip = _normalize_clip({"st": 1782488168, "ed": 1782488206})

    assert clip is not None
    assert clip["start"] == 1782488168
    assert clip["end"] == 1782488206
    assert clip["date"] == date.fromtimestamp(1782488168).isoformat()


def test_normalize_clip_accepts_epoch_milliseconds():
    clip = _normalize_clip({"startTime": 1782488168000, "endTime": 1782488206000})

    assert clip is not None
    assert clip["start"] == 1782488168
    assert clip["end"] == 1782488206


def test_cached_camera_index_does_not_refresh_without_cache():
    client = TuyaRecordingsClient({})

    index = client.cached_camera_index()

    assert index["cameras"] == []
    assert "No cached recordings yet" in index["warning"]


def test_client_constructor_does_not_load_cache(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        '{"expiresAt":"2099-01-01T00:00:00+00:00","index":{"cameras":[{"clips":[{}]}]}}',
        encoding="utf-8",
    )

    client = TuyaRecordingsClient({}, cache_path=cache_path)

    assert client._camera_index_cache is None


def test_client_update_options_changes_storage_path_without_rebuild(tmp_path):
    old_path = tmp_path / "old"
    new_path = tmp_path / "new"
    client = TuyaRecordingsClient({"media_storage_path": str(old_path)})

    client.update_options({"media_storage_path": str(new_path), "media_sync_enabled": True, "media_sync_hours": 0})

    assert client.media_storage_path == new_path
    assert client.media_sync_enabled is True


def test_clip_ready_requires_mp4_signature(tmp_path):
    client = TuyaRecordingsClient({}, media_storage_path=tmp_path)
    clip = client.clip_path("camera", 10, 20)
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"junk" * 300)

    assert client.clip_ready("camera", 10, 20) is False

    clip.write_bytes(_fake_mp4())

    assert client.clip_ready("camera", 10, 20) is True


def test_clip_cached_uses_fast_mp4_header_check(tmp_path):
    client = TuyaRecordingsClient({}, media_storage_path=tmp_path)
    clip = client.clip_path("camera", 10, 20)
    clip.parent.mkdir(parents=True)

    clip.write_bytes(b"junk" * 300)
    assert client.clip_cached("camera", 10, 20) is False

    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + (b"\x00" * 2048))
    assert client.clip_cached("camera", 10, 20) is True


def test_download_clip_removes_invalid_cached_file(monkeypatch, tmp_path):
    client = TuyaRecordingsClient({}, media_storage_path=tmp_path)
    output = client.clip_path("camera", 10, 20)
    output.parent.mkdir(parents=True)
    output.write_bytes(b"junk" * 300)
    attempted = {}

    def fake_bootstrap(dev_id):
        return {"motoId": "moto", "auth": "auth"}, {"msid": "msid", "password": "password"}

    def fake_remux(h264_path, temp_output_path):
        attempted["started"] = True
        temp_output_path.write_bytes(_fake_mp4())
        return None

    def fake_download(dev_id, config, mqtt_auth, start, end, h264_path, **kwargs):
        h264_path.write_bytes(b"h264")

    def fake_finalize(temp_output_path, output_path):
        attempted["finalized"] = True
        output_path.write_bytes(temp_output_path.read_bytes())

    monkeypatch.setattr(client, "_ipc_bootstrap", fake_bootstrap)
    monkeypatch.setattr(client, "_ipc_download_clip_h264", fake_download)
    monkeypatch.setattr(client_module, "_start_live_h264_remux", fake_remux)
    monkeypatch.setattr(client_module, "_finish_live_h264_remux", lambda proc, path: None)
    monkeypatch.setattr(client_module, "_finalize_mp4_for_browser", fake_finalize)
    monkeypatch.setattr(client, "ensure_thumbnail", lambda dev_id, start, end: None)

    client.download_clip("camera", 10, 20, output)

    assert attempted["started"] is True
    assert attempted["finalized"] is True
    assert output.read_bytes() == _fake_mp4()


def test_download_clip_retries_cache_stream_with_queried_bounds(monkeypatch, tmp_path):
    client = TuyaRecordingsClient({}, media_storage_path=tmp_path)
    output = client.clip_path("camera", 10, 20)
    output.parent.mkdir(parents=True)
    attempts = []

    def fake_bootstrap(dev_id):
        return {"motoId": "moto", "auth": "auth"}, {"msid": "msid", "password": "password"}

    class FakeProcess:
        def poll(self):
            return 0

    def fake_remux(h264_path, temp_output_path):
        return FakeProcess()

    def fake_download(dev_id, config, mqtt_auth, start, end, h264_path, **kwargs):
        attempts.append(kwargs["verify_clip"])
        if not kwargs["verify_clip"]:
            raise RuntimeError("no media")
        h264_path.write_bytes(b"h264")

    def fake_finish(proc, temp_output_path):
        temp_output_path.write_bytes(_fake_mp4())

    def fake_finalize(temp_output_path, output_path):
        output_path.write_bytes(temp_output_path.read_bytes())

    monkeypatch.setattr(client, "_ipc_bootstrap", fake_bootstrap)
    monkeypatch.setattr(client, "_ipc_download_clip_h264", fake_download)
    monkeypatch.setattr(client_module, "_start_live_h264_remux", fake_remux)
    monkeypatch.setattr(client_module, "_finish_live_h264_remux", fake_finish)
    monkeypatch.setattr(client_module, "_finalize_mp4_for_browser", fake_finalize)
    monkeypatch.setattr(client, "ensure_thumbnail", lambda dev_id, start, end: None)

    client.download_clip("camera", 10, 20, output, verify_clip=False)

    assert attempts == [False, True]
    assert output.read_bytes() == _fake_mp4()


def test_download_clip_recovers_partial_temp_mp4_before_retry(monkeypatch, tmp_path):
    client = TuyaRecordingsClient({}, media_storage_path=tmp_path)
    output = client.clip_path("camera", 10, 20)
    output.parent.mkdir(parents=True)
    attempts = []

    def fake_bootstrap(dev_id):
        return {"motoId": "moto", "auth": "auth"}, {"msid": "msid", "password": "password"}

    class FakeProcess:
        def poll(self):
            return 0

    def fake_remux(h264_path, temp_output_path):
        temp_output_path.write_bytes(b"partial" * 300)
        return FakeProcess()

    def fake_download(dev_id, config, mqtt_auth, start, end, h264_path, **kwargs):
        attempts.append(kwargs["verify_clip"])
        raise RuntimeError("helper result said zero bytes")

    def fake_finish(proc, temp_output_path):
        temp_output_path.write_bytes(_fake_mp4())

    def fake_finalize(temp_output_path, output_path):
        output_path.write_bytes(temp_output_path.read_bytes())

    monkeypatch.setattr(client, "_ipc_bootstrap", fake_bootstrap)
    monkeypatch.setattr(client, "_ipc_download_clip_h264", fake_download)
    monkeypatch.setattr(client_module, "_start_live_h264_remux", fake_remux)
    monkeypatch.setattr(client_module, "_finish_live_h264_remux", fake_finish)
    monkeypatch.setattr(client_module, "_finalize_mp4_for_browser", fake_finalize)
    monkeypatch.setattr(client, "ensure_thumbnail", lambda dev_id, start, end: None)

    client.download_clip("camera", 10, 20, output, verify_clip=False)

    assert attempts == [False]
    assert output.read_bytes() == _fake_mp4()


def test_download_clip_promotes_partial_temp_mp4_when_faststart_finalize_fails(monkeypatch, tmp_path):
    client = TuyaRecordingsClient({}, media_storage_path=tmp_path)
    output = client.clip_path("camera", 10, 20)
    output.parent.mkdir(parents=True)

    def fake_bootstrap(dev_id):
        return {"motoId": "moto", "auth": "auth"}, {"msid": "msid", "password": "password"}

    class FakeProcess:
        def poll(self):
            return 0

    def fake_remux(h264_path, temp_output_path):
        temp_output_path.write_bytes(_fake_mp4())
        return FakeProcess()

    def fake_download(dev_id, config, mqtt_auth, start, end, h264_path, **kwargs):
        raise RuntimeError("helper result said zero bytes")

    def fake_finalize(temp_output_path, output_path):
        raise RuntimeError("faststart failed")

    monkeypatch.setattr(client, "_ipc_bootstrap", fake_bootstrap)
    monkeypatch.setattr(client, "_ipc_download_clip_h264", fake_download)
    monkeypatch.setattr(client_module, "_start_live_h264_remux", fake_remux)
    monkeypatch.setattr(client_module, "_finish_live_h264_remux", lambda proc, path: None)
    monkeypatch.setattr(client_module, "_finalize_mp4_for_browser", fake_finalize)
    monkeypatch.setattr(client, "ensure_thumbnail", lambda dev_id, start, end: None)

    client.download_clip("camera", 10, 20, output, verify_clip=False)

    assert output.read_bytes() == _fake_mp4()


def test_load_cache_reads_cached_index_when_called_explicitly(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        '{"expiresAt":"2099-01-01T00:00:00+00:00","index":{"cameras":[{"clips":[{}]}]}}',
        encoding="utf-8",
    )
    client = TuyaRecordingsClient({}, cache_path=cache_path)

    client.load_cache()

    assert len(client.cached_camera_index()["cameras"]) == 1


def test_cached_camera_index_loads_newer_disk_cache(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        '{"expiresAt":"2099-01-01T00:00:00+00:00","index":{"cameras":[{"clips":[{}]}]}}',
        encoding="utf-8",
    )
    client = TuyaRecordingsClient({}, cache_path=cache_path)

    index = client.cached_camera_index()

    assert len(index["cameras"]) == 1


def test_camera_index_returns_busy_cache_when_refresh_lock_is_held():
    client = TuyaRecordingsClient({})

    assert client._refresh_lock.acquire(blocking=False)
    try:
        index = client.camera_index(force_refresh=True)
    finally:
        client._refresh_lock.release()

    assert index["cameras"] == []
    assert "already running" in index["warning"]


def test_clear_cache_skips_when_refresh_lock_is_held(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("{}", encoding="utf-8")
    client = TuyaRecordingsClient({}, cache_path=cache_path)
    client._camera_index_cache = {"cameras": [{"clips": []}]}

    assert client._refresh_lock.acquire(blocking=False)
    try:
        client.clear_cache()
    finally:
        client._refresh_lock.release()

    assert client._camera_index_cache is not None
    assert cache_path.exists()


def test_clear_cache_removes_memory_and_disk_cache(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("{}", encoding="utf-8")
    client = TuyaRecordingsClient({}, cache_path=cache_path)
    client._camera_index_cache = {"cameras": [{"clips": []}]}

    client.clear_cache()

    assert client._camera_index_cache is None
    assert not cache_path.exists()


def test_stale_cache_preserves_cached_cameras():
    client = TuyaRecordingsClient({})
    client._camera_index_cache = {"cameras": [{"name": "Camera"}]}

    index = client._stale_cache(datetime.now(timezone.utc), "temporary problem")

    assert index["stale"] is True
    assert index["warning"] == "temporary problem"
    assert index["cameras"] == [{"name": "Camera"}]


def test_stale_cache_persists_extended_cache_window(tmp_path):
    cache_path = tmp_path / "cache.json"
    client = TuyaRecordingsClient({}, cache_path=cache_path)
    client._camera_index_cache = {"cameras": [{"name": "Camera"}]}

    index = client._stale_cache(datetime.now(timezone.utc), "temporary problem")
    payload = json.loads(cache_path.read_text(encoding="utf-8"))

    assert payload["index"]["stale"] is True
    assert payload["index"]["warning"] == "temporary problem"
    assert payload["expiresAt"] == index["cacheExpiresAt"]


def test_camera_index_preserves_cached_clips_when_camera_query_fails(monkeypatch):
    client = TuyaRecordingsClient({})
    client._camera_index_cache = {
        "cameras": [
            {
                "devId": "camera-1",
                "clips": [{"start": 1, "end": 2}],
            }
        ]
    }

    monkeypatch.setattr(
        client,
        "_camera_devices",
        lambda: [{"devId": "camera-1", "category": "sp", "name": "Camera"}],
    )
    monkeypatch.setattr(
        client,
        "sd_recordings",
        lambda dev_id: (_ for _ in ()).throw(RuntimeError("temporary problem")),
    )

    index = client.camera_index(force_refresh=True)

    assert index["cameras"][0]["clips"] == [{"start": 1, "end": 2}]
    assert index["cameras"][0]["bridgeStatus"]["error"] == "temporary problem"


def test_camera_index_skips_recording_query_for_offline_camera(monkeypatch):
    client = TuyaRecordingsClient({})
    client._camera_index_cache = {
        "cameras": [
            {
                "devId": "camera-1",
                "clips": [{"start": 1, "end": 2}],
            }
        ]
    }

    def sd_recordings(dev_id):
        raise AssertionError("offline cameras should not be queried")

    monkeypatch.setattr(
        client,
        "_camera_devices",
        lambda: [{"devId": "camera-1", "category": "sp", "name": "Camera", "online": False}],
    )
    monkeypatch.setattr(client, "sd_recordings", sd_recordings)

    index = client.camera_index(force_refresh=True)

    assert index["cameras"][0]["clips"] == [{"start": 1, "end": 2}]
    assert "offline" in index["cameras"][0]["bridgeStatus"]["error"]


def test_camera_index_merges_new_clips_with_cached_clips(monkeypatch):
    client = TuyaRecordingsClient({})
    client._camera_index_cache = {
        "cameras": [
            {
                "devId": "camera-1",
                "clips": [
                    {"start": 1, "end": 2},
                    {"start": 3, "end": 4},
                ],
            }
        ]
    }

    monkeypatch.setattr(
        client,
        "_camera_devices",
        lambda: [{"devId": "camera-1", "category": "sp", "name": "Camera", "online": True}],
    )
    monkeypatch.setattr(client, "sd_recordings", lambda dev_id: ([{"start": 3, "end": 4}, {"start": 5, "end": 6}], ["2026-06-27"]))

    index = client.camera_index(force_refresh=True)

    assert index["cameras"][0]["clips"] == [
        {"start": 5, "end": 6},
        {"start": 3, "end": 4},
        {"start": 1, "end": 2},
    ]


def test_auth_error_is_specific_exception():
    error = TuyaRecordingsAuthError(
        "tuya_openapi",
        {"code": "missing_credentials", "msg": "Missing credentials"},
    )

    assert "missing_credentials" in str(error)


def test_best_clip_match_prefers_exact_bounds():
    clips = [
        {"start": 100, "end": 130},
        {"start": 101, "end": 130},
    ]

    assert _best_clip_match(clips, 100, 130) == {"start": 100, "end": 130}


def test_best_clip_match_uses_nearest_overlap():
    clips = [
        {"start": 80, "end": 95},
        {"start": 98, "end": 131},
        {"start": 50, "end": 180},
    ]

    assert _best_clip_match(clips, 100, 130) == {"start": 98, "end": 131}


def test_best_clip_match_returns_none_without_overlap():
    clips = [
        {"start": 10, "end": 20},
        {"start": 140, "end": 160},
    ]

    assert _best_clip_match(clips, 100, 130) is None


def test_pion_helper_path_prefers_arch_specific_binary(monkeypatch, tmp_path):
    helper = tmp_path / "pion_offer_linux_amd64"
    helper.write_text("", encoding="utf-8")
    fallback = tmp_path / "pion_offer"
    fallback.write_text("", encoding="utf-8")

    monkeypatch.setattr("custom_components.tuya_recordings.lib.ipc.platform.system", lambda: "Linux")
    monkeypatch.setattr("custom_components.tuya_recordings.lib.ipc.platform.machine", lambda: "x86_64")
    monkeypatch.setattr(ipc_module, "__file__", str(tmp_path / "lib" / "ipc.py"))

    assert _pion_helper_path().name == "pion_offer_linux_amd64"


def test_start_pion_helper_uses_browser_controlled_mode(monkeypatch, tmp_path):
    helper = tmp_path / "pion_offer_linux_amd64"
    helper.write_text("", encoding="utf-8")
    captured = {}

    class FakeProcess:
        pass

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr("custom_components.tuya_recordings.lib.ipc.platform.system", lambda: "Linux")
    monkeypatch.setattr("custom_components.tuya_recordings.lib.ipc.platform.machine", lambda: "x86_64")
    monkeypatch.setattr(ipc_module, "__file__", str(tmp_path / "lib" / "ipc.py"))
    monkeypatch.setattr(client_module.subprocess, "Popen", fake_popen)

    proc = client_module._start_pion_helper({"p2pConfig": {"ices": [{"urls": "stun:example"}]}})

    assert isinstance(proc, FakeProcess)
    assert captured["command"][0] == str(helper)
    assert captured["env"]["TUYA_RECORDINGS_DUMMY_AUDIO_SENDRECV"] == "1"
    assert captured["env"]["TUYA_RECORDINGS_TRICKLE_CANDIDATES"] == "1"
    assert captured["env"]["TUYA_RECORDINGS_CHROME_SDP"] == "1"
    assert captured["env"]["TUYA_RECORDINGS_FORCE_STUN_CONTROLLED"] == "1"


def test_thumbnail_autofill_only_uses_cached_videos(tmp_path):
    client = TuyaRecordingsClient({}, media_storage_path=tmp_path)
    ensured = []

    def fake_ensure_thumbnail(dev_id, start, end):
        ensured.append((dev_id, start, end))
        if start == 10:
            return tmp_path / "cached.jpg"
        return None

    client.ensure_thumbnail = fake_ensure_thumbnail

    result = client.populate_thumbnails_for_clips(
        "camera",
        [
            {"start": 10, "end": 20},
            {"start": 30, "end": 40},
            {"start": 50, "end": 60},
        ],
        limit=1,
    )

    assert ensured == [("camera", 10, 20)]
    assert result["created_from_cache"] == 1
    assert result["created"] == 0
    assert result["checked"] == 1


def test_populate_thumbnails_only_uses_cached_videos(monkeypatch, tmp_path):
    client = TuyaRecordingsClient({}, media_storage_path=tmp_path)
    monkeypatch.setattr(
        client,
        "cached_camera_index",
        lambda: {
            "cameras": [
                {
                    "devId": "camera",
                    "clips": [
                        {"start": 10, "end": 20},
                        {"start": 30, "end": 40},
                        {"start": 50, "end": 60},
                    ],
                }
            ]
        },
    )
    ensured = []

    def fake_ensure_thumbnail(dev_id, start, end):
        ensured.append((dev_id, start, end))
        if start in {10, 50}:
            return tmp_path / f"{start}.jpg"
        return None

    monkeypatch.setattr(client, "ensure_thumbnail", fake_ensure_thumbnail)

    result = client.populate_thumbnails(limit=1)

    assert ensured == [("camera", 10, 20)]
    assert result["created_from_cache"] == 1
    assert result["created"] == 0
    assert result["checked"] == 1


def test_populate_thumbnails_skips_uncached_videos(monkeypatch, tmp_path):
    client = TuyaRecordingsClient({}, media_storage_path=tmp_path)
    monkeypatch.setattr(
        client,
        "cached_camera_index",
        lambda: {
            "cameras": [
                {
                    "devId": "camera",
                    "clips": [
                        {"start": 10, "end": 20},
                        {"start": 30, "end": 40},
                        {"start": 50, "end": 60},
                    ],
                }
            ]
        },
    )
    monkeypatch.setattr(client, "ensure_thumbnail", lambda dev_id, start, end: None)

    result = client.populate_thumbnails(limit=3)

    assert result["failed"] == 0
    assert result["created"] == 0
    assert result["checked"] == 3
    assert result["skipped"] == 3


def test_sync_recordings_zero_hours_caches_all_discovered_clips(monkeypatch, tmp_path):
    client = TuyaRecordingsClient({"media_sync_enabled": True, "media_sync_hours": 0}, media_storage_path=tmp_path)
    force_refresh_values = []

    def fake_camera_index(force_refresh=False):
        force_refresh_values.append(force_refresh)
        return {
            "cameras": [
                {
                    "devId": "camera",
                    "clips": [
                        {"start": 10, "end": 20},
                        {"start": 1000, "end": 1020},
                    ],
                }
            ]
        }

    monkeypatch.setattr(
        client,
        "camera_index",
        fake_camera_index,
    )
    downloaded = []

    def fake_download_clip(dev_id, start, end, output_path, **kwargs):
        downloaded.append((dev_id, start, end))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"mp4")
        return output_path

    monkeypatch.setattr(client, "download_clip", fake_download_clip)

    result = client.sync_recordings()

    assert downloaded == [("camera", 1000, 1020), ("camera", 10, 20)]
    assert result["downloaded"] == 2
    assert force_refresh_values == [True]


def test_sync_recordings_positive_hours_limits_to_recent_discovered_clips(monkeypatch, tmp_path):
    client = TuyaRecordingsClient({"media_sync_enabled": True, "media_sync_hours": 1}, media_storage_path=tmp_path)
    old_video = client.clip_path("camera", 10, 20)
    old_thumb = client.thumbnail_path("camera", 10, 20)
    old_video.parent.mkdir(parents=True, exist_ok=True)
    old_thumb.parent.mkdir(parents=True, exist_ok=True)
    old_video.write_bytes(b"old")
    old_thumb.write_bytes(b"old")
    monkeypatch.setattr(
        client,
        "refresh_recent_recordings",
        lambda: {
            "cameras": [
                {
                    "devId": "camera",
                    "clips": [
                        {"start": 10, "end": 20},
                        {"start": 4000, "end": 4020},
                    ],
                }
            ]
        },
    )
    downloaded = []

    def fake_download_clip(dev_id, start, end, output_path, **kwargs):
        downloaded.append((dev_id, start, end))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"mp4")
        return output_path

    monkeypatch.setattr(client, "download_clip", fake_download_clip)

    result = client.sync_recordings()

    assert downloaded == [("camera", 4000, 4020)]
    assert result["downloaded"] == 1
    assert result["skipped"] == 1
    assert result["deleted_videos"] == 1
    assert result["deleted_thumbnails"] == 1
    assert not old_video.exists()
    assert not old_thumb.exists()
    status = client.diagnostics()["media_sync_status"]
    assert status["state"] == "idle"
    assert status["last_result"]["downloaded"] == 1


def test_sync_recordings_empty_index_does_not_delete_cache(monkeypatch, tmp_path):
    client = TuyaRecordingsClient({"media_sync_enabled": True, "media_sync_hours": 0}, media_storage_path=tmp_path)
    cached_video = client.clip_path("camera", 10, 20)
    cached_video.parent.mkdir(parents=True, exist_ok=True)
    cached_video.write_bytes(b"cached")
    monkeypatch.setattr(client, "camera_index", lambda force_refresh=False: {"cameras": []})

    result = client.sync_recordings()

    assert cached_video.exists()
    assert result["deleted_videos"] == 0


def test_recover_interrupted_media_sync_promotes_valid_temp_mp4(monkeypatch, tmp_path):
    client = TuyaRecordingsClient({"media_sync_enabled": True, "media_sync_hours": 0}, media_storage_path=tmp_path)
    final_path = client.clip_path("camera", 10, 20)
    temp_path = final_path.with_suffix(".tmp.mp4")
    pipe_path = final_path.with_name(f"{final_path.name}.h264.pipe")
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.write_bytes(_fake_mp4())
    pipe_path.write_bytes(b"")
    thumbnails = []

    def fake_ensure_thumbnail(dev_id, start, end):
        thumbnails.append((dev_id, start, end))

    monkeypatch.setattr(client, "ensure_thumbnail", fake_ensure_thumbnail)

    recovered = client._recover_interrupted_media_sync()

    assert recovered == 1
    assert final_path.read_bytes() == _fake_mp4()
    assert not temp_path.exists()
    assert not pipe_path.exists()
    assert thumbnails == [("camera", 10, 20)]


def test_sync_recordings_limits_failed_attempts_per_camera(monkeypatch, tmp_path):
    client = TuyaRecordingsClient({"media_sync_enabled": True, "media_sync_hours": 0}, media_storage_path=tmp_path)
    monkeypatch.setattr(client_module, "MEDIA_MAX_ATTEMPTS_PER_CAMERA", 2)
    monkeypatch.setattr(
        client,
        "refresh_recent_recordings",
        lambda: {
            "cameras": [
                {
                    "devId": "camera",
                    "clips": [
                        {"start": 10, "end": 20},
                        {"start": 30, "end": 40},
                        {"start": 50, "end": 60},
                        {"start": 70, "end": 80},
                    ],
                }
            ]
        },
    )
    attempted = []

    def fake_download_clip(dev_id, start, end, output_path, **kwargs):
        attempted.append((start, end, kwargs.get("log_traceback")))
        raise RuntimeError("no media")

    monkeypatch.setattr(client, "download_clip", fake_download_clip)
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: None)

    result = client.sync_recordings()

    assert attempted == [(70, 80, False), (50, 60, False)]
    assert result["failed"] == 2
    assert result["skipped"] == 2


def test_sync_recordings_limits_attempts_per_camera(monkeypatch, tmp_path):
    client = TuyaRecordingsClient({"media_sync_enabled": True, "media_sync_hours": 0}, media_storage_path=tmp_path)
    monkeypatch.setattr(client_module, "MEDIA_MAX_ATTEMPTS_PER_CAMERA", 3)
    monkeypatch.setattr(
        client,
        "camera_index",
        lambda force_refresh=False: {
            "cameras": [
                {
                    "devId": "camera",
                    "clips": [
                        {"start": 10, "end": 20},
                        {"start": 30, "end": 40},
                        {"start": 50, "end": 60},
                        {"start": 70, "end": 80},
                    ],
                }
            ]
        },
    )
    attempted = []

    def fake_download_clip(dev_id, start, end, output_path, **kwargs):
        attempted.append((start, end))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"mp4")
        return output_path

    monkeypatch.setattr(client, "download_clip", fake_download_clip)
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: None)

    result = client.sync_recordings()

    assert attempted == [(70, 80), (50, 60), (30, 40)]
    assert result["downloaded"] == 3
    assert result["skipped"] == 1


def test_sync_recordings_skips_recent_clips(monkeypatch, tmp_path):
    client = TuyaRecordingsClient({"media_sync_enabled": True, "media_sync_hours": 0}, media_storage_path=tmp_path)
    now = 10_000
    monkeypatch.setattr(client_module.time, "time", lambda: now)
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: None)
    recent_end = now - client_module.MEDIA_SYNC_MIN_CLIP_AGE + 10
    older_end = now - client_module.MEDIA_SYNC_MIN_CLIP_AGE - 10
    monkeypatch.setattr(
        client,
        "camera_index",
        lambda force_refresh=False: {
            "cameras": [
                {
                    "devId": "camera",
                    "clips": [
                        {"start": recent_end - 30, "end": recent_end},
                        {"start": older_end - 30, "end": older_end},
                    ],
                }
            ]
        },
    )
    attempted = []

    def fake_download_clip(dev_id, start, end, output_path, **kwargs):
        attempted.append((start, end))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"mp4")
        return output_path

    monkeypatch.setattr(client, "download_clip", fake_download_clip)

    result = client.sync_recordings()

    assert attempted == [(older_end - 30, older_end)]
    assert result["downloaded"] == 1
    assert result["skipped"] == 1


def test_sync_recordings_round_robins_cameras(monkeypatch, tmp_path):
    client = TuyaRecordingsClient({"media_sync_enabled": True, "media_sync_hours": 0}, media_storage_path=tmp_path)
    monkeypatch.setattr(
        client,
        "camera_index",
        lambda force_refresh=False: {
            "cameras": [
                {"devId": "one", "clips": [{"start": 30, "end": 40}, {"start": 10, "end": 20}]},
                {"devId": "two", "clips": [{"start": 300, "end": 310}, {"start": 100, "end": 110}]},
            ]
        },
    )
    attempted = []

    def fake_download_clip(dev_id, start, end, output_path, **kwargs):
        attempted.append((dev_id, start, end, kwargs.get("verify_clip")))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"mp4")
        return output_path

    monkeypatch.setattr(client, "download_clip", fake_download_clip)
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: None)

    result = client.sync_recordings()

    assert sorted(attempted) == [
        ("one", 10, 20, False),
        ("one", 30, 40, False),
        ("two", 100, 110, False),
        ("two", 300, 310, False),
    ]
    assert result["downloaded"] == 4


def test_sync_recordings_downloads_newest_first_per_camera(monkeypatch, tmp_path):
    client = TuyaRecordingsClient({"media_sync_enabled": True, "media_sync_hours": 0}, media_storage_path=tmp_path)
    monkeypatch.setattr(
        client,
        "refresh_recent_recordings",
        lambda: {
            "cameras": [
                {
                    "devId": "camera",
                    "clips": [
                        {"start": 10, "end": 20},
                        {"start": 30, "end": 40},
                        {"start": 100, "end": 110},
                    ],
                }
            ]
        },
    )
    attempted = []

    def fake_download_clip(dev_id, start, end, output_path, **kwargs):
        attempted.append((start, end))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"mp4")
        return output_path

    monkeypatch.setattr(client, "download_clip", fake_download_clip)

    result = client.sync_recordings()

    assert attempted == [(100, 110), (30, 40), (10, 20)]
    assert result["downloaded"] == 3


def test_drain_helper_events_publishes_local_candidates():
    events = queue.Queue()
    events.put({"type": "localCandidate", "candidate": "candidate:1 1 udp 1 192.168.0.108 123 typ host"})
    events.put({"type": "localCandidateEnd"})
    events.put({"type": "connectionState", "state": "connected"})
    published = []

    class FakeMqtt:
        def publish(self, topic, raw, qos):
            published.append((topic, json.loads(raw), qos))

    other_events = _drain_helper_events(
        events,
        FakeMqtt(),
        "/av/moto/signaling/u/camera",
        "msid",
        "camera",
        "signaling",
        "session",
        "2.3",
    )

    assert published[0][0] == "/av/moto/signaling/u/camera"
    assert published[0][1]["protocol"] == 302
    assert published[0][1]["data"]["msg"]["candidate"].startswith("candidate:1")
    assert published[0][2] == 1
    assert other_events == [{"type": "connectionState", "state": "connected"}]

