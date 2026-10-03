"""Client and region constants of the Tuya mobile app build this SDK impersonates."""

from __future__ import annotations

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

PACKAGE_NAME = "com.tuya.smartlife"
CERT_SHA256 = (
    "0F:C3:61:99:9C:C0:C3:5B:A8:AC:A5:7D:AA:55:93:A2"
    ":0C:F5:57:27:70:2E:A8:5A:D7:B3:22:89:49:F8:88:FE"
)
DERIVED_KEY = "jfg5rs5kkmrj5mxahugvucrsvw43t48x"
APP_SECRET = "r3me7ghmxjevrvnpemwmhw3fxtacphyg"  # noqa: S105

COMPOSITE_KEY = f"{PACKAGE_NAME}_{CERT_SHA256}_{DERIVED_KEY}_{APP_SECRET}"

CLIENT_ID = "ekmnwp9f5pnh3trdtpgy"
CH_KEY = "ec9709a4"
APP_VERSION = "7.10.3"
SDK_VERSION = "5.2.0"
LANGUAGE = "en_US"
TTID = f"sdk_international@{CLIENT_ID}"

PARTNER_IDENTITY = "p1000018"
CLIENT_ID_SALT = "sdkfasodifca"

# The install id identifies this client to the account. Any stable 44-character
# value works, but it must not change between logins of the same account.
DEFAULT_DEVICE_FINGERPRINT = "a" * 44

DEFAULT_REGION = "us"
REGIONS: tuple[str, ...] = ("us", "eu", "cn", "in", "we")

# The device's own reason for turning an offer down: a previous session is
# still held. See PROTOCOL.md for the values seen in practice.
CLOSE_REASON_BUSY = 12


def gateway_url(region: str = DEFAULT_REGION) -> str:
    """Return the mobile API gateway of a region."""
    return f"https://a1-{region}.lifeaiot.com/api.json"


def broker_host(region: str = DEFAULT_REGION) -> str:
    """Return the signaling MQTT broker host of a region."""
    return f"m1-{region}.lifeaiot.com"


BROKER_PORT = 8883
