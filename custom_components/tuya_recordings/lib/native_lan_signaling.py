"""APK-equivalent local P2P signaling over Tuya's extended-stream frame."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from typing import Any, Callable

import tinytuya

_LAN_EXT_STREAM = 0x40
_LAN_TIMEOUT_SECONDS = 5.0
_DISCOVERY_CACHE_SECONDS = 900.0


class NativeLanSignalError(RuntimeError):
    """Local P2P signaling could not be established for this camera."""


class NativeLanUnavailableError(NativeLanSignalError):
    """The camera cannot be reached through the local network."""


class Lan302Client:
    """One serialized local ``lan302Publish`` equivalent.

    The Android SDK sends the native P2P offer through ``lan302Publish`` when
    the camera is locally reachable and its ``p2pPolicy`` permits LAN.  The
    underlying LAN command is ``FRM_LAN_EXT_STREAM`` (0x40); its payload is the
    same JSON signaling object that would otherwise travel through MQTT.
    """

    def __init__(
        self,
        *,
        uid: str,
        device_id: str,
        session_id: str,
        local_key: str,
        on_answer: Callable[[str], None],
        on_candidate: Callable[[str], None],
        on_disconnect: Callable[[int], None],
        preconnect: bool,
        security_level: int,
        moto_id: str = "",
    ) -> None:
        self._uid = uid
        self._device_id = device_id
        self._session_id = session_id
        self._local_key = local_key
        self._moto_id = moto_id
        self._on_answer = on_answer
        self._on_candidate = on_candidate
        self._on_disconnect = on_disconnect
        self._preconnect = bool(preconnect)
        self._security_level = security_level
        self._device: Any | None = None
        self._lock = asyncio.Lock()
        self._reader: asyncio.Task | None = None
        self._closed = False
        self._offer_attempted = False

    async def async_connect(self) -> None:
        """Discover the single configured device and open one local socket."""
        async with self._lock:
            if self._device is None:
                opening = asyncio.create_task(asyncio.to_thread(self._open_device))
                try:
                    self._device = await asyncio.shield(opening)
                except asyncio.CancelledError:
                    with suppress(Exception):
                        self._device = await _finish_device_io(opening)
                    raise
                await _join_device_io(self._connect_device, self._device)

    async def async_send_offer(
        self,
        sdp: str,
        ice_servers: Any,
        trace_id: str,
        tcp_token: Any,
        log_config: Any,
    ) -> None:
        """Send one local offer, then listen for asynchronous replies."""
        message = {
            "header": self._header("offer", trace_id),
            "msg": {
                "sdp": sdp,
                "preconnect": self._preconnect,
                "token": ice_servers,
                "tcp_token": tcp_token,
                "log": log_config,
            },
        }
        message["header"].update(
            {
                "is_pre": 0,
                "p2p_skill": 1635,
                "security_level": self._security_level,
            }
        )
        self._offer_attempted = True
        await self._async_send(message)
        if self._reader is None:
            self._reader = asyncio.create_task(self._read_messages())

    async def _read_messages(self) -> None:
        while not self._closed:
            async with self._lock:
                device = self._device
                if device is None:
                    return
                try:
                    device.set_socketTimeout(0.25)
                    message = await _join_device_io(device.receive)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._on_disconnect(0)
                    return
            if _is_device_error(message):
                self._on_disconnect(0)
                return
            dispatched = self._dispatch(message)
            # TinyTuya may return immediately on an empty/error response.
            # Do not turn that into an executor busy loop.
            await asyncio.sleep(0.01 if dispatched else 0.1)

    async def async_send_candidate(self, candidate: str) -> None:
        await self._async_send(
            {"header": self._header("candidate"), "msg": {"candidate": candidate}},
        )

    async def async_send_disconnect(self, close_reason: int = 4) -> None:
        if not self._offer_attempted:
            return
        try:
            await self._async_send(
                {
                    "header": self._header("disconnect"),
                    "msg": {"close_reason": close_reason, "close_reason_local": 0},
                },
            )
        except NativeLanSignalError:
            pass

    async def async_close(self) -> None:
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
            self._reader = None
        async with self._lock:
            device = self._device
            if device is not None:
                await _join_device_io(device.close)
                self._device = None

    def _header(self, message_type: str, trace_id: str = "") -> dict[str, Any]:
        header: dict[str, Any] = {
            "type": message_type,
            "from": self._uid,
            "to": self._device_id,
            "sessionid": self._session_id,
            "moto_id": self._moto_id,
            "path": "lan",
        }
        if trace_id:
            header["trace_id"] = trace_id
        return header

    def _open_device(self) -> Any:
        now = time.monotonic()
        cached = self._discovery_cache.get(self._device_id)
        if cached is not None and now - cached[0] < _DISCOVERY_CACHE_SECONDS:
            _, ip_address, protocol_version = cached
        else:
            discovered = tinytuya.find_device(dev_id=self._device_id)
            ip_address = discovered.get("ip") if isinstance(discovered, dict) else None
            version = discovered.get("version") if isinstance(discovered, dict) else None
            try:
                protocol_version = float(version)
            except (TypeError, ValueError) as err:
                raise NativeLanUnavailableError(
                    "Camera LAN protocol version is unavailable"
                ) from err
            if isinstance(ip_address, str) and ip_address:
                self._discovery_cache[self._device_id] = (
                    now,
                    ip_address,
                    protocol_version,
                )
        if not isinstance(ip_address, str) or not ip_address:
            raise NativeLanUnavailableError(
                "Camera is not discoverable on the local network"
            )
        device = tinytuya.Device(
            self._device_id,
            ip_address,
            self._local_key,
            version=protocol_version,
        )
        device.set_socketPersistent(True)
        device.set_socketTimeout(_LAN_TIMEOUT_SECONDS)
        device.set_retry(False)
        return device

    def _connect_device(self, device: Any) -> None:
        # TinyTuya 1.20 counts the initial socket attempt in this limit.
        # Open once before sending an offer, then forbid implicit reconnects.
        device.set_socketRetryLimit(1)
        try:
            if device._get_socket(False) is not True:
                self._discovery_cache.pop(self._device_id, None)
                raise NativeLanUnavailableError("Camera LAN socket is unavailable")
        finally:
            device.set_socketRetryLimit(0)

    async def _async_send(self, message: dict[str, Any]) -> None:
        async with self._lock:
            device = self._device
            if device is None:
                raise NativeLanSignalError("Local P2P signaling is not connected")
            await _join_device_io(self._send, device, message)

    def _send(self, device: Any, message: dict[str, Any]) -> None:
        payload = tinytuya.MessagePayload(
            _LAN_EXT_STREAM,
            json.dumps(message, separators=(",", ":")).encode(),
        )
        try:
            result = device.send(payload)
        except Exception as err:
            self._discovery_cache.pop(self._device_id, None)
            raise NativeLanSignalError("Local P2P signaling transport failed") from err
        if _is_device_error(result):
            self._discovery_cache.pop(self._device_id, None)
            raise NativeLanSignalError("Local P2P signaling send failed")

    def _dispatch(self, message: dict[str, Any] | None) -> bool:
        if not isinstance(message, dict):
            return False
        header = message.get("header")
        body = message.get("msg")
        if not isinstance(header, dict) or not isinstance(body, dict):
            return False
        if header.get("sessionid") not in (None, "", self._session_id):
            return False
        message_type = header.get("type")
        if message_type == "answer" and isinstance(body.get("sdp"), str):
            self._on_answer(body["sdp"])
            return True
        if message_type == "candidate" and isinstance(body.get("candidate"), str):
            self._on_candidate(body["candidate"])
            return True
        if message_type == "disconnect":
            reason = body.get("close_reason")
            self._on_disconnect(reason if type(reason) is int else 0)
            return True
        return False
    _discovery_cache: dict[str, tuple[float, str, float]] = {}


async def _join_device_io(operation: Callable[..., Any], *args: Any) -> Any:
    """Keep the socket lock until the synchronous operation actually ends."""
    task = asyncio.create_task(asyncio.to_thread(operation, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(Exception):
            await _finish_device_io(task)
        raise


async def _finish_device_io(task: asyncio.Task[Any]) -> Any:
    # A second cancellation must not release the lock while the thread runs.
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


def _is_device_error(result: Any) -> bool:
    return isinstance(result, dict) and "Err" in result and "Error" in result
