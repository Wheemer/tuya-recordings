import pytest

from custom_components.tuya_recordings.const import (
    CONF_DEVICE_LOCAL_KEYS,
    CONF_DEVICE_PROTOCOL_VERSIONS,
    CONF_NATIVE_APP_SESSION,
)
from custom_components.tuya_recordings.lib.native_backend import (
    ApkNativeRecordingBackend,
    NativeBackendNotConfigured,
)
from custom_components.tuya_recordings.lib.native_setup import (
    CONF_NATIVE_APP_CALL,
    NativeBackendSetupError,
    build_recordings_backend,
    lookup_local_key,
    native_app_call,
    resolved_local_keys,
    resolved_protocol_versions,
)

APP_SESSION = {
    "sid": "sid",
    "ecode": "ecode",
    "uid": "uid",
    "partner_identity": "partner",
    "mobile_mqtts_url": "mqtt.example.test",
    "device_fingerprint": "fingerprint",
}


def test_local_keys_only_use_current_smart_life_qr_data():
    data = {
        CONF_DEVICE_LOCAL_KEYS: {"camera": "current-key"},
        "_localtuya_entries": {
            "old-entry": {"device_id": "camera", "local_key": "stale-key"},
        },
    }
    assert resolved_local_keys(data) == {"camera": "current-key"}
    assert resolved_local_keys({"_localtuya_entries": data["_localtuya_entries"]}) == {}
    assert lookup_local_key(resolved_local_keys(data), "camera") == "current-key"
    with pytest.raises(NativeBackendSetupError, match="Missing local key"):
        lookup_local_key({}, "missing")


def test_protocol_versions_only_use_smart_life_device_data():
    data = {
        CONF_DEVICE_PROTOCOL_VERSIONS: {"camera-id": "2.3"},
        "devices": {
            "camera": {
                "device_id": "camera-id",
                "protocol_version": "3.5",
            }
        }
    }

    assert resolved_protocol_versions(data) == {"camera-id": "2.3"}


def test_unrelated_lan_protocol_is_not_used_as_mqtt_protocol():
    data = {
        "devices": {
            "camera": {"device_id": "camera-id", "protocol_version": "3.5"}
        }
    }

    assert resolved_protocol_versions(data) == {}


def test_native_app_call_uses_saved_app_session():
    with pytest.raises(NativeBackendSetupError, match="authorization"):
        native_app_call({})
    with pytest.raises(NativeBackendSetupError, match="authorization"):
        native_app_call({CONF_NATIVE_APP_SESSION: {"sid": "sid", "ecode": "ecode"}})
    call = native_app_call({CONF_NATIVE_APP_SESSION: APP_SESSION})
    assert callable(call)
    def injected(api, version, body):
        return {}
    assert native_app_call({CONF_NATIVE_APP_CALL: injected}) is injected


def test_build_recordings_backend_fails_closed_without_all_apk_inputs():
    backend = build_recordings_backend({})
    assert isinstance(backend, NativeBackendNotConfigured)
    assert "authorization" in backend.reason


def test_build_recordings_backend_uses_in_process_sts_runtime():
    def app_call(api, version, body, extra_params):
        return {}

    backend = build_recordings_backend(
        {
            CONF_NATIVE_APP_CALL: app_call,
            CONF_NATIVE_APP_SESSION: APP_SESSION,
            CONF_DEVICE_LOCAL_KEYS: {"camera": "local-key"},
        }
    )

    assert isinstance(backend, ApkNativeRecordingBackend)
    runtime_pool = backend._provider.open_session.__self__
    assert runtime_pool._app_session == APP_SESSION
    assert runtime_pool._media_secrets is not None
    assert runtime_pool.__class__.__name__ == "NativeRelayRuntimePool"


def test_build_recordings_backend_fails_closed_without_complete_app_session():
    backend = build_recordings_backend(
        {
            CONF_NATIVE_APP_CALL: lambda api, version, body, extra_params: {},
            CONF_DEVICE_LOCAL_KEYS: {"camera": "local-key"},
        }
    )
    assert isinstance(backend, NativeBackendNotConfigured)
    assert "reauthenticate" in backend.reason
