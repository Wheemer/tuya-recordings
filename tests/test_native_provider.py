import json

import pytest

from custom_components.tuya_recordings.lib.native_provider import (
    NativeAppCameraConfigProvider,
    NativeCameraConfigAuthError,
    NativeCameraConfigError,
    _apk_trace_id,
    app_camera_api_names,
    extract_camera_info,
)
from custom_components.tuya_recordings.lib.native_gateway import NativeGatewayAuthError
from custom_components.tuya_recordings.lib.native_session import (
    APK_RTC_CONFIG_API,
    derive_native_password,
)


def camera_info(**updates):
    data = {
        "id": "p2p-id",
        "sessionTid": "server-session-tid",
        "password": "camera-password",
        "p2pSpecifiedType": 4,
        "p2pConfig": {"session": {"sessionId": "sid"}},
        "skill": json.dumps({"videos": [], "audios": []}),
    }
    data.update(updates)
    return data


def test_app_camera_api_names_match_apk_contract():
    assert app_camera_api_names() == (APK_RTC_CONFIG_API,)


@pytest.mark.parametrize(
    "payload",
    [
        camera_info(),
        {"cameraInfo": camera_info()},
        {"result": {"camera_info": camera_info()}},
        {"data": {"config": camera_info()}},
    ],
)
def test_extract_camera_info_from_common_wrappers(payload):
    assert extract_camera_info(payload)["password"] == "camera-password"


def test_extract_camera_info_rejects_missing_shape():
    with pytest.raises(NativeCameraConfigError, match="camera info"):
        extract_camera_info({"result": {"not": "camera"}})


def test_provider_uses_apk_connection_config_and_normalizes_session_config():
    calls = []

    def call_api(api, version, body, extra_params):
        calls.append((api, version, body, extra_params))
        return {"cameraInfo": camera_info()}

    provider = NativeAppCameraConfigProvider(
        call_api=call_api,
        local_key_lookup=lambda dev_id: "local-key",
        trace_factory=lambda dev_id: "ipc_p2p_android_dev123_1234",
    )
    config = provider.session_config("dev123")
    assert calls == [
        (
            APK_RTC_CONFIG_API,
            "1.0",
            {"devId": "dev123"},
            {"bizDM": "ipc", "ctId": "ipc_p2p_android_dev123_1234"},
        )
    ]
    assert config.dev_id == "dev123"
    assert config.trace_id == "ipc_p2p_android_dev123_1234"
    assert config.derived_password == derive_native_password("camera-password", "local-key")


def test_apk_trace_matches_toolkit_format(monkeypatch):
    monkeypatch.setattr(
        "custom_components.tuya_recordings.lib.native_provider.time.time",
        lambda: 1234.567,
    )

    assert _apk_trace_id("dev123") == "ipc_p2p_android_dev123_1234567"


def test_provider_threads_native_app_session_into_session_config():
    provider = NativeAppCameraConfigProvider(
        call_api=lambda *args: {"cameraInfo": camera_info()},
        local_key_lookup=lambda dev_id: "local-key",
        trace_factory=lambda dev_id: "trace-1",
        app_session={"sid": "sid-value", "ecode": "ecode-value", "uid": "uid-value"},
        app_region="eu",
        app_device_fingerprint="fingerprint",
    )

    config = provider.session_config("dev123")

    assert config.app_sid == "sid-value"
    assert config.app_ecode == "ecode-value"
    assert config.app_uid == "uid-value"
    assert config.app_region == "eu"
    assert config.app_device_fingerprint == "fingerprint"


def test_provider_preserves_apk_p2p_policy_metadata():
    provider = NativeAppCameraConfigProvider(
        call_api=lambda *args: {"cameraInfo": camera_info(p2pPolicy=1)},
        local_key_lookup=lambda dev_id: "local-key",
        trace_factory=lambda dev_id: "trace-1",
    )

    config = provider.session_config("dev123")

    assert config.dev_id == "dev123"
    assert config.p2p_policy == 1
    assert config.mqtt_protocol_version == "2.2"


def test_provider_does_not_retry_camera_config_request():
    calls = []

    def call_api(api, version, body, extra_params):
        calls.append(api)
        raise RuntimeError("temporary")

    with pytest.raises(NativeCameraConfigError, match="request failed"):
        NativeAppCameraConfigProvider(
            call_api=call_api,
            local_key_lookup=lambda dev_id: "local-key",
            trace_factory=lambda dev_id: "trace-1",
        ).session_config("dev123")
    assert calls == [APK_RTC_CONFIG_API]


def test_provider_does_not_retry_an_expired_app_session():
    calls = []

    def call_api(*args):
        calls.append(args)
        raise NativeGatewayAuthError("expired secret response")

    provider = NativeAppCameraConfigProvider(
        call_api=call_api,
        local_key_lookup=lambda dev_id: "local-key",
    )

    with pytest.raises(NativeCameraConfigAuthError, match="authorization expired"):
        provider.session_config("dev123")
    assert len(calls) == 1


def test_provider_reuses_bounded_camera_config_cache():
    calls = []
    now = [100.0]

    provider = NativeAppCameraConfigProvider(
        call_api=lambda *args: calls.append(args) or camera_info(),
        local_key_lookup=lambda dev_id: "local-key",
        trace_factory=lambda dev_id: f"trace-{len(calls) + 1}",
        cache_ttl=60,
        clock=lambda: now[0],
    )

    first = provider.session_config("dev123")
    second = provider.session_config("dev123")
    assert second is first
    assert len(calls) == 1

    now[0] = 161.0
    third = provider.session_config("dev123")
    assert third is not first
    assert len(calls) == 2


def test_provider_default_cache_expires_before_idle_worker_transport():
    calls = []
    now = [100.0]
    provider = NativeAppCameraConfigProvider(
        call_api=lambda *args: calls.append(args) or camera_info(),
        local_key_lookup=lambda dev_id: "local-key",
        trace_factory=lambda dev_id: f"trace-{len(calls) + 1}",
        clock=lambda: now[0],
    )

    first = provider.session_config("dev123")
    now[0] = 104.9
    assert provider.session_config("dev123") is first

    now[0] = 105.0
    second = provider.session_config("dev123")
    assert second is not first
    assert second.trace_id != first.trace_id
    assert len(calls) == 2


def test_provider_rejects_unbounded_camera_config_cache():
    provider = NativeAppCameraConfigProvider(
        call_api=lambda *args: camera_info(),
        local_key_lookup=lambda dev_id: "local-key",
        cache_ttl=301,
    )
    with pytest.raises(NativeCameraConfigError, match="cache TTL"):
        provider.session_config("dev123")


def test_provider_rejects_missing_local_key_before_api_call():
    calls = []
    provider = NativeAppCameraConfigProvider(
        call_api=lambda *args: calls.append(args),
        local_key_lookup=lambda dev_id: "",
    )
    with pytest.raises(NativeCameraConfigError, match="local key"):
        provider.session_config("dev123")
    assert calls == []


def test_provider_rejects_incompatible_camera_info_without_secret_leak():
    provider = NativeAppCameraConfigProvider(
        call_api=lambda *args: camera_info(password=""),
        local_key_lookup=lambda dev_id: "local-key",
    )
    with pytest.raises(NativeCameraConfigError) as error:
        provider.session_config("dev123")
    assert "camera-password" not in str(error.value)
    assert "local-key" not in str(error.value)
