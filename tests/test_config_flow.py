from types import SimpleNamespace

import pytest
try:
    from probatio import to_field_list as convert
except ImportError:
    from voluptuous_serialize import convert

from homeassistant.helpers import config_validation as cv

from custom_components.tuya_recordings.config_flow import (
    _dependency_error_key,
    _native_authorize_schema,
    _options_schema,
    _required_dependency_errors,
    _user_schema,
    _validate_form_input,
)
from custom_components.tuya_recordings.const import CONF_MEDIA_STORAGE_PATH


def test_user_schema_is_frontend_serializable():
    converted = convert(_user_schema({}), custom_serializer=cv.custom_serializer)

    assert any(field["name"] == CONF_MEDIA_STORAGE_PATH for field in converted)


def test_native_authorization_qr_is_frontend_serializable():
    converted = convert(
        _native_authorize_schema("tuyaSmart--qrLogin?token=test-token"),
        custom_serializer=cv.custom_serializer,
    )

    qr = next(field for field in converted if field["name"] == "qr_code")
    assert qr["selector"]["qr_code"]["data"] == "tuyaSmart--qrLogin?token=test-token"


def test_options_schema_is_frontend_serializable():
    config_entry = SimpleNamespace(data={}, options={})

    converted = convert(_options_schema(config_entry), custom_serializer=cv.custom_serializer)

    assert any(field["name"] == CONF_MEDIA_STORAGE_PATH for field in converted)


@pytest.mark.parametrize("key,value", [
    ("media_sync_enabled", True),
    ("media_sync_hours", 48),
    ("media_view_recordings_order", "Ascending"),
])
def test_options_preserve_setup_choices(key, value):
    entry = SimpleNamespace(data={key: value}, options={})
    assert _options_schema(entry)({})[key] == value


def test_saved_options_override_setup_choices():
    entry = SimpleNamespace(data={"media_sync_enabled": True}, options={"media_sync_enabled": False})
    assert _options_schema(entry)({})["media_sync_enabled"] is False


def test_thumbnail_sync_is_not_a_user_option():
    entry = SimpleNamespace(data={"thumbnail_sync_enabled": False}, options={})
    for schema in (_user_schema({}), _options_schema(entry)):
        fields = convert(schema, custom_serializer=cv.custom_serializer)
        assert all(field["name"] != "thumbnail_sync_enabled" for field in fields)


def test_media_storage_path_validation_returns_field_error():
    errors = _validate_form_input({CONF_MEDIA_STORAGE_PATH: "/config/www/tuya_recordings"})

    assert errors == {CONF_MEDIA_STORAGE_PATH: "path_not_allowed"}


@pytest.mark.parametrize("path", ["/media/tuya_recordings", " /media/tuya_recordings "])
def test_media_storage_path_validation_normalizes_valid_path(path):
    user_input = {CONF_MEDIA_STORAGE_PATH: path}

    assert _validate_form_input(user_input) == {}
    assert user_input[CONF_MEDIA_STORAGE_PATH] == "/media/tuya_recordings"


def test_setup_requires_official_tuya_but_not_localtuya():
    class Entries:
        def __init__(self, entries):
            self.entries = entries

        def async_entries(self, domain):
            return self.entries.get(domain, [])

    hass = SimpleNamespace(config_entries=Entries({"localtuya": [object()]}))
    errors = _required_dependency_errors(hass)
    assert errors == {"tuya_required"}
    assert _dependency_error_key(errors) == "tuya_required"

    hass = SimpleNamespace(config_entries=Entries({"tuya": [object()]}))
    assert _required_dependency_errors(hass) == set()
