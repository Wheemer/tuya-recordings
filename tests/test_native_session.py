import hashlib
import json
from datetime import date

import pytest

from custom_components.tuya_recordings.lib.native_session import (
    APK_API_VERSION,
    APK_AUDIO_OUTPUT_FORMAT_PCM,
    APK_AUDIO_OUTPUT_FORMAT_RAWDATA,
    APK_RTC_CONFIG_API,
    APK_RTC_SESSION_INIT_API,
    APK_VIDEO_OUTPUT_FORMAT_RAWDATA,
    APK_VIDEO_OUTPUT_FORMAT_YUV,
    NativeCameraSessionConfig,
    NativeMediaFormat,
    NativePlaybackMethod,
    NativePlaybackRequest,
    NativeRecordingQuery,
    NativeSessionError,
    derive_native_password,
    native_encryption_info_json,
    native_playback_jni_args,
    native_playback_cpp_signatures,
    native_session_init_api_request,
    native_session_api_request,
    normalize_camera_info,
    record_query_method,
)


def camera_info(**updates):
    data = {
        "id": "device-p2p-id",
        "password": "camera-password",
        "p2pSpecifiedType": 4,
        "p2pConfig": {
            "session": {
                "icePassword": "pw",
                "iceUfrag": "ufrag",
                "aesKey": "0123456789abcdef",
                "sessionId": "sid",
            },
            "ices": [{"urls": "stun:127.0.0.1:3478"}],
        },
        "skill": json.dumps({
            "videos": [{"streamType": 2, "codecType": 4, "width": 1920, "height": 1080}],
            "audios": [{"codecType": 101, "sampleRate": 8000, "channels": 1}],
            "localStorage": 16777217,
            "webrtc": 3,
        }),
    }
    data.update(updates)
    return data


def test_native_api_request_matches_apk_overloads():
    assert native_session_api_request(
        "dev123", trace_id="ipc_p2p_android_dev123_1234"
    ) == (
        APK_RTC_CONFIG_API,
        APK_API_VERSION,
        {"devId": "dev123"},
        {"bizDM": "ipc", "ctId": "ipc_p2p_android_dev123_1234"},
    )

    assert native_session_init_api_request(
        "dev123", trace_id="ipc_p2p_android_dev123_1234"
    ) == (
        APK_RTC_SESSION_INIT_API,
        APK_API_VERSION,
        {"devId": "dev123"},
        {"bizDM": "ipc", "ctId": "ipc_p2p_android_dev123_1234"},
    )


def test_native_password_derivation_matches_apk_formula():
    expected = hashlib.md5(b"camera-password||local-key").hexdigest()
    assert derive_native_password("camera-password", "local-key") == expected


def test_normalize_camera_info_for_p2p_type_4():
    config = normalize_camera_info(
        "dev123",
        camera_info(p2pConfig={
            "auth": "auth-token",
            "motoId": "moto-1",
            "session": {
                "icePassword": "pw",
                "iceUfrag": "ufrag",
                "aesKey": "0123456789abcdef",
                "sessionId": "sid",
            },
            "ices": [{"urls": "stun:127.0.0.1:3478"}],
        }),
        local_key="local-key",
        trace_id="trace-1",
        preconnect=1,
        local_id="uid-1",
    )
    assert config.dev_id == "dev123"
    assert config.username == "admin"
    assert config.p2p_type == 4
    assert config.p2p_id == "device-p2p-id"
    assert config.preconnect == 1
    assert config.uses_connection_params is False
    assert config.local_storage == 16777217
    assert config.event_catalog_supported is False
    assert config.event_catalog_v2 is False
    assert config.playback_catalog_version == 0
    assert json.loads(config.token)["session"]["sessionId"] == "sid"
    assert config.p2p_config["auth"] == "auth-token"
    assert config.p2p_auth == "auth-token"
    assert config.p2p_moto_id == "moto-1"
    assert config.p2p_security_level == 3
    assert config.p2p_session_id == "sid"
    assert config.p2p_ice_servers == [{"urls": "stun:127.0.0.1:3478"}]
    assert json.loads(config.connect_ext_config_json) == {"trace_id": "trace-1"}
    assert "camera-password" not in repr(config)
    assert "local-key" not in repr(config)
    assert config.derived_password == derive_native_password("camera-password", "local-key")


def test_playback_catalog_v3_uses_the_apk_capability_bit():
    config = normalize_camera_info(
        "dev123",
        camera_info(skill=json.dumps({"localStorage": 134217728})),
        local_key="local-key",
        trace_id="trace-v3",
    )

    assert config.playback_catalog_version == 2


def test_playback_mode_uses_the_apk_local_storage_capability_bit():
    base = normalize_camera_info(
        "dev123",
        camera_info(skill=json.dumps({"localStorage": 1})),
        local_key="local-key",
        trace_id="trace-standard",
    )
    supported = normalize_camera_info(
        "dev123",
        camera_info(skill=json.dumps({"localStorage": 129})),
        local_key="local-key",
        trace_id="trace-play-mode",
    )

    assert supported.playback_mode_supported is True
    assert base.playback_mode_supported is False


def test_normalize_camera_info_for_p2p_type_8_uses_connection_params():
    config = normalize_camera_info(
        "dev123",
        camera_info(p2pSpecifiedType=8, p2pConfig=json.dumps({"session": {"sessionId": "sid8"}})),
        local_key="local-key",
        trace_id="trace-8",
    )
    assert config.p2p_type == 8
    assert config.uses_connection_params is True
    assert json.loads(config.token)["session"]["sessionId"] == "sid8"
    assert json.loads(config.config_json)["p2pConfig"] == {
        "session": {"sessionId": "sid8"}
    }


def test_native_encryption_info_json_matches_apk_set_encryption_info_shape():
    payload = native_encryption_info_json(
        "dev123",
        [
            {"uuid": "clip-1", "secretKey": "secret-1"},
            {"uuid": "clip-2", "encrypt": "secret-2"},
        ],
    )

    assert json.loads(payload) == {
        "devId": "dev123",
        "encryptInfos": [
            {"uuid": "clip-1", "encrypt": "secret-1"},
            {"uuid": "clip-2", "encrypt": "secret-2"},
        ],
    }


@pytest.mark.parametrize("secrets", [None, [{}], [{"uuid": "", "secretKey": "x"}], [{"uuid": "u"}]])
def test_native_encryption_info_json_rejects_invalid_secret_rows(secrets):
    with pytest.raises(NativeSessionError):
        native_encryption_info_json("dev123", secrets)


@pytest.mark.parametrize(
    "updates,error",
    [
        ({"password": ""}, "camera password"),
        ({"skill": ""}, "skill"),
        ({"skill": "not-json"}, "skill must be valid JSON"),
        ({"p2pConfig": {}}, "p2pConfig"),
        ({"p2pSpecifiedType": 3}, "Unsupported"),
        ({"p2pSpecifiedType": "bad"}, "Unsupported"),
    ],
)
def test_invalid_camera_info_is_rejected(updates, error):
    with pytest.raises(NativeSessionError, match=error):
        normalize_camera_info(
            "dev123",
            camera_info(**updates),
            local_key="local-key",
            trace_id="trace",
        )


def test_session_config_validation_rejects_bad_preconnect_and_json():
    with pytest.raises(NativeSessionError, match="preconnect"):
        NativeCameraSessionConfig(
            dev_id="dev",
            local_key="key",
            camera_password="pw",
            token="{}",
            skill="{}",
            p2p_type=4,
            trace_id="trace",
            preconnect=2,
        ).validate()
    with pytest.raises(NativeSessionError, match="skill"):
        NativeCameraSessionConfig(
            dev_id="dev",
            local_key="key",
            camera_password="pw",
            token="{}",
            skill="[]",
            p2p_type=4,
            trace_id="trace",
        ).validate()


@pytest.mark.parametrize(
    ("encrypted", "version", "method"),
    [
        (False, None, NativePlaybackMethod.RECORD_FRAGMENTS_BY_DAY),
        (True, None, NativePlaybackMethod.RECORD_FRAGMENTS_BY_DAY_V2),
        (False, 3, NativePlaybackMethod.RECORD_FRAGMENTS_BY_DAY_V3),
        (True, 3, NativePlaybackMethod.RECORD_FRAGMENTS_BY_DAY_V3),
    ],
)
def test_record_query_method(encrypted, version, method):
    assert record_query_method(encrypted=encrypted, version=version) == method


@pytest.mark.parametrize(
    ("query", "method", "day_text", "args"),
    [
        (
            NativeRecordingQuery(date(2026, 9, 16)),
            NativePlaybackMethod.RECORD_FRAGMENTS_BY_DAY,
            "20260916",
            ("20260916",),
        ),
        (
            NativeRecordingQuery(date(2026, 9, 16), encrypted=True),
            NativePlaybackMethod.RECORD_FRAGMENTS_BY_DAY_V2,
            "20260916",
            ("20260916",),
        ),
        (
            NativeRecordingQuery(date(2026, 9, 16), version=3),
            NativePlaybackMethod.RECORD_FRAGMENTS_BY_DAY_V3,
            "20260916",
            ("20260916",),
        ),
    ],
)
def test_recording_query_models_apk_catalog_call(query, method, day_text, args):
    assert query.method == method
    assert query.day_text == day_text
    assert query.args == args


@pytest.mark.parametrize(
    ("query", "error"),
    [
        (NativeRecordingQuery("20260916"), "date"),
        (NativeRecordingQuery(date(2026, 9, 16), encrypted="yes"), "boolean"),
        (NativeRecordingQuery(date(2026, 9, 16), version="3"), "integer"),
    ],
)
def test_invalid_recording_query_is_rejected(query, error):
    with pytest.raises(NativeSessionError, match=error):
        query.validate()


@pytest.mark.parametrize(
    ("playback_request", "method", "args"),
    [
        (
            NativePlaybackRequest(10, 20),
            NativePlaybackMethod.START_PLAYBACK,
            (10, 20, 10),
        ),
        (
            NativePlaybackRequest(10, 20, play_time=13, encrypted=True),
            NativePlaybackMethod.START_PLAYBACK_V2,
            (10, 20, 13),
        ),
        (
            NativePlaybackRequest(
                10,
                20,
                play_time=13,
                fragments_json='{"fragments":[{"start":10,"end":20}]}',
                play_mode_supported=True,
            ),
            NativePlaybackMethod.START_PLAYBACK_WITH_PLAY_TIME,
            (13, 0, '{"fragments":[{"start":10,"end":20}]}'),
        ),
        (
            NativePlaybackRequest(
                5,
                25,
                play_time=13,
                fragments_json='{"fragments":[{"start":10,"end":20},{"start":30,"end":40}]}',
                play_mode_supported=True,
                encrypted=True,
            ),
            NativePlaybackMethod.START_PLAYBACK_V2,
            (10, 20, 13),
        ),
    ],
)
def test_playback_method_selection(playback_request, method, args):
    assert playback_request.method == method
    assert playback_request.args == args


def test_apk_default_playback_output_is_decoded_yuv_pcm():
    request = NativePlaybackRequest(10, 20)
    assert request.output_format_args == (APK_VIDEO_OUTPUT_FORMAT_YUV, APK_AUDIO_OUTPUT_FORMAT_PCM)


def test_raw_packet_output_is_explicit_for_no_transcoding_helper_path():
    request = NativePlaybackRequest(10, 20, media_format=NativeMediaFormat.RAW_PACKETS)
    assert request.output_format_args == (APK_VIDEO_OUTPUT_FORMAT_RAWDATA, APK_AUDIO_OUTPUT_FORMAT_RAWDATA)


def test_playback_call_spec_uses_verified_native_start_playback_signature():
    request = NativePlaybackRequest(10, 20, play_time=12, media_format=NativeMediaFormat.RAW_PACKETS)
    spec = request.call_spec

    assert spec.method == NativePlaybackMethod.START_PLAYBACK
    assert spec.args == (10, 20, 12)
    assert spec.uses_raw_packets is True
    assert spec.video_output_format == APK_VIDEO_OUTPUT_FORMAT_RAWDATA
    assert spec.audio_output_format == APK_AUDIO_OUTPUT_FORMAT_RAWDATA
    assert spec.jni_args == (10, 20, 12, 0, 0, 0)
    assert any("ThingCameraSimple::StartPlayBack" in item for item in spec.cpp_signatures)
    assert any("ThingCameraV4::StartPlayBack" in item for item in spec.cpp_signatures)


def test_play_mode_call_spec_uses_fragments_signature_for_decoded_apk_default():
    fragments = '{"fragments":[{"start":10,"end":20}]}'
    spec = NativePlaybackRequest(
        10,
        20,
        play_time=12,
        fragments_json=fragments,
        play_mode_supported=True,
    ).call_spec

    assert spec.method == NativePlaybackMethod.START_PLAYBACK_WITH_PLAY_TIME
    assert spec.args == (12, 0, fragments)
    assert spec.jni_args == (12, 0, fragments)
    assert all("char const*" in item for item in spec.cpp_signatures)


def test_raw_play_mode_is_rejected_because_jni_has_no_output_formats():
    with pytest.raises(NativeSessionError, match="raw packet output"):
        NativePlaybackRequest(
            10,
            20,
            play_time=12,
            fragments_json='{"fragments":[{"start":10,"end":20}]}',
            play_mode_supported=True,
            media_format=NativeMediaFormat.RAW_PACKETS,
        ).call_spec


def test_native_playback_signatures_reject_non_playback_method():
    with pytest.raises(NativeSessionError, match="Unsupported"):
        native_playback_cpp_signatures(NativePlaybackMethod.DOWNLOAD_PLAYBACK_IMAGE_V2)


def test_native_playback_jni_args_match_apk_native_method_order():
    assert native_playback_jni_args(
        NativePlaybackMethod.START_PLAYBACK_V2,
        (10, 20, 12),
        video_output_format=0,
        audio_output_format=0,
        encrypted=True,
    ) == (10, 20, 12, 1, 0, 0)


@pytest.mark.parametrize(
    "playback_request,error",
    [
        (NativePlaybackRequest(20, 10), "end"),
        (NativePlaybackRequest(10, 20, play_time=20), "position"),
        (NativePlaybackRequest(10, 20, play_mode_supported=True), "fragments_json"),
        (
            NativePlaybackRequest(10, 20, fragments_json='{"fragments":[]}', play_mode_supported=True),
            "non-empty",
        ),
        (
            NativePlaybackRequest(10, 20, fragments_json='{"fragments":[{"start":20,"end":10}]}', play_mode_supported=True),
            "Fragment end",
        ),
        (
            NativePlaybackRequest(10, 20, media_format="raw"),
            "media_format",
        ),
    ],
)
def test_invalid_playback_requests(playback_request, error):
    with pytest.raises(NativeSessionError, match=error):
        _ = playback_request.method
    native_session_init_api_request,
