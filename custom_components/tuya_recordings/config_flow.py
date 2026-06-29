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
    CONF_LOOKBACK_DAYS,
    CONF_MEDIA_SYNC_ENABLED,
    CONF_MEDIA_SYNC_HOURS,
    CONF_MEDIA_STORAGE_PATH,
    CONF_MEDIA_VIEW_RECORDINGS_ORDER,
    CONF_REGION,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MEDIA_SYNC_ENABLED,
    DEFAULT_MEDIA_SYNC_HOURS,
    DEFAULT_MEDIA_STORAGE_PATH,
    DEFAULT_REGION,
    DOMAIN,
    MEDIA_VIEW_RECORDINGS_ORDER_OPTIONS,
    REGION_LABELS,
)


class TuyaRecordingsConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors = _required_dependency_errors(self.hass)
        if user_input is not None:
            if errors:
                return self.async_show_form(
                    step_id="user",
                    data_schema=_user_schema(user_input),
                    errors={"base": _dependency_error_key(errors)},
                )

            entry_data = {
                CONF_REGION: user_input[CONF_REGION],
                CONF_MEDIA_STORAGE_PATH: user_input[CONF_MEDIA_STORAGE_PATH],
                CONF_MEDIA_SYNC_ENABLED: user_input[CONF_MEDIA_SYNC_ENABLED],
                CONF_MEDIA_SYNC_HOURS: user_input[CONF_MEDIA_SYNC_HOURS],
                CONF_MEDIA_VIEW_RECORDINGS_ORDER: user_input[CONF_MEDIA_VIEW_RECORDINGS_ORDER],
            }
            unique = _localtuya_unique_id(self.hass)
            await self.async_set_unique_id(f"{DOMAIN}_{unique}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Tuya Recordings", data=entry_data)
        else:
            user_input = {}

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input),
            errors={"base": _dependency_error_key(errors)} if errors else {},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> TuyaRecordingsOptionsFlow:
        return TuyaRecordingsOptionsFlow()


class TuyaRecordingsOptionsFlow(OptionsFlow):
    def __init__(self) -> None:
        self._pending_options: dict[str, Any] | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            old_path = _current_media_storage_path(self.config_entry)
            new_path = user_input[CONF_MEDIA_STORAGE_PATH]
            if new_path != old_path:
                self._pending_options = user_input
                return await self.async_step_storage_path_changed()
            return self.async_create_entry(title="", data=user_input)

        options = dict(self.config_entry.options)
        data = dict(self.config_entry.data)
        lookback_days = options.get(CONF_LOOKBACK_DAYS, data.get(CONF_LOOKBACK_DAYS, DEFAULT_LOOKBACK_DAYS))
        media_sync_enabled = options.get(CONF_MEDIA_SYNC_ENABLED, DEFAULT_MEDIA_SYNC_ENABLED)
        media_sync_hours = options.get(CONF_MEDIA_SYNC_HOURS, DEFAULT_MEDIA_SYNC_HOURS)
        media_storage_path = options.get(CONF_MEDIA_STORAGE_PATH, data.get(CONF_MEDIA_STORAGE_PATH, DEFAULT_MEDIA_STORAGE_PATH))
        recordings_order = options.get(CONF_MEDIA_VIEW_RECORDINGS_ORDER, "Descending")
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LOOKBACK_DAYS, default=lookback_days): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=31,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(CONF_MEDIA_VIEW_RECORDINGS_ORDER, default=recordings_order): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=MEDIA_VIEW_RECORDINGS_ORDER_OPTIONS)
                    ),
                    vol.Required(CONF_MEDIA_SYNC_ENABLED, default=media_sync_enabled): selector.BooleanSelector(),
                    vol.Required(CONF_MEDIA_SYNC_HOURS, default=media_sync_hours): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=744,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(CONF_MEDIA_STORAGE_PATH, default=media_storage_path): vol.All(
                        selector.TextSelector(),
                        _validate_media_storage_path,
                    ),
                }
            ),
        )

    async def async_step_storage_path_changed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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
    if path.rstrip("/") in {"", "/media", "/config", "/config/www"} or path.startswith("/config/www/"):
        raise vol.Invalid("path_not_allowed")
    return path


def _current_media_storage_path(config_entry) -> str:
    return config_entry.options.get(
        CONF_MEDIA_STORAGE_PATH,
        config_entry.data.get(CONF_MEDIA_STORAGE_PATH, DEFAULT_MEDIA_STORAGE_PATH),
    )


def _user_schema(user_input: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_REGION, default=user_input.get(CONF_REGION, DEFAULT_REGION)): selector.SelectSelector(
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
                default=user_input.get(CONF_MEDIA_STORAGE_PATH, DEFAULT_MEDIA_STORAGE_PATH),
            ): vol.All(
                selector.TextSelector(),
                _validate_media_storage_path,
            ),
            vol.Required(
                CONF_MEDIA_VIEW_RECORDINGS_ORDER,
                default=user_input.get(CONF_MEDIA_VIEW_RECORDINGS_ORDER, "Descending"),
            ): selector.SelectSelector(selector.SelectSelectorConfig(options=MEDIA_VIEW_RECORDINGS_ORDER_OPTIONS)),
            vol.Required(
                CONF_MEDIA_SYNC_ENABLED,
                default=user_input.get(CONF_MEDIA_SYNC_ENABLED, DEFAULT_MEDIA_SYNC_ENABLED),
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
        }
    )


def _required_dependency_errors(hass) -> set[str]:
    errors: set[str] = set()
    if not hass.config_entries.async_entries("tuya"):
        errors.add("tuya_required")
    localtuya_entries = hass.config_entries.async_entries("localtuya")
    if not localtuya_entries:
        errors.add("localtuya_required")
    elif not any(
        entry.data.get("client_id") and entry.data.get("client_secret") and entry.data.get("user_id")
        for entry in localtuya_entries
    ):
        errors.add("localtuya_cloud_credentials_required")
    return errors


def _dependency_error_key(errors: set[str]) -> str:
    for key in ("tuya_required", "localtuya_required", "localtuya_cloud_credentials_required"):
        if key in errors:
            return key
    return "unknown"


def _localtuya_unique_id(hass) -> str:
    for entry in hass.config_entries.async_entries("localtuya"):
        if entry.data.get("user_id"):
            return str(entry.data["user_id"])
    return "localtuya"
