from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import voluptuous as vol
from homeassistant.components import frontend as ha_frontend
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_time_interval,
)

from .client import TuyaRecordingsAuthError, TuyaRecordingsClient
from .const import (
    CATALOG_SYNC_DAYS_PER_PASS,
    CATALOG_SYNC_INTERVAL,
    CATALOG_SYNC_STARTUP_DELAY,
    CONF_APP_PROFILE,
    CONF_CLOUD_ACTIVITY_PAUSED,
    CONF_DEVICE_LOCAL_KEYS,
    CONF_DEVICE_PROTOCOL_VERSIONS,
    CONF_MEDIA_SYNC_ENABLED,
    CONF_NATIVE_APP_SESSION,
    CONF_THUMBNAIL_SYNC_ENABLED,
    DATA_INTERACTIVE_PLAYBACK_ACTIVE,
    DOMAIN,
    DEFAULT_MEDIA_SYNC_ENABLED,
    MEDIA_SYNC_INTERVAL,
    MEDIA_SYNC_STARTUP_DELAY,
    PLATFORMS,
    SIGNAL_RECORDINGS_UPDATED,
    THUMBNAIL_BACKGROUND_COOLDOWN,
    THUMBNAIL_BACKGROUND_LIMIT,
    THUMBNAIL_SYNC_LIMIT,
)
from .frontend import FRONTEND_URL_PATH, async_register_frontend
from .http import (
    TuyaRecordingsDebugView,
    TuyaRecordingsPanelDataView,
    TuyaRecordingsTimelineView,
    TuyaRecordingsThumbnailView,
)
from .lib.commands import CameraWorkBusy, CameraWorkCancelled
from .lib.native_setup import NativeBackendSetupError, native_app_session

CONF_ENTRY_ID = "entry_id"
CONF_LIMIT = "limit"
SERVICE_REFRESH = "refresh_recordings"
SERVICE_CLEAR_CACHE = "clear_cache"
SERVICE_CLEAR_VIDEO_CACHE = "clear_video_cache"
SERVICE_SYNC_MEDIA = "sync_media"
SERVICE_POPULATE_THUMBNAILS = "populate_thumbnails"
DATA_CAMERA_WORK_TASK = "camera_work_task"
DATA_CATALOG_REFRESH_TASK = "catalog_refresh_task"
DATA_MEDIA_SYNC_RUNNING = "media_sync_running"
DATA_MEDIA_SYNC_PENDING = "media_sync_pending"
DATA_MEDIA_SYNC_SCHEDULE = "media_sync_schedule"
DATA_THUMBNAIL_SYNC_RUNNING = "thumbnail_sync_running"
DATA_THUMBNAIL_SYNC_PENDING = "thumbnail_sync_pending"
DATA_THUMBNAIL_SYNC_LIMIT = "thumbnail_sync_limit"
DATA_THUMBNAIL_SYNC_REQUIRE_MEDIA = "thumbnail_sync_require_media"
DATA_THUMBNAIL_SYNC_REFRESH_CATALOG = "thumbnail_sync_refresh_catalog"
DATA_THUMBNAIL_SYNC_RETRY_AFTER = "thumbnail_sync_retry_after"
DATA_CAMERA_RESUME_TIMER = "camera_resume_timer"
DATA_RECORDING_TRIGGER_TIMER = "recording_trigger_timer"
DATA_RECORDING_TRIGGER_LAST = "recording_trigger_last"
DATA_RECORDING_TRIGGER_UNSUB = "recording_trigger_unsub"
CAMERA_RESUME_BACKGROUND_DELAY = 30
NATIVE_SESSION_IMPORT_FILE = ".tuya_recordings_native_session_import.json"
STALE_ENTRY_KEYS = {
    "client_id",
    "client_secret",
    "cookies",
    "go2rtc_url",
    "login_result",
    "protect_base_url",
    "protect_cookie",
    "protect_csrf",
    "rtsp_port",
    "server_host",
}

_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _monotonic(hass: HomeAssistant) -> float:
    loop = getattr(hass, "loop", None)
    if loop is not None and hasattr(loop, "time"):
        return float(loop.time())
    return time.monotonic()


def _thumbnail_background_cooldown_until(hass: HomeAssistant) -> float:
    return _monotonic(hass) + THUMBNAIL_BACKGROUND_COOLDOWN


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up Tuya Recordings."""
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate older Tuya Recordings config entries."""
    stale_keys = STALE_ENTRY_KEYS
    data = dict(entry.data)
    changed = entry.version < 6 or bool(stale_keys.intersection(data))
    if CONF_APP_PROFILE not in data:
        data[CONF_APP_PROFILE] = "smart_life"
        changed = True
    if changed:
        for key in stale_keys:
            data.pop(key, None)
        if entry.version < 5:
            # Version 4 populated this from moduleMap.wifi.pv, which is not
            # DeviceBean's MQTT communication version.
            data.pop(CONF_DEVICE_PROTOCOL_VERSIONS, None)

    config = getattr(hass, "config", None)
    if CONF_NATIVE_APP_SESSION not in data and config is not None:
        import_path = Path(config.path(NATIVE_SESSION_IMPORT_FILE))
        try:
            imported = await hass.async_add_executor_job(
                _read_native_session_import, import_path
            )
        except (OSError, ValueError) as err:
            _LOGGER.error("Could not import the saved Tuya mobile app session: %s", err)
            return False
        if imported is not None:
            data.update(imported)
            try:
                native_app_session(data)
            except NativeBackendSetupError as err:
                _LOGGER.error("Saved Tuya mobile app session import is incomplete: %s", err)
                return False
            await hass.async_add_executor_job(import_path.unlink)
            changed = True

    if changed:
        hass.config_entries.async_update_entry(entry, data=data, version=6)
    return True


def _read_native_session_import(path: Path) -> dict | None:
    """Read and validate one explicitly staged beta-session migration file."""
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ValueError("session import is not valid JSON") from err
    if not isinstance(payload, dict):
        raise ValueError("session import must be an object")
    session = payload.get(CONF_NATIVE_APP_SESSION)
    local_keys = payload.get(CONF_DEVICE_LOCAL_KEYS)
    protocols = payload.get(CONF_DEVICE_PROTOCOL_VERSIONS, {})
    if not isinstance(session, dict):
        raise ValueError("session import has no native app session")
    if not isinstance(local_keys, dict) or not local_keys or any(
        not isinstance(device_id, str)
        or not device_id.strip()
        or not isinstance(local_key, str)
        or not local_key.strip()
        for device_id, local_key in local_keys.items()
    ):
        raise ValueError("session import has no valid camera keys")
    if not isinstance(protocols, dict):
        raise ValueError("session import camera protocols are invalid")
    return {
        CONF_NATIVE_APP_SESSION: session,
        CONF_DEVICE_LOCAL_KEYS: local_keys,
        CONF_DEVICE_PROTOCOL_VERSIONS: protocols,
    }


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    dependency_errors = _required_dependency_errors(hass)
    _async_update_dependency_issues(hass, dependency_errors)
    if dependency_errors:
        raise ConfigEntryError("Tuya Recordings requires the official Tuya integration")
    _async_update_camera_repair_issues(hass)

    cache_path = Path(hass.config.path(".storage", DOMAIN, f"{entry.entry_id}_recordings.json"))
    entry_data = _entry_runtime_data(entry)
    _async_scrub_legacy_entry_data(hass, entry, entry_data)
    try:
        native_app_session(entry_data)
    except NativeBackendSetupError:
        raise ConfigEntryAuthFailed("Tuya mobile app camera playback authorization is required")
    client = TuyaRecordingsClient(
        entry_data,
        cache_path=cache_path,
        hass=hass,
    )
    @callback
    def update_camera_inventory(event=None):
        client.set_camera_inventory(_official_tuya_camera_device_ids(
            er.async_get(hass).entities.values(), dr.async_get(hass).devices,
        ))

    await hass.async_add_executor_job(client.load_cache)
    update_camera_inventory()
    entry.async_on_unload(hass.bus.async_listen("entity_registry_updated", update_camera_inventory))
    entry.async_on_unload(hass.bus.async_listen("device_registry_updated", update_camera_inventory))
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
    clean_data = {
        key: value
        for key, value in entry.data.items()
        if key not in STALE_ENTRY_KEYS and key != CONF_THUMBNAIL_SYNC_ENABLED
    }
    clean_options = {
        key: value
        for key, value in entry.options.items()
        if key not in STALE_ENTRY_KEYS and key != CONF_THUMBNAIL_SYNC_ENABLED
    }
    media_sync_enabled = bool(
        clean_options.get(
            CONF_MEDIA_SYNC_ENABLED,
            clean_data.get(CONF_MEDIA_SYNC_ENABLED, DEFAULT_MEDIA_SYNC_ENABLED),
        )
    )
    persisted_pause = clean_options.get(
        CONF_CLOUD_ACTIVITY_PAUSED,
        clean_data.get(CONF_CLOUD_ACTIVITY_PAUSED, False),
    )
    if not media_sync_enabled and persisted_pause:
        clean_options[CONF_CLOUD_ACTIVITY_PAUSED] = False
    if clean_data != dict(entry.data) or clean_options != dict(entry.options):
        hass.config_entries.async_update_entry(entry, data=clean_data, options=clean_options, version=5)


def _entry_runtime_data(entry: ConfigEntry) -> dict:
    """Combine persisted setup and options for one runtime client."""
    runtime = {**entry.data, **entry.options}
    media_sync_enabled = bool(runtime.get(CONF_MEDIA_SYNC_ENABLED, DEFAULT_MEDIA_SYNC_ENABLED))
    runtime[CONF_THUMBNAIL_SYNC_ENABLED] = media_sync_enabled
    runtime[CONF_CLOUD_ACTIVITY_PAUSED] = bool(
        media_sync_enabled and runtime.get(CONF_CLOUD_ACTIVITY_PAUSED, False)
    )
    return runtime


def _required_dependency_errors(hass: HomeAssistant) -> set[str]:
    errors: set[str] = set()
    if not hass.config_entries.async_entries("tuya"):
        errors.add("tuya_required")
    return errors


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
    # Older releases incorrectly warned that cameras needed LocalTuya entries.
    ir.async_delete_issue(hass, DOMAIN, "localtuya_camera_setup_incomplete")


def _official_tuya_camera_device_ids(entity_entries, device_entries) -> dict[str, str]:
    camera_registry_ids = {
        entry.device_id
        for entry in entity_entries
        if getattr(entry, "platform", "") == "tuya"
        and str(getattr(entry, "entity_id", "")).startswith("camera.")
        and getattr(entry, "device_id", None)
        and getattr(entry, "disabled_by", None) is None
    }
    cameras: dict[str, str] = {}
    for device in device_entries:
        if getattr(device, "id", None) not in camera_registry_ids:
            continue
        for domain, device_id in getattr(device, "identifiers", set()):
            if domain == "tuya" and device_id:
                cameras[str(device_id)] = getattr(device, "name_by_user", None) or getattr(device, "name", None) or str(device_id)
    return cameras


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not isinstance(entry_data, dict) or not isinstance(entry_data.get("client"), TuyaRecordingsClient):
        return
    client: TuyaRecordingsClient = entry_data["client"]
    await hass.async_add_executor_job(client.update_options, _entry_runtime_data(entry))
    _async_schedule_media_sync(hass, entry)
    _async_setup_recording_triggers(hass, entry)
    if client.cloud_activity_paused:
        await async_pause_camera_work(hass, entry.entry_id)
    async_dispatcher_send(hass, SIGNAL_RECORDINGS_UPDATED, entry.entry_id)


async def async_pause_camera_work(hass: HomeAssistant, entry_id: str) -> None:
    """Clear queued Tuya camera work for an entry while camera activity is paused."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry_id)
    if not isinstance(entry_data, dict):
        return
    entry_data[DATA_MEDIA_SYNC_PENDING] = False
    entry_data[DATA_THUMBNAIL_SYNC_PENDING] = False
    entry_data.pop(DATA_THUMBNAIL_SYNC_LIMIT, None)
    entry_data.pop(DATA_THUMBNAIL_SYNC_REQUIRE_MEDIA, None)
    entry_data.pop(DATA_THUMBNAIL_SYNC_REFRESH_CATALOG, None)
    entry_data[DATA_THUMBNAIL_SYNC_RETRY_AFTER] = _thumbnail_background_cooldown_until(hass)
    if timer := entry_data.pop(DATA_CAMERA_RESUME_TIMER, None):
        timer()
    if timer := entry_data.pop(DATA_RECORDING_TRIGGER_TIMER, None):
        timer()


async def async_resume_camera_work(hass: HomeAssistant, entry_id: str) -> None:
    """Clear camera backoff when explicitly enabled cache work resumes."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry_id)
    if not isinstance(entry_data, dict):
        return
    client = entry_data.get("client")
    if not isinstance(client, TuyaRecordingsClient) or client.cloud_activity_paused:
        return
    await hass.async_add_executor_job(client.reset_camera_work_backoff)
    if timer := entry_data.pop(DATA_CAMERA_RESUME_TIMER, None):
        timer()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if isinstance(entry_data, dict) and isinstance(entry_data.get("client"), TuyaRecordingsClient):
        _async_cancel_media_sync_schedule(entry_data)
        if unsubscribe := entry_data.pop(DATA_RECORDING_TRIGGER_UNSUB, None):
            unsubscribe()
        await hass.async_add_executor_job(entry_data["client"].close)
        await async_pause_camera_work(hass, entry.entry_id)
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if not _configured_entries(hass):
        hass.services.async_remove(DOMAIN, SERVICE_REFRESH)
        hass.services.async_remove(DOMAIN, SERVICE_CLEAR_CACHE)
        hass.services.async_remove(DOMAIN, SERVICE_CLEAR_VIDEO_CACHE)
        hass.services.async_remove(DOMAIN, SERVICE_SYNC_MEDIA)
        hass.services.async_remove(DOMAIN, SERVICE_POPULATE_THUMBNAILS)
        ha_frontend.async_remove_panel(hass, FRONTEND_URL_PATH, warn_if_unknown=False)
        hass.data.get(DOMAIN, {}).pop("_panel_registered", None)
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Reload Tuya Recordings."""
    if not await async_unload_entry(hass, entry):
        return False
    return await async_setup_entry(hass, entry)


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_REFRESH):
        return

    async def refresh_recordings(call) -> None:
        entries = _entries_for_call(hass, call.data.get(CONF_ENTRY_ID))
        for entry_data in entries:
            if entry_data.get(DATA_INTERACTIVE_PLAYBACK_ACTIVE):
                _LOGGER.info("Skipping Tuya Recordings refresh during interactive playback")
                continue
            if _entry_cloud_paused(entry_data):
                _LOGGER.info("Skipping Tuya Recordings refresh because camera activity is paused")
                continue
            try:
                client = entry_data["client"]
                if client.media_sync_enabled:
                    await hass.async_add_executor_job(client.refresh_recent_recordings)
                else:
                    await _async_request_catalog_refresh(
                        hass, entry_data["entry"].entry_id, "service", wait=True
                    )
            except TuyaRecordingsAuthError as exc:
                entry_data["entry"].async_start_reauth(hass)
                raise ConfigEntryAuthFailed("Tuya Recordings session expired") from exc
            async_dispatcher_send(hass, SIGNAL_RECORDINGS_UPDATED, entry_data["entry"].entry_id)

    async def clear_cache(call) -> None:
        entries = _entries_for_call(hass, call.data.get(CONF_ENTRY_ID))
        for entry_data in entries:
            await hass.async_add_executor_job(entry_data["client"].clear_cache)
            async_dispatcher_send(hass, SIGNAL_RECORDINGS_UPDATED, entry_data["entry"].entry_id)

    async def clear_video_cache(call) -> None:
        entries = _entries_for_call(hass, call.data.get(CONF_ENTRY_ID))
        for entry_data in entries:
            result = await hass.async_add_executor_job(entry_data["client"].clear_video_cache)
            _LOGGER.info(
                "Deleted %s cached Tuya recording video(s) and %s temp file(s) from %s",
                result["deleted_videos"],
                result["deleted_temp_files"],
                result["path"],
            )
            async_dispatcher_send(hass, SIGNAL_RECORDINGS_UPDATED, entry_data["entry"].entry_id)

    async def sync_media(call) -> None:
        entries = _entries_for_call(hass, call.data.get(CONF_ENTRY_ID))
        for entry_data in entries:
            if _entry_cloud_paused(entry_data):
                _LOGGER.info("Skipping Tuya Recordings media sync because camera activity is paused")
                continue
            await _async_request_camera_work(hass, entry_data["entry"].entry_id, "service", media=True, thumbnails=True, wait=True)

    async def populate_thumbnails(call) -> None:
        entries = _entries_for_call(hass, call.data.get(CONF_ENTRY_ID))
        limit = int(call.data.get(CONF_LIMIT) or THUMBNAIL_SYNC_LIMIT)
        for entry_data in entries:
            if _entry_cloud_paused(entry_data):
                _LOGGER.info("Skipping Tuya Recordings thumbnail sync because camera activity is paused")
                continue
            await _async_request_camera_work(
                hass,
                entry_data["entry"].entry_id,
                "service",
                thumbnails=True,
                thumbnail_limit=limit,
                require_media_sync=True,
                refresh_catalog=False,
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
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_VIDEO_CACHE, clear_video_cache, schema=schema)
    hass.services.async_register(DOMAIN, SERVICE_SYNC_MEDIA, sync_media, schema=schema)
    hass.services.async_register(DOMAIN, SERVICE_POPULATE_THUMBNAILS, populate_thumbnails, schema=thumbnail_schema)


def _async_register_views(hass: HomeAssistant) -> None:
    if hass.data.setdefault(DOMAIN, {}).get("_playback_view_registered"):
        return
    hass.http.register_view(TuyaRecordingsPanelDataView(hass))
    hass.http.register_view(TuyaRecordingsDebugView(hass))
    hass.http.register_view(TuyaRecordingsTimelineView(hass))
    hass.http.register_view(TuyaRecordingsThumbnailView(hass))
    hass.data[DOMAIN]["_playback_view_registered"] = True


def _async_schedule_media_sync(hass: HomeAssistant, entry: ConfigEntry) -> None:
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not isinstance(entry_data, dict):
        return
    _async_cancel_media_sync_schedule(entry_data)
    client = entry_data.get("client")
    if (
        not isinstance(client, TuyaRecordingsClient)
        or client.cloud_activity_paused
        or not _client_recording_backend_available(client)
    ):
        return

    if not client.media_sync_enabled:
        async def _run_catalog_cycle(now) -> None:
            del now
            if _entry_cloud_paused(_entry_data(hass, entry.entry_id)):
                return
            await _async_request_catalog_refresh(hass, entry.entry_id, "catalog_cycle")

        entry_data[DATA_MEDIA_SYNC_SCHEDULE] = [
            async_track_time_interval(hass, _run_catalog_cycle, CATALOG_SYNC_INTERVAL),
            async_call_later(hass, CATALOG_SYNC_STARTUP_DELAY, _run_catalog_cycle),
        ]
        return

    async def _run_cache_cycle(now) -> None:
        if _entry_cloud_paused(_entry_data(hass, entry.entry_id)):
            return
        await _async_request_camera_work(
            hass,
            entry.entry_id,
            "cache_cycle",
            media=True,
            thumbnails=True,
            thumbnail_limit=THUMBNAIL_BACKGROUND_LIMIT,
            require_media_sync=True,
            refresh_catalog=False,
        )

    entry_data[DATA_MEDIA_SYNC_SCHEDULE] = [
        async_track_time_interval(hass, _run_cache_cycle, MEDIA_SYNC_INTERVAL),
        async_call_later(hass, MEDIA_SYNC_STARTUP_DELAY, _run_cache_cycle),
    ]


def _async_cancel_media_sync_schedule(entry_data: dict) -> None:
    """Cancel every timer owned by optional local media caching."""
    for cancel in entry_data.pop(DATA_MEDIA_SYNC_SCHEDULE, []):
        cancel()


def _async_setup_recording_triggers(hass: HomeAssistant, entry: ConfigEntry) -> None:
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not isinstance(entry_data, dict):
        return
    if unsubscribe := entry_data.pop(DATA_RECORDING_TRIGGER_UNSUB, None):
        unsubscribe()
    # HA entity changes are noisy and not reliable proof that a new SD clip
    # exists. Cache mode uses one bounded timer-driven command path instead.
    return


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


async def _async_request_catalog_refresh(
    hass: HomeAssistant,
    entry_id: str,
    reason: str,
    *,
    wait: bool = False,
) -> None:
    """Queue one lightweight, newest-first recording catalog pass.

    This is deliberately separate from optional local media caching. The
    native backend's shared command queue keeps it behind active playback and
    other camera commands.
    """
    entry_data = _entry_data(hass, entry_id)
    if not isinstance(entry_data, dict) or not isinstance(entry_data.get("client"), TuyaRecordingsClient):
        return
    client: TuyaRecordingsClient = entry_data["client"]
    if entry_data.get(DATA_INTERACTIVE_PLAYBACK_ACTIVE):
        _LOGGER.debug("Skipping Tuya Recordings catalog refresh during interactive playback")
        return
    if client.cloud_activity_paused or not _client_recording_backend_available(client):
        return

    task = entry_data.get(DATA_CATALOG_REFRESH_TASK)
    if task is None or task.done():
        task = hass.async_create_task(_async_run_catalog_refresh(hass, entry_id, reason))
        entry_data[DATA_CATALOG_REFRESH_TASK] = task
    if wait:
        await task


async def _async_run_catalog_refresh(hass: HomeAssistant, entry_id: str, reason: str) -> None:
    """Run one bounded metadata-only catalog pass without media transfer."""
    entry_data = _entry_data(hass, entry_id)
    if not isinstance(entry_data, dict) or not isinstance(entry_data.get("client"), TuyaRecordingsClient):
        return
    client: TuyaRecordingsClient = entry_data["client"]
    try:
        if entry_data.get(DATA_INTERACTIVE_PLAYBACK_ACTIVE) or client.cloud_activity_paused:
            return
        result = await hass.async_add_executor_job(
            client.refresh_background_catalog, CATALOG_SYNC_DAYS_PER_PASS
        )
        _LOGGER.info("Tuya Recordings catalog refresh result for %s via %s: %s", entry_id, reason, result)
        if not client.cloud_activity_paused:
            async_dispatcher_send(hass, SIGNAL_RECORDINGS_UPDATED, entry_id)
    except (CameraWorkBusy, CameraWorkCancelled):
        _LOGGER.debug("Tuya Recordings catalog refresh deferred for %s", entry_id)
    except TuyaRecordingsAuthError as exc:
        if not client.cloud_activity_paused:
            entry_data["entry"].async_start_reauth(hass)
        raise ConfigEntryAuthFailed("Tuya Recordings session expired") from exc
    finally:
        if entry_data.get(DATA_CATALOG_REFRESH_TASK) is not None:
            entry_data.pop(DATA_CATALOG_REFRESH_TASK, None)


async def _async_request_camera_work(
    hass: HomeAssistant,
    entry_id: str,
    reason: str,
    *,
    media: bool = False,
    thumbnails: bool = False,
    thumbnail_limit: int = THUMBNAIL_SYNC_LIMIT,
    require_media_sync: bool = True,
    refresh_catalog: bool = True,
    wait: bool = False,
) -> None:
    entry_data = hass.data.get(DOMAIN, {}).get(entry_id)
    if not isinstance(entry_data, dict) or not isinstance(entry_data.get("client"), TuyaRecordingsClient):
        return
    client: TuyaRecordingsClient = entry_data["client"]
    if entry_data.get(DATA_INTERACTIVE_PLAYBACK_ACTIVE):
        _LOGGER.debug("Skipping Tuya Recordings %s work during interactive playback", reason)
        return
    if client.cloud_activity_paused:
        entry_data[DATA_MEDIA_SYNC_PENDING] = False
        entry_data[DATA_THUMBNAIL_SYNC_PENDING] = False
        return
    if not _client_recording_backend_available(client):
        entry_data.pop(DATA_MEDIA_SYNC_PENDING, None)
        entry_data.pop(DATA_THUMBNAIL_SYNC_PENDING, None)
        entry_data.pop(DATA_THUMBNAIL_SYNC_LIMIT, None)
        entry_data.pop(DATA_THUMBNAIL_SYNC_REQUIRE_MEDIA, None)
        entry_data.pop(DATA_THUMBNAIL_SYNC_REFRESH_CATALOG, None)
        _LOGGER.debug("Skipping Tuya Recordings %s work because APK-native playback backend is not available", reason)
        return
    queued = False
    if media and client.media_sync_enabled:
        if not entry_data.get(DATA_MEDIA_SYNC_RUNNING):
            entry_data[DATA_MEDIA_SYNC_PENDING] = True
        queued = True
    if thumbnails and client.media_sync_enabled:
        retry_after = float(entry_data.get(DATA_THUMBNAIL_SYNC_RETRY_AFTER) or 0)
        if refresh_catalog and retry_after > _monotonic(hass):
            return
        if refresh_catalog:
            entry_data.pop(DATA_THUMBNAIL_SYNC_RETRY_AFTER, None)
        if not entry_data.get(DATA_THUMBNAIL_SYNC_RUNNING):
            entry_data[DATA_THUMBNAIL_SYNC_PENDING] = True
            previous_limit = entry_data.get(DATA_THUMBNAIL_SYNC_LIMIT)
            thumbnail_limit = max(1, int(thumbnail_limit or THUMBNAIL_BACKGROUND_LIMIT))
            entry_data[DATA_THUMBNAIL_SYNC_LIMIT] = (
                thumbnail_limit if previous_limit is None else
                min(int(previous_limit), thumbnail_limit)
            )
            entry_data[DATA_THUMBNAIL_SYNC_REQUIRE_MEDIA] = bool(
                entry_data.get(DATA_THUMBNAIL_SYNC_REQUIRE_MEDIA, True) and require_media_sync
            )
            entry_data[DATA_THUMBNAIL_SYNC_REFRESH_CATALOG] = bool(
                entry_data.get(DATA_THUMBNAIL_SYNC_REFRESH_CATALOG, False) or refresh_catalog
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


def _client_recording_backend_available(client: TuyaRecordingsClient) -> bool:
    backend = getattr(client, "_recordings_backend", None)
    if backend is None:
        return True
    if getattr(backend, "name", "") == "apk-native-not-configured":
        return False
    return bool(getattr(backend, "clip_playback_available", True))


async def _async_run_camera_work(hass: HomeAssistant, entry_id: str, reason: str) -> None:
    entry_data = hass.data.get(DOMAIN, {}).get(entry_id)
    if not isinstance(entry_data, dict) or not isinstance(entry_data.get("client"), TuyaRecordingsClient):
        return
    client: TuyaRecordingsClient = entry_data["client"]
    while True:
        if entry_data.get(DATA_INTERACTIVE_PLAYBACK_ACTIVE):
            entry_data[DATA_MEDIA_SYNC_PENDING] = False
            entry_data[DATA_THUMBNAIL_SYNC_PENDING] = False
            return
        if client.cloud_activity_paused:
            await async_pause_camera_work(hass, entry_id)
            return
        if entry_data.pop(DATA_MEDIA_SYNC_PENDING, False):
            if (client.media_sync_enabled and not client.cloud_activity_paused
                    and not entry_data.get(DATA_INTERACTIVE_PLAYBACK_ACTIVE)):
                entry_data[DATA_MEDIA_SYNC_RUNNING] = True
                try:
                    cancellation = client.media_work_cancellation()
                    result = await hass.async_add_executor_job(client.sync_recordings, cancellation)
                    _LOGGER.info("Tuya Recordings media sync result for %s via %s: %s", entry_id, reason, result)
                except (CameraWorkBusy, CameraWorkCancelled):
                    _LOGGER.debug("Tuya Recordings media work stopped for %s", entry_id)
                    return
                except TuyaRecordingsAuthError as exc:
                    entry_data["entry"].async_start_reauth(hass)
                    raise ConfigEntryAuthFailed("Tuya Recordings session expired") from exc
                finally:
                    entry_data[DATA_MEDIA_SYNC_RUNNING] = False
                async_dispatcher_send(hass, SIGNAL_RECORDINGS_UPDATED, entry_id)
            continue

        if entry_data.pop(DATA_THUMBNAIL_SYNC_PENDING, False):
            limit = max(1, int(entry_data.pop(DATA_THUMBNAIL_SYNC_LIMIT, THUMBNAIL_SYNC_LIMIT) or THUMBNAIL_BACKGROUND_LIMIT))
            require_media_sync = bool(entry_data.pop(DATA_THUMBNAIL_SYNC_REQUIRE_MEDIA, True))
            refresh_catalog = bool(entry_data.pop(DATA_THUMBNAIL_SYNC_REFRESH_CATALOG, True))
            if client.cloud_activity_paused or entry_data.get(DATA_INTERACTIVE_PLAYBACK_ACTIVE):
                continue
            if require_media_sync and not client.media_sync_enabled:
                continue
            if not client.media_sync_enabled:
                continue
            entry_data[DATA_THUMBNAIL_SYNC_RUNNING] = True
            try:
                cancellation = client.thumbnail_work_cancellation(require_media_sync)
                result = await hass.async_add_executor_job(
                    client.sync_thumbnails,
                    limit,
                    cancellation,
                    refresh_catalog,
                )
                if client.cloud_activity_paused:
                    return
                if result.get("checked") or result.get("failed"):
                    entry_data[DATA_THUMBNAIL_SYNC_RETRY_AFTER] = _thumbnail_background_cooldown_until(hass)
                else:
                    entry_data.pop(DATA_THUMBNAIL_SYNC_RETRY_AFTER, None)
                _LOGGER.info("Tuya Recordings thumbnail sync result for %s via %s: %s", entry_id, reason, result)
            except (CameraWorkBusy, CameraWorkCancelled):
                _LOGGER.debug("Tuya Recordings thumbnail work stopped for %s", entry_id)
                return
            except TuyaRecordingsAuthError as exc:
                if not client.cloud_activity_paused:
                    entry_data["entry"].async_start_reauth(hass)
                raise ConfigEntryAuthFailed("Tuya Recordings session expired") from exc
            finally:
                entry_data[DATA_THUMBNAIL_SYNC_RUNNING] = False
            if not client.cloud_activity_paused:
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


def _entry_data(hass: HomeAssistant, entry_id: str) -> dict | None:
    entry_data = hass.data.get(DOMAIN, {}).get(entry_id)
    return entry_data if isinstance(entry_data, dict) else None


def _entry_cloud_paused(entry_data: dict | None) -> bool:
    if not isinstance(entry_data, dict):
        return False
    client = entry_data.get("client")
    return isinstance(client, TuyaRecordingsClient) and bool(client.cloud_activity_paused)
