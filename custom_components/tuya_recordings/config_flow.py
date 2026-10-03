from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ALERT_RESET_SECONDS,
    CONF_DEVICE_LOCAL_KEYS,
    CONF_DEVICE_PROTOCOL_VERSIONS,
    CONF_LOOKBACK_DAYS,
    CONF_MEDIA_STORAGE_PATH,
    CONF_MEDIA_SYNC_ENABLED,
    CONF_MEDIA_SYNC_HOURS,
    CONF_MEDIA_VIEW_RECORDINGS_ORDER,
    CONF_NATIVE_APP_SESSION,
    CONF_REGION,
    CONF_THUMBNAIL_SYNC_ENABLED,
    DEFAULT_ALERT_RESET_SECONDS,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MEDIA_STORAGE_PATH,
    DEFAULT_MEDIA_SYNC_ENABLED,
    DEFAULT_MEDIA_SYNC_HOURS,
    DEFAULT_REGION,
    DOMAIN,
    MEDIA_VIEW_RECORDINGS_ORDER_OPTIONS,
    REGION_LABELS,
)
from .lib.native_auth import NativeAuthorizationError, NativeQrAuthorization
from .lib.native_gateway import (
    NativeAppGatewayClient,
    NativeGatewayError,
    generate_device_fingerprint,
)


class TuyaRecordingsConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 5

    def __init__(self) -> None:
        self._pending_entry_data: dict[str, Any] | None = None
        self._native_authorization: NativeQrAuthorization | None = None
        self._native_gateway: NativeAppGatewayClient | None = None
        self._native_qr_payload = ""
        self._reauth_entry = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        dependency_errors = _required_dependency_errors(self.hass)
        if user_input is not None:
            errors = _validate_form_input(user_input)
            if dependency_errors:
                errors["base"] = _dependency_error_key(dependency_errors)
            if errors:
                return self.async_show_form(
                    step_id="user",
                    data_schema=_user_schema(user_input),
                    errors=errors,
                )

            self._pending_entry_data = {
                CONF_REGION: user_input[CONF_REGION],
                CONF_MEDIA_STORAGE_PATH: user_input[CONF_MEDIA_STORAGE_PATH],
                CONF_MEDIA_SYNC_ENABLED: user_input[CONF_MEDIA_SYNC_ENABLED],
                CONF_MEDIA_SYNC_HOURS: user_input[CONF_MEDIA_SYNC_HOURS],
                CONF_MEDIA_VIEW_RECORDINGS_ORDER: user_input[
                    CONF_MEDIA_VIEW_RECORDINGS_ORDER
                ],
                CONF_ALERT_RESET_SECONDS: user_input[CONF_ALERT_RESET_SECONDS],
                CONF_THUMBNAIL_SYNC_ENABLED: True,
            }
            if not await self._async_begin_native_authorization(
                user_input[CONF_REGION]
            ):
                return self.async_show_form(
                    step_id="user",
                    data_schema=_user_schema(user_input),
                    errors={"base": "native_authorization_start_failed"},
                )
            return await self.async_step_native_authorize()
        else:
            user_input = {}

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input),
            errors={"base": _dependency_error_key(dependency_errors)}
            if dependency_errors
            else {},
        )

    async def async_step_native_authorize(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Complete one explicit Smart Life app-session QR authorization."""
        if self._native_authorization is None or self._pending_entry_data is None:
            return self.async_abort(reason="native_authorization_missing")
        if user_input is None:
            return self.async_show_form(
                step_id="native_authorize",
                data_schema=_native_authorize_schema(self._native_qr_payload),
            )
        try:
            session = await self._native_authorization.finish()
        except NativeAuthorizationError:
            self._native_authorization = None
            return self.async_abort(reason="native_authorization_failed")
        saved_session = {
            "sid": session.sid,
            "ecode": session.ecode,
            "uid": session.uid,
            "partner_identity": session.partner_identity,
            "mobile_mqtts_url": session.mobile_mqtts_url,
            "device_fingerprint": session.device_fingerprint,
        }
        try:
            if self._native_gateway is None:
                raise NativeGatewayError("Native gateway is unavailable")
            (
                device_local_keys,
                device_protocol_versions,
            ) = await self.hass.async_add_executor_job(
                self._native_gateway.device_connection_data,
                saved_session,
            )
        except NativeGatewayError:
            self._native_gateway = None
            return self.async_abort(reason="native_authorization_failed")
        if not device_local_keys:
            self._native_gateway = None
            return self.async_abort(reason="native_authorization_failed")
        entry_data = {
            **self._pending_entry_data,
            CONF_NATIVE_APP_SESSION: saved_session,
            CONF_DEVICE_LOCAL_KEYS: device_local_keys,
            CONF_DEVICE_PROTOCOL_VERSIONS: device_protocol_versions,
        }
        self._native_gateway = None
        if self._reauth_entry is not None:
            return self.async_update_reload_and_abort(
                self._reauth_entry,
                data_updates={
                    CONF_NATIVE_APP_SESSION: entry_data[CONF_NATIVE_APP_SESSION],
                    CONF_DEVICE_LOCAL_KEYS: entry_data[CONF_DEVICE_LOCAL_KEYS],
                    CONF_DEVICE_PROTOCOL_VERSIONS: entry_data[
                        CONF_DEVICE_PROTOCOL_VERSIONS
                    ],
                },
            )
        await self.async_set_unique_id(f"{DOMAIN}_{saved_session['uid']}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Tuya Recordings", data=entry_data)

    async def async_step_reauth(
        self,
        entry_data: dict[str, Any],
    ) -> ConfigFlowResult:
        """Renew only the explicit Smart Life app session."""
        self._reauth_entry = self._get_reauth_entry()
        self._pending_entry_data = dict(self._reauth_entry.data)
        region = str(self._pending_entry_data.get(CONF_REGION) or DEFAULT_REGION)
        saved_session = self._pending_entry_data.get(CONF_NATIVE_APP_SESSION) or {}
        device_fingerprint = (
            saved_session.get("device_fingerprint")
            if isinstance(saved_session, dict)
            else None
        )
        if not await self._async_begin_native_authorization(
            region, device_fingerprint=device_fingerprint
        ):
            return self.async_abort(reason="native_authorization_failed")
        return await self.async_step_native_authorize()

    async def _async_begin_native_authorization(
        self,
        region: str,
        *,
        device_fingerprint: str | None = None,
    ) -> bool:
        device_fingerprint = device_fingerprint or generate_device_fingerprint()
        gateway = NativeAppGatewayClient(
            region=region,
            device_fingerprint=device_fingerprint,
        )
        self._native_gateway = gateway

        async def call(api: str, version: str, body: dict[str, Any]):
            return await self.hass.async_add_executor_job(
                gateway.call,
                api,
                version,
                body,
            )

        self._native_authorization = NativeQrAuthorization(
            call,
            device_fingerprint=device_fingerprint,
        )
        try:
            self._native_qr_payload = await self._native_authorization.begin()
        except NativeAuthorizationError:
            self._native_authorization = None
            self._native_gateway = None
            return False
        return True

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> TuyaRecordingsOptionsFlow:
        return TuyaRecordingsOptionsFlow()


class TuyaRecordingsOptionsFlow(OptionsFlow):
    def __init__(self) -> None:
        self._pending_options: dict[str, Any] | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            errors = _validate_form_input(user_input)
            if errors:
                return self.async_show_form(
                    step_id="init",
                    data_schema=_options_schema(self.config_entry, user_input),
                    errors=errors,
                )
            old_path = _current_media_storage_path(self.config_entry)
            new_path = user_input[CONF_MEDIA_STORAGE_PATH]
            if new_path != old_path:
                self._pending_options = user_input
                return await self.async_step_storage_path_changed()
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(self.config_entry),
        )

    async def async_step_storage_path_changed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._pending_options is None:
            return await self.async_step_init()
        if user_input is not None:
            return self.async_create_entry(title="", data=self._pending_options)

        return self.async_show_form(
            step_id="storage_path_changed",
            data_schema=vol.Schema({}),
            description_placeholders={
                "old_path": _current_media_storage_path(self.config_entry),
                "new_path": self._pending_options[CONF_MEDIA_STORAGE_PATH],
            },
        )


def _validate_media_storage_path(value: str) -> str:
    path = str(value or "").strip()
    if not path.startswith("/"):
        raise vol.Invalid("path_not_absolute")
    if path.rstrip("/") in {"", "/media", "/config", "/config/www"} or path.startswith(
        "/config/www/"
    ):
        raise vol.Invalid("path_not_allowed")
    return path


def _validate_form_input(user_input: dict[str, Any]) -> dict[str, str]:
    try:
        user_input[CONF_MEDIA_STORAGE_PATH] = _validate_media_storage_path(
            user_input.get(CONF_MEDIA_STORAGE_PATH, "")
        )
    except vol.Invalid as exc:
        return {CONF_MEDIA_STORAGE_PATH: str(exc)}
    return {}


def _current_media_storage_path(config_entry) -> str:
    return config_entry.options.get(
        CONF_MEDIA_STORAGE_PATH,
        config_entry.data.get(CONF_MEDIA_STORAGE_PATH, DEFAULT_MEDIA_STORAGE_PATH),
    )


def _options_schema(
    config_entry, user_input: dict[str, Any] | None = None
) -> vol.Schema:
    options = dict(config_entry.options)
    data = dict(config_entry.data)
    user_input = user_input or {}
    lookback_days = user_input.get(
        CONF_LOOKBACK_DAYS,
        options.get(
            CONF_LOOKBACK_DAYS, data.get(CONF_LOOKBACK_DAYS, DEFAULT_LOOKBACK_DAYS)
        ),
    )
    media_sync_enabled = user_input.get(
        CONF_MEDIA_SYNC_ENABLED,
        options.get(
            CONF_MEDIA_SYNC_ENABLED,
            data.get(CONF_MEDIA_SYNC_ENABLED, DEFAULT_MEDIA_SYNC_ENABLED),
        ),
    )
    media_sync_hours = user_input.get(
        CONF_MEDIA_SYNC_HOURS,
        options.get(
            CONF_MEDIA_SYNC_HOURS,
            data.get(CONF_MEDIA_SYNC_HOURS, DEFAULT_MEDIA_SYNC_HOURS),
        ),
    )
    media_storage_path = user_input.get(
        CONF_MEDIA_STORAGE_PATH,
        options.get(
            CONF_MEDIA_STORAGE_PATH,
            data.get(CONF_MEDIA_STORAGE_PATH, DEFAULT_MEDIA_STORAGE_PATH),
        ),
    )
    recordings_order = user_input.get(
        CONF_MEDIA_VIEW_RECORDINGS_ORDER,
        options.get(
            CONF_MEDIA_VIEW_RECORDINGS_ORDER,
            data.get(CONF_MEDIA_VIEW_RECORDINGS_ORDER, "Descending"),
        ),
    )
    alert_reset_seconds = user_input.get(
        CONF_ALERT_RESET_SECONDS,
        options.get(
            CONF_ALERT_RESET_SECONDS,
            data.get(CONF_ALERT_RESET_SECONDS, DEFAULT_ALERT_RESET_SECONDS),
        ),
    )
    return vol.Schema(
        {
            vol.Required(
                CONF_LOOKBACK_DAYS, default=lookback_days
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=31,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_MEDIA_VIEW_RECORDINGS_ORDER, default=recordings_order
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=MEDIA_VIEW_RECORDINGS_ORDER_OPTIONS
                )
            ),
            vol.Required(
                CONF_MEDIA_SYNC_ENABLED, default=media_sync_enabled
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_MEDIA_SYNC_HOURS, default=media_sync_hours
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=744,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_MEDIA_STORAGE_PATH, default=media_storage_path
            ): selector.TextSelector(),
            vol.Required(
                CONF_ALERT_RESET_SECONDS, default=alert_reset_seconds
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=5,
                    max=3600,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }
    )


def _user_schema(user_input: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_REGION, default=user_input.get(CONF_REGION, DEFAULT_REGION)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=region, label=label)
                        for region, label in REGION_LABELS.items()
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_MEDIA_STORAGE_PATH,
                default=user_input.get(
                    CONF_MEDIA_STORAGE_PATH, DEFAULT_MEDIA_STORAGE_PATH
                ),
            ): selector.TextSelector(),
            vol.Required(
                CONF_MEDIA_VIEW_RECORDINGS_ORDER,
                default=user_input.get(CONF_MEDIA_VIEW_RECORDINGS_ORDER, "Descending"),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=MEDIA_VIEW_RECORDINGS_ORDER_OPTIONS
                )
            ),
            vol.Required(
                CONF_MEDIA_SYNC_ENABLED,
                default=user_input.get(
                    CONF_MEDIA_SYNC_ENABLED, DEFAULT_MEDIA_SYNC_ENABLED
                ),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_MEDIA_SYNC_HOURS,
                default=user_input.get(CONF_MEDIA_SYNC_HOURS, DEFAULT_MEDIA_SYNC_HOURS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=744,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_ALERT_RESET_SECONDS,
                default=user_input.get(
                    CONF_ALERT_RESET_SECONDS, DEFAULT_ALERT_RESET_SECONDS
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=5,
                    max=3600,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }
    )


def _native_authorize_schema(payload: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional("qr_code"): selector.QrCodeSelector(
                selector.QrCodeSelectorConfig(
                    data=payload,
                    scale=5,
                    error_correction_level=selector.QrErrorCorrectionLevel.QUARTILE,
                )
            )
        }
    )


def _required_dependency_errors(hass) -> set[str]:
    errors: set[str] = set()
    if not hass.config_entries.async_entries("tuya"):
        errors.add("tuya_required")
    return errors


def _dependency_error_key(errors: set[str]) -> str:
    return "tuya_required" if "tuya_required" in errors else "unknown"
