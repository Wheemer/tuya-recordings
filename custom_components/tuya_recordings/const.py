import logging
from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "tuya_recordings"
NAME = "Tuya Recordings"
LOGGER = logging.getLogger(__package__)
PLATFORMS = [Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]
MANUFACTURER = "Tuya"
SIGNAL_RECORDINGS_UPDATED = f"{DOMAIN}_recordings_updated"
MEDIA_SYNC_INTERVAL = timedelta(minutes=2)
MEDIA_SYNC_STARTUP_DELAY = 30
THUMBNAIL_SYNC_LIMIT = 10
THUMBNAIL_SYNC_INTERVAL = timedelta(minutes=1)
THUMBNAIL_SYNC_STARTUP_DELAY = 90
RECORDING_TRIGGER_SETTLE_DELAY = 45
RECORDING_TRIGGER_COOLDOWN = 90

CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_LOOKBACK_DAYS = "lookback_days"
CONF_MEDIA_SYNC_ENABLED = "media_sync_enabled"
CONF_MEDIA_SYNC_HOURS = "media_sync_hours"
CONF_MEDIA_STORAGE_PATH = "media_storage_path"
CONF_MEDIA_VIEW_RECORDINGS_ORDER = "media_view_recordings_order"
CONF_THUMBNAIL_SYNC_ENABLED = "thumbnail_sync_enabled"
CONF_REGION = "region"
CONF_USER_ID = "user_id"

DEFAULT_REGION = "us"
DEFAULT_LOOKBACK_DAYS = 0
DEFAULT_MEDIA_SYNC_ENABLED = False
DEFAULT_MEDIA_SYNC_HOURS = 0
DEFAULT_MEDIA_STORAGE_PATH = "/media/tuya_recordings"
DEFAULT_THUMBNAIL_SYNC_ENABLED = False
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
