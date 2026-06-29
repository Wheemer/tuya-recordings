from __future__ import annotations

from pathlib import Path
import logging

import voluptuous as vol

from homeassistant.components import frontend
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, State
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_state_change_event, async_track_time_interval
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .client import TuyaRecordingsAuthError, TuyaRecordingsClient
from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    DOMAIN,
    MEDIA_SYNC_INTERVAL,
    MEDIA_SYNC_STARTUP_DELAY,
    RECORDING_TRIGGER_COOLDOWN,
    RECORDING_TRIGGER_SETTLE_DELAY,
    PLATFORMS,
    CONF_REGION,
    CONF_USER_ID,
    SIGNAL_RECORDINGS_UPDATED,
    THUMBNAIL_SYNC_INTERVAL,
    THUMBNAIL_SYNC_LIMIT,
    THUMBNAIL_SYNC_STARTUP_DELAY,
)
from .frontend import FRONTEND_URL_PATH, async_register_frontend
from .http import TuyaRecordingsPanelDataView, TuyaRecordingsPlaybackView, TuyaRecordingsThumbnailView

CONF_ENTRY_ID = "entry_id"
CONF_LIMIT = "limit"
SERVICE_REFRESH = "refresh_recordings"
SERVICE_CLEAR_CACHE = "clear_cache"
SERVICE_SYNC_MEDIA = "sync_media"
SERVICE_POPULATE_THUMBNAILS = "populate_thumbnails"
DATA_CAMERA_WORK_TASK = "camera_work_task"
DATA_MEDIA_SYNC_RUNNING = "media_sync_running"
DATA_MEDIA_SYNC_PENDING = "media_sync_pending"
DATA_THUMBNAIL_SYNC_RUNNING = "thumbnail_sync_running"
DATA_THUMBNAIL_SYNC_PENDING = "thumbnail_sync_pending"
DATA_THUMBNAIL_SYNC_LIMIT = "thumbnail_sync_limit"
DATA_THUMBNAIL_SYNC_REQUIRE_MEDIA = "thumbnail_sync_require_media"
DATA_RECORDING_TRIGGER_TIMER = "recording_trigger_timer"
DATA_RECORDING_TRIGGER_LAST = "recording_trigger_last"

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up Tuya Recordings."""
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate older Tuya Recordings config entries."""
    stale_keys = {"cookies", "login_result", "server_host", "go2rtc_url", "rtsp_port"}
    if entry.version < 2 or stale_keys.intersection(entry.data):
        data = dict(entry.data)
        for key in stale_keys:
            data.pop(key, None)
        hass.config_entries.async_update_entry(entry, data=data, version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    dependency_errors = _required_dependency_errors(hass)
    _async_update_dependency_issues(hass, dependency_errors)
    if dependency_errors:
        raise ConfigEntryError("Tuya Recordings requires official Tuya and LocalTuya with cloud credentials")
    _async_update_camera_repair_issues(hass)

    cache_path = Path(hass.config.path(".storage", DOMAIN, f"{entry.entry_id}_recordings.json"))
    entry_data = _entry_data_with_localtuya_credentials(hass, entry)
    _async_scrub_legacy_entry_data(hass, entry, entry_data)
    client = TuyaRecordingsClient(entry_data, cache_path=cache_path)
    await hass.async_add_executor_job(client.load_cache)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"client": client, "entry": entry}
    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    _async_register_services(hass)
    _async_register_views(hass)
    await async_register_frontend(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_schedule_media_sync(hass, entry)
    _async_setup_recording_triggers(hass, entry)
    return True


def _async_scrub_legacy_entry_data(hass: HomeAssistant, entry: ConfigEntry, entry_data: dict) -> None:
    stale_keys = {"cookies", "login_result", "server_host", "go2rtc_url", "rtsp_port"}
    clean_data = {key: value for key, value in entry_data.items() if key not in stale_keys}
    if clean_data != dict(entry.data):
        hass.config_entries.async_update_entry(entry, data=clean_data, version=2)


def _entry_data_with_localtuya_credentials(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    data = {**entry.data, **entry.options}
    if data.get(CONF_CLIENT_ID) and data.get(CONF_CLIENT_SECRET) and data.get(CONF_USER_ID):
        return data

    for localtuya_entry in _localtuya_credential_entries(hass):
        local_data = localtuya_entry.data
        merged = {
            **data,
            CONF_CLIENT_ID: local_data["client_id"],
            CONF_CLIENT_SECRET: local_data["client_secret"],
            CONF_USER_ID: local_data["user_id"],
            CONF_REGION: local_data.get("region", data.get(CONF_REGION, "us")),
        }
        _LOGGER.info("Tuya Recordings is using OpenAPI credentials from LocalTuya entry %s", localtuya_entry.title)
        return merged
    return data


def _required_dependency_errors(hass: HomeAssistant) -> set[str]:
    errors: set[str] = set()
    if not hass.config_entries.async_entries("tuya"):
        errors.add("tuya_required")
    if not hass.config_entries.async_entries("localtuya"):
        errors.add("localtuya_required")
    elif not _localtuya_credential_entries(hass):
        errors.add("localtuya_cloud_credentials_required")
    return errors


def _localtuya_credential_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    return [
        entry
        for entry in hass.config_entries.async_entries("localtuya")
        if entry.data.get("client_id") and entry.data.get("client_secret") and entry.data.get("user_id")
    ]


def _async_update_dependency_issues(hass: HomeAssistant, dependency_errors: set[str]) -> None:
    all_issues = {"tuya_required", "localtuya_required", "localtuya_cloud_credentials_required"}
    for issue_id in all_issues - dependency_errors:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    for issue_id in dependency_errors:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=issue_id,
        )


def _async_update_camera_repair_issues(hass: HomeAssistant) -> None:
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    tuya_cameras = _official_tuya_camera_device_ids(
        er.async_entries_for_config_entry(entity_registry, _tuya_config_entry_id(hass)),
        device_registry.devices.values(),
    )
    localtuya_ids = _localtuya_configured_device_ids(hass)
    missing = sorted(name for device_id, name in tuya_cameras.items() if device_id not in localtuya_ids)
    issue_id = "localtuya_camera_setup_incomplete"
    if not missing:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=issue_id,
        translation_placeholders={"cameras": ", ".join(missing)},
    )


def _tuya_config_entry_id(hass: HomeAssistant) -> str:
    entries = hass.config_entries.async_entries("tuya")
    return entries[0].entry_id if entries else ""


def _official_tuya_camera_device_ids(entity_entries, device_entries) -> dict[str, str]:
    camera_registry_ids = {
        entry.device_id
        for entry in entity_entries
        if getattr(entry, "platform", "") == "tuya"
        and str(getattr(entry, "entity_id", "")).startswith("camera.")
        and getattr(entry, "device_id", None)
    }
    cameras: dict[str, str] = {}
    for device in device_entries:
        if getattr(device, "id", None) not in camera_registry_ids:
            continue
        for domain, device_id in getattr(device, "identifiers", set()):
            if domain == "tuya" and device_id:
                cameras[str(device_id)] = getattr(device, "name_by_user", None) or getattr(device, "name", None) or str(device_id)
    return cameras


def _localtuya_configured_device_ids(hass: HomeAssistant) -> set[str]:
    device_ids: set[str] = set()
    for entry in hass.config_entries.async_entries("localtuya"):
        devices = entry.data.get("devices") or {}
        if isinstance(devices, dict):
            device_ids.update(str(device_id) for device_id in devices)
    return device_ids


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not isinstance(entry_data, dict) or not isinstance(entry_data.get("client"), TuyaRecordingsClient):
        return
    client: TuyaRecordingsClient = entry_data["client"]
    await hass.async_add_executor_job(client.update_options, {**entry.data, **entry.options})
    async_dispatcher_send(hass, SIGNAL_RECORDINGS_UPDATED, entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if not _configured_entries(hass):
        hass.services.async_remove(DOMAIN, SERVICE_REFRESH)
        hass.services.async_remove(DOMAIN, SERVICE_CLEAR_CACHE)
        hass.services.async_remove(DOMAIN, SERVICE_SYNC_MEDIA)
        hass.services.async_remove(DOMAIN, SERVICE_POPULATE_THUMBNAILS)
        frontend.async_remove_panel(hass, FRONTEND_URL_PATH, warn_if_unknown=False)
        hass.data.get(DOMAIN, {}).pop("_panel_registered", None)
    return True


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_REFRESH):
        return

    async def refresh_recordings(call) -> None:
        entries = _entries_for_call(hass, call.data.get(CONF_ENTRY_ID))
        for entry_data in entries:
            try:
                await hass.async_add_executor_job(entry_data["client"].camera_index, True)
            except TuyaRecordingsAuthError as exc:
                entry_data["entry"].async_start_reauth(hass)
                raise ConfigEntryAuthFailed("Tuya Recordings session expired") from exc
            async_dispatcher_send(hass, SIGNAL_RECORDINGS_UPDATED, entry_data["entry"].entry_id)

    async def clear_cache(call) -> None:
        entries = _entries_for_call(hass, call.data.get(CONF_ENTRY_ID))
        for entry_data in entries:
            await hass.async_add_executor_job(entry_data["client"].clear_cache)
            async_dispatcher_send(hass, SIGNAL_RECORDINGS_UPDATED, entry_data["entry"].entry_id)

    async def sync_media(call) -> None:
        entries = _entries_for_call(hass, call.data.get(CONF_ENTRY_ID))
        for entry_data in entries:
            await _async_request_camera_work(hass, entry_data["entry"].entry_id, "service", media=True, thumbnails=True, wait=True)

    async def populate_thumbnails(call) -> None:
        entries = _entries_for_call(hass, call.data.get(CONF_ENTRY_ID))
        limit = int(call.data.get(CONF_LIMIT) or THUMBNAIL_SYNC_LIMIT)
        for entry_data in entries:
            await _async_request_camera_work(
                hass,
                entry_data["entry"].entry_id,
                "service",
                thumbnails=True,
                thumbnail_limit=limit,
                require_media_sync=False,
                wait=True,
            )

    schema = vol.Schema({vol.Optional(CONF_ENTRY_ID): str})
    thumbnail_schema = vol.Schema(
        {
            vol.Optional(CONF_ENTRY_ID): str,
            vol.Optional(CONF_LIMIT, default=THUMBNAIL_SYNC_LIMIT): vol.All(vol.Coerce(int), vol.Range(min=1, max=50)),
        }
    )
    hass.services.async_register(DOMAIN, SERVICE_REFRESH, refresh_recordings, schema=schema)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_CACHE, clear_cache, schema=schema)
    hass.services.async_register(DOMAIN, SERVICE_SYNC_MEDIA, sync_media, schema=schema)
    hass.services.async_register(DOMAIN, SERVICE_POPULATE_THUMBNAILS, populate_thumbnails, schema=thumbnail_schema)


def _async_register_views(hass: HomeAssistant) -> None:
    if hass.data.setdefault(DOMAIN, {}).get("_playback_view_registered"):
        return
    hass.http.register_view(TuyaRecordingsPanelDataView(hass))
    hass.http.register_view(TuyaRecordingsPlaybackView(hass))
    hass.http.register_view(TuyaRecordingsThumbnailView(hass))
    hass.data[DOMAIN]["_playback_view_registered"] = True


def _async_schedule_media_sync(hass: HomeAssistant, entry: ConfigEntry) -> None:
    async def _run_thumbnail_interval(now) -> None:
        await _async_request_camera_work(
            hass,
            entry.entry_id,
            "interval",
            thumbnails=True,
            thumbnail_limit=THUMBNAIL_SYNC_LIMIT,
            require_media_sync=True,
        )

    async def _run_media_interval(now) -> None:
        await _async_request_camera_work(hass, entry.entry_id, "interval", media=True)

    async def _run_thumbnail_startup(now) -> None:
        await _async_request_camera_work(
            hass,
            entry.entry_id,
            "startup",
            thumbnails=True,
            thumbnail_limit=THUMBNAIL_SYNC_LIMIT,
            require_media_sync=True,
        )

    async def _run_media_startup(now) -> None:
        await _async_request_camera_work(hass, entry.entry_id, "startup", media=True)

    entry.async_on_unload(async_track_time_interval(hass, _run_thumbnail_interval, THUMBNAIL_SYNC_INTERVAL))
    entry.async_on_unload(async_track_time_interval(hass, _run_media_interval, MEDIA_SYNC_INTERVAL))
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if isinstance(entry_data, dict) and entry_data.get("client") and entry_data["client"].media_sync_enabled:
        entry.async_on_unload(async_call_later(hass, THUMBNAIL_SYNC_STARTUP_DELAY, _run_thumbnail_startup))
        entry.async_on_unload(async_call_later(hass, MEDIA_SYNC_STARTUP_DELAY, _run_media_startup))


def _async_setup_recording_triggers(hass: HomeAssistant, entry: ConfigEntry) -> None:
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    client = entry_data.get("client") if isinstance(entry_data, dict) else None
    camera_tokens = _client_camera_tokens(client) if isinstance(client, TuyaRecordingsClient) else set()
    entity_ids = _recording_trigger_entity_ids(hass, camera_tokens)
    if not entity_ids:
        _LOGGER.debug("Tuya Recordings found no HA camera trigger entities for %s", entry.entry_id)
        return

    _LOGGER.info("Tuya Recordings will watch %s HA camera trigger entities for %s", len(entity_ids), entry.entry_id)

    async def _handle_trigger(event: Event) -> None:
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        if not _state_change_suggests_recording(old_state, new_state):
            return

        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if not isinstance(entry_data, dict) or not isinstance(entry_data.get("client"), TuyaRecordingsClient):
            return
        client: TuyaRecordingsClient = entry_data["client"]
        if not client.media_sync_enabled:
            return

        now = hass.loop.time()
        last_trigger = float(entry_data.get(DATA_RECORDING_TRIGGER_LAST) or 0)
        if now - last_trigger < RECORDING_TRIGGER_COOLDOWN:
            return
        entry_data[DATA_RECORDING_TRIGGER_LAST] = now

        if entry_data.get(DATA_RECORDING_TRIGGER_TIMER):
            return

        async def _run_after_settle(now) -> None:
            entry_data.pop(DATA_RECORDING_TRIGGER_TIMER, None)
            await _async_request_camera_work(
                hass,
                entry.entry_id,
                "ha_recording_trigger",
                media=True,
                thumbnails=True,
                thumbnail_limit=THUMBNAIL_SYNC_LIMIT,
            )

        entry_data[DATA_RECORDING_TRIGGER_TIMER] = async_call_later(
            hass,
            RECORDING_TRIGGER_SETTLE_DELAY,
            _run_after_settle,
        )
        _LOGGER.debug(
            "Tuya Recordings queued sync for %s after HA camera trigger %s changed to %s",
            entry.entry_id,
            new_state.entity_id if isinstance(new_state, State) else event.data.get("entity_id"),
            new_state.state if isinstance(new_state, State) else None,
        )

    entry.async_on_unload(async_track_state_change_event(hass, entity_ids, _handle_trigger))


def _recording_trigger_entity_ids(hass: HomeAssistant, camera_tokens: set[str] | None = None) -> list[str]:
    return _recording_trigger_entity_ids_from_states(hass.states.async_all(), camera_tokens)


def _recording_trigger_entity_ids_from_states(states: list[State], camera_tokens: set[str] | None = None) -> list[str]:
    camera_tokens = camera_tokens or _recording_trigger_camera_tokens(states)
    return sorted(
        state.entity_id
        for state in states
        if _is_recording_trigger_entity(state, camera_tokens)
    )


def _client_camera_tokens(client: TuyaRecordingsClient) -> set[str]:
    try:
        index = client.cached_camera_index()
    except Exception:
        return set()
    return {
        token
        for camera in index.get("cameras", [])
        if isinstance(camera, dict)
        for token in {_base_camera_token(str(camera.get("name") or ""))}
        if token
    }


def _recording_trigger_camera_tokens(states: list[State]) -> set[str]:
    tokens: set[str] = set()
    for state in states:
        entity_id = state.entity_id.lower()
        name = str(state.attributes.get("friendly_name") or "").lower()
        domain, _, object_id = entity_id.partition(".")
        if domain == "camera":
            tokens.add(_base_camera_token(object_id))
        elif domain == "sensor" and "camera_display_status" in entity_id:
            tokens.add(_base_camera_token(object_id.removesuffix("_camera_display_status")))
        elif domain == "sensor" and "sd_storage" in entity_id:
            tokens.add(_base_camera_token(object_id.removesuffix("_sd_storage").removesuffix("_local")))
        elif domain == "sensor" and "camera display status" in name:
            tokens.add(_base_camera_token(name.replace("camera display status", "")))
        elif domain == "sensor" and "sd storage" in name:
            tokens.add(_base_camera_token(name.replace("sd storage", "").replace("local", "")))
    return {token for token in tokens if token}


def _base_camera_token(value: str) -> str:
    token = value.strip().lower().replace(" ", "_")
    while token and token[-1].isdigit():
        token = token[:-1].rstrip("_")
    return token


def _is_recording_trigger_entity(state: State, camera_tokens: set[str] | None = None) -> bool:
    entity_id = state.entity_id.lower()
    name = str(state.attributes.get("friendly_name") or "").lower()
    text = f"{entity_id} {name}"
    domain = entity_id.split(".", 1)[0]
    camera_tokens = camera_tokens or set()

    matches_camera = not camera_tokens or any(token in text for token in camera_tokens)
    if not matches_camera:
        return False
    if domain == "event" and any(marker in text for marker in ("doorbell", "motion", "alarm")):
        return True
    if domain == "sensor" and ("camera_display_status" in entity_id or "camera display status" in name):
        return True
    if domain == "sensor" and ("sd_storage" in entity_id or "sd storage" in name):
        return True
    return False


def _state_change_suggests_recording(old_state: State | None, new_state: State | None) -> bool:
    if old_state is None or new_state is None:
        return False
    if new_state.state in {"unknown", "unavailable"} or old_state.state == new_state.state:
        return False

    entity_id = new_state.entity_id.lower()
    name = str(new_state.attributes.get("friendly_name") or "").lower()
    domain = entity_id.split(".", 1)[0]
    if domain == "event":
        return True
    if "camera_display_status" in entity_id or "camera display status" in name:
        return new_state.state.lower() == "recording"
    if "sd_storage" in entity_id or "sd storage" in name:
        return True
    return False


async def _async_run_sync_cycle(hass: HomeAssistant, entry_id: str, reason: str) -> None:
    await _async_request_camera_work(hass, entry_id, reason, media=True, thumbnails=True, wait=True)


async def _async_request_camera_work(
    hass: HomeAssistant,
    entry_id: str,
    reason: str,
    *,
    media: bool = False,
    thumbnails: bool = False,
    thumbnail_limit: int = THUMBNAIL_SYNC_LIMIT,
    require_media_sync: bool = True,
    wait: bool = False,
) -> None:
    entry_data = hass.data.get(DOMAIN, {}).get(entry_id)
    if not isinstance(entry_data, dict) or not isinstance(entry_data.get("client"), TuyaRecordingsClient):
        return
    client: TuyaRecordingsClient = entry_data["client"]
    queued = False
    if media and client.media_sync_enabled:
        if not entry_data.get(DATA_MEDIA_SYNC_RUNNING):
            entry_data[DATA_MEDIA_SYNC_PENDING] = True
        queued = True
    if thumbnails and (not require_media_sync or client.media_sync_enabled):
        if not entry_data.get(DATA_THUMBNAIL_SYNC_RUNNING):
            entry_data[DATA_THUMBNAIL_SYNC_PENDING] = True
            entry_data[DATA_THUMBNAIL_SYNC_LIMIT] = max(int(entry_data.get(DATA_THUMBNAIL_SYNC_LIMIT) or 0), thumbnail_limit)
            entry_data[DATA_THUMBNAIL_SYNC_REQUIRE_MEDIA] = bool(
                entry_data.get(DATA_THUMBNAIL_SYNC_REQUIRE_MEDIA, True) and require_media_sync
            )
        queued = True
    if not queued:
        return

    task = entry_data.get(DATA_CAMERA_WORK_TASK)
    if task is None or task.done():
        task = hass.async_create_task(_async_run_camera_work(hass, entry_id, reason))
        entry_data[DATA_CAMERA_WORK_TASK] = task
    if wait:
        await task


async def _async_run_camera_work(hass: HomeAssistant, entry_id: str, reason: str) -> None:
    entry_data = hass.data.get(DOMAIN, {}).get(entry_id)
    if not isinstance(entry_data, dict) or not isinstance(entry_data.get("client"), TuyaRecordingsClient):
        return
    client: TuyaRecordingsClient = entry_data["client"]
    while True:
        if entry_data.pop(DATA_MEDIA_SYNC_PENDING, False):
            if client.media_sync_enabled:
                entry_data[DATA_MEDIA_SYNC_RUNNING] = True
                try:
                    result = await hass.async_add_executor_job(client.sync_recordings)
                    _LOGGER.info("Tuya Recordings media sync result for %s via %s: %s", entry_id, reason, result)
                except TuyaRecordingsAuthError as exc:
                    entry_data["entry"].async_start_reauth(hass)
                    raise ConfigEntryAuthFailed("Tuya Recordings session expired") from exc
                finally:
                    entry_data[DATA_MEDIA_SYNC_RUNNING] = False
                async_dispatcher_send(hass, SIGNAL_RECORDINGS_UPDATED, entry_id)
            continue

        if entry_data.pop(DATA_THUMBNAIL_SYNC_PENDING, False):
            limit = int(entry_data.pop(DATA_THUMBNAIL_SYNC_LIMIT, THUMBNAIL_SYNC_LIMIT) or THUMBNAIL_SYNC_LIMIT)
            require_media_sync = bool(entry_data.pop(DATA_THUMBNAIL_SYNC_REQUIRE_MEDIA, True))
            if require_media_sync and not client.media_sync_enabled:
                continue
            entry_data[DATA_THUMBNAIL_SYNC_RUNNING] = True
            try:
                result = await hass.async_add_executor_job(client.populate_thumbnails, limit)
                _LOGGER.info("Tuya Recordings thumbnail sync result for %s via %s: %s", entry_id, reason, result)
            finally:
                entry_data[DATA_THUMBNAIL_SYNC_RUNNING] = False
            async_dispatcher_send(hass, SIGNAL_RECORDINGS_UPDATED, entry_id)
            continue

        return


def _entries_for_call(hass: HomeAssistant, entry_id: str | None) -> list[dict]:
    entries = hass.data.get(DOMAIN, {})
    if entry_id:
        entry_data = entries.get(entry_id)
        if isinstance(entry_data, dict) and isinstance(entry_data.get("client"), TuyaRecordingsClient):
            return [entry_data]
        return []
    return [
        entry_data
        for entry_data in entries.values()
        if isinstance(entry_data, dict) and isinstance(entry_data.get("client"), TuyaRecordingsClient)
    ]


def _configured_entries(hass: HomeAssistant) -> list[dict]:
    return _entries_for_call(hass, None)
