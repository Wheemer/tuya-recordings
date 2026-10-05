import logging
from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "tuya_recordings"
NAME = "Tuya Recordings"
LOGGER = logging.getLogger(__package__)
PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.SENSOR,
    Platform.SWITCH,
]
MANUFACTURER = "Tuya"
SIGNAL_RECORDINGS_UPDATED = f"{DOMAIN}_recordings_updated"
MEDIA_SYNC_INTERVAL = timedelta(minutes=15)
MEDIA_SYNC_STARTUP_DELAY = 5 * 60
# Catalog refreshes only request the recording index. They never open clip
# playback, download media, or render thumbnails. Keep them regular enough for
# the timeline to remain current while the shared camera queue stays serial.
DEFAULT_CATALOG_SYNC_MINUTES = 15
CATALOG_SYNC_STARTUP_DELAY = 30
CATALOG_SYNC_DAYS_PER_PASS = 2
THUMBNAIL_SYNC_LIMIT = 10
THUMBNAIL_BACKGROUND_LIMIT = 10
# Thumbnails are rendered only from locally cached MP4 files. They do not own
# a timer or a camera session; cache cycles perform this local follow-up work.
THUMBNAIL_SYNC_INTERVAL = MEDIA_SYNC_INTERVAL
THUMBNAIL_SYNC_STARTUP_DELAY = MEDIA_SYNC_STARTUP_DELAY
THUMBNAIL_BACKGROUND_COOLDOWN = 60
RECORDING_TRIGGER_SETTLE_DELAY = 45
RECORDING_TRIGGER_COOLDOWN = 90

CONF_LOOKBACK_DAYS = "lookback_days"
CONF_CATALOG_SYNC_MINUTES = "catalog_sync_minutes"
CONF_MEDIA_SYNC_ENABLED = "media_sync_enabled"
CONF_MEDIA_SYNC_HOURS = "media_sync_hours"
CONF_MEDIA_STORAGE_PATH = "media_storage_path"
CONF_MEDIA_VIEW_RECORDINGS_ORDER = "media_view_recordings_order"
CONF_ALERT_RESET_SECONDS = "alert_reset_seconds"
CONF_THUMBNAIL_SYNC_ENABLED = "thumbnail_sync_enabled"
DATA_INTERACTIVE_PLAYBACK_ACTIVE = "interactive_playback_active"
CONF_APP_PROFILE = "app_profile"
CONF_CLOUD_ACTIVITY_PAUSED = "cloud_activity_paused"
CONF_REGION = "region"
CONF_NATIVE_APP_SESSION = "native_app_session"
CONF_DEVICE_LOCAL_KEYS = "device_local_keys"
CONF_DEVICE_PROTOCOL_VERSIONS = "device_protocol_versions"

DEFAULT_REGION = "us"
DEFAULT_APP_PROFILE = "smart_life"
DEFAULT_LOOKBACK_DAYS = 0
DEFAULT_MEDIA_SYNC_ENABLED = False
DEFAULT_MEDIA_SYNC_HOURS = 0
DEFAULT_MEDIA_STORAGE_PATH = "/media/tuya_recordings"
DEFAULT_THUMBNAIL_SYNC_ENABLED = False
DEFAULT_CLOUD_ACTIVITY_PAUSED = False
DEFAULT_ALERT_RESET_SECONDS = 60
MEDIA_VIEW_RECORDINGS_ORDER_OPTIONS = ["Descending", "Ascending"]

REGION_LABELS = {
    "us": "Western America",
    "eu": "Central Europe",
    "we": "Western Europe",
    "ea": "Eastern America",
    "cn": "China",
    "in": "India",
    "sg": "Singapore",
}

APP_PROFILE_LABELS = {
    "smart_life": "Smart Life",
    "tuya_smart": "Tuya Smart",
}
