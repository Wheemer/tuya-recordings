"""
Public surface of the SDK.

Everything a consumer is meant to import is re-exported here, so the module
layout stays free to change without breaking the integration that depends on
it.
"""

from __future__ import annotations

from .camera_stream import (
    DEFAULT_BUSY_REFUSAL_LIMIT,
    DEFAULT_REFUSED_RETRY_SECONDS,
    DEFAULT_RETRY_MAX_SECONDS,
    DEFAULT_RETRY_MIN_SECONDS,
    DEFAULT_SESSION_COOLDOWN_SECONDS,
    DEFAULT_STALL_TIMEOUT_SECONDS,
    CameraStream,
)
from .client import TuyaIpcP2pClient
from .const import DEFAULT_REGION, REGIONS
from .exceptions import (
    TuyaIpcP2pAuthenticationError,
    TuyaIpcP2pConnectionError,
    TuyaIpcP2pDeviceBusyError,
    TuyaIpcP2pError,
    TuyaIpcP2pGatewayError,
    TuyaIpcP2pProtocolError,
    TuyaIpcP2pSessionError,
)
from .models import AccountSession, MqttIdentity, StreamConfig, TuyaDevice
from .motion_detector import DEFAULT_MOTION_HOLD_SECONDS, DEFAULT_SENSITIVITY, MotionDetector

__all__ = [
    "DEFAULT_BUSY_REFUSAL_LIMIT",
    "DEFAULT_MOTION_HOLD_SECONDS",
    "DEFAULT_REFUSED_RETRY_SECONDS",
    "DEFAULT_REGION",
    "DEFAULT_RETRY_MAX_SECONDS",
    "DEFAULT_RETRY_MIN_SECONDS",
    "DEFAULT_SENSITIVITY",
    "DEFAULT_SESSION_COOLDOWN_SECONDS",
    "DEFAULT_STALL_TIMEOUT_SECONDS",
    "REGIONS",
    "AccountSession",
    "CameraStream",
    "MotionDetector",
    "MqttIdentity",
    "StreamConfig",
    "TuyaDevice",
    "TuyaIpcP2pAuthenticationError",
    "TuyaIpcP2pClient",
    "TuyaIpcP2pConnectionError",
    "TuyaIpcP2pDeviceBusyError",
    "TuyaIpcP2pError",
    "TuyaIpcP2pGatewayError",
    "TuyaIpcP2pProtocolError",
    "TuyaIpcP2pSessionError",
]
