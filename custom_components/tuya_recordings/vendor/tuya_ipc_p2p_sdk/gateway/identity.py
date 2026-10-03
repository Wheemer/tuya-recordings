"""The signaling MQTT identity, derived from the login session."""

from __future__ import annotations

from ..const import (
    BROKER_PORT,
    CH_KEY,
    CLIENT_ID,
    CLIENT_ID_SALT,
    COMPOSITE_KEY,
    PACKAGE_NAME,
    PARTNER_IDENTITY,
    broker_host,
)
from ..crypto import md5_hex
from ..models import AccountSession, MqttIdentity


def mqtt_client_id(device_fingerprint: str, uid: str) -> str:
    """Return ``<packageName>_mb_<installId>_<md5(uid + salt)>_DEFAULT``."""
    return f"{PACKAGE_NAME}_mb_{device_fingerprint}_{md5_hex(uid + CLIENT_ID_SALT)}_DEFAULT"


def mqtt_username(sid: str, ecode: str, app_key: str = CLIENT_ID) -> str:
    """Return ``p1000018_v1_<appKey>_<chKey>_mb_<sid>`` plus the ecode-bound tail."""
    tail = md5_hex(md5_hex(app_key) + ecode)[16:32]
    return f"{PARTNER_IDENTITY}_v1_{app_key}_{CH_KEY}_mb_{sid}{tail}"


def mqtt_password(ecode: str, key: str = COMPOSITE_KEY) -> str:
    """
    Return ``md5(md5(K) + ecode)[8:24]``.

    The slice is used verbatim as the password string; it is not the hex of a
    byte sequence the broker decodes.
    """
    return md5_hex(md5_hex(key) + ecode)[8:24]


def build_mqtt_identity(session: AccountSession, region: str) -> MqttIdentity:
    """Assemble the broker identity of one logged-in account."""
    return MqttIdentity(
        host=broker_host(region),
        port=BROKER_PORT,
        client_id=mqtt_client_id(session.device_fingerprint, session.uid),
        username=mqtt_username(session.sid, session.ecode),
        password=mqtt_password(session.ecode),
    )
