"""APK local P2P signaling over Tuya's IPC_LAN_302 frame."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from typing import Any, Callable

import tinytuya

from ..vendor.tuya_ipc_p2p_sdk.json_types import dp_integer

_LAN_IPC_302 = 32
_LAN_CONTROL = 7
_LAN_TIMEOUT_SECONDS = 5.0
_DISCOVERY_CACHE_SECONDS = 900.0


class NativeLanSignalError(RuntimeError):
    """Local P2P signaling could not be established for this camera."""


class NativeLanUnavailableError(NativeLanSignalError):
    """The camera cannot be reached through the local network."""


class NativeLanDpReportError(NativeLanSignalError):
    """A dispatched LAN query lost its report channel."""


class Lan302Client:
    """One serialized local ``lan302Publish`` equivalent.

    The Android SDK sends the native P2P offer through ``lan302Publish`` when
    the camera is locally reachable and its ``p2pPolicy`` permits LAN.  The
    underlying LAN command is ``IPC_LAN_302`` (32); its payload is the
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
        on_failure: Callable[[str], None] | None = None,
    ) -> None:
        self._uid = uid
        self._device_id = device_id
        self._session_id = session_id
        self._local_key = local_key
        self._moto_id = moto_id
        self._on_answer = on_answer
        self._on_candidate = on_candidate
        self._on_disconnect = on_disconnect
        self._on_failure = on_failure
        self._preconnect = bool(preconnect)
        self._security_level = security_level
        self._device: Any | None = None
        self._lock = asyncio.Lock()
        self._reader: asyncio.Task | None = None
        self._closed = False
        self._offer_attempted = False
        self._query_state = "not_sent"
        self._query_protocol = "unselected"
        self._reader_state = "not_started"
        self._session_mismatches = 0
        self._received_messages = 0
        self._invalid_messages = 0
        self._signals: dict[str, int] = {}
        self._connect_state = "not_started"
        self._connect_error = "none"

    @property
    def diagnostics(self) -> str:
        """Describe the local query without keys, addresses or DP values."""
        version = getattr(self._device, "version", "unknown")
        return (f"version={version}, query_protocol={self._query_protocol}, query_state={self._query_state}, "
                f"reader={self._reader_state}, received={self._received_messages}, "
                f"invalid_shape={self._invalid_messages}, session_mismatches={self._session_mismatches}, "
                f"signals={self._signals or 'none'}, connect_state={self._connect_state}, "
                f"connect_error={self._connect_error}")

    def configure_offer_metadata(self, *, moto_id: str, security_level: int, preconnect: bool) -> None:
        """Reuse the established device socket for a native LAN offer."""
        self._moto_id = moto_id
        self._security_level = security_level
        self._preconnect = preconnect

    async def async_listen(self) -> None:
        """Read IPC_LAN_302 replies without sending an offer or another query."""
        async with self._lock:
            if self._closed or self._device is None:
                raise NativeLanUnavailableError("LAN answer listener needs an open socket")
            if self._reader is None:
                self._reader_state = "listening"
                self._reader = asyncio.create_task(self._read_messages())

    async def async_connect(self) -> None:
        """Discover the single configured device and open one local socket."""
        async with self._lock:
            if self._device is None:
                self._connect_state = "discovering"
                opening = asyncio.create_task(asyncio.to_thread(self._open_device))
                try:
                    self._device = await asyncio.shield(opening)
                except asyncio.CancelledError:
                    with suppress(Exception):
                        self._device = await _finish_device_io(opening)
                    raise
                except (NativeLanSignalError, OSError) as error:
                    self._connect_state = "discovery_failed"
                    self._connect_error = type(error).__name__
                    raise
                self._connect_state = "connecting"
                await _join_device_io(self._connect_device, self._device)
                self._connect_state = "connected"

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
        await self.async_listen()

    async def async_query_direct_dp(self, dp: str) -> int:
        """Send one CONTROL query on a socket not yet used for signaling."""
        async with self._lock:
            device = self._device
            if device is None or self._reader is not None:
                raise NativeLanUnavailableError("DP query needs an idle local socket")
            message = await _join_device_io(self._send_query, device, {
                "devId": self._device_id, "uid": self._uid,
                "t": int(time.time()), "dps": {dp: None},
            })
            while not self._closed:
                if _is_device_error(message):
                    raise NativeLanDpReportError("Local DP report channel failed")
                if isinstance(message, dict) and message.get("devId") in (None, "", self._device_id):
                    data = message.get("data", message)
                    dps = data.get("dps") if (
                        isinstance(data, dict) and not data.get("cid")
                        and data.get("devId") in (None, "", self._device_id)
                    ) else None
                    value = dp_integer(dps.get(dp)) if isinstance(dps, dict) else None
                    if value is not None:
                        self._query_state = "report_received"
                        return value
                device.set_socketTimeout(0.25)
                try:
                    message = await _join_device_io(device.receive)
                except OSError as error:
                    raise NativeLanDpReportError("Local DP report channel failed") from error
                await asyncio.sleep(0.05)
        raise NativeLanDpReportError("Local DP socket closed")

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
                    self._reader_failed("receive_failed")
                    return
            if _is_device_error(message):
                self._reader_failed("device_error")
                return
            dispatched = self._dispatch(message)
            # TinyTuya may return immediately on an empty/error response.
            # Do not turn that into an executor busy loop.
            await asyncio.sleep(0.01 if dispatched else 0.1)

    def _reader_failed(self, reason: str) -> None:
        self._reader_state = reason
        if self._on_failure is not None:
            self._on_failure(reason)
        else:
            self._on_disconnect(0)

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
        self._reader_state = "closed"
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
            result = device._get_socket(False)
            if result is not True:
                self._connect_state = "socket_failed"
                code = result.get("Err") if isinstance(result, dict) else None
                self._connect_error = (f"tinytuya_{code}" if str(code).isascii()
                                       and str(code).isdecimal() and len(str(code)) <= 4 else "socket_unavailable")
                self._discovery_cache.pop(self._device_id, None)
                raise NativeLanUnavailableError("Camera LAN socket is unavailable")
        except OSError as error:
            self._connect_state = "socket_failed"
            self._connect_error = type(error).__name__
            self._discovery_cache.pop(self._device_id, None)
            raise
        finally:
            device.set_socketRetryLimit(0)

    async def _async_send(self, message: dict[str, Any]) -> None:
        async with self._lock:
            device = self._device
            if device is None:
                raise NativeLanSignalError("Local P2P signaling is not connected")
            await _join_device_io(self._send, device, message)

    def _send(self, device: Any, message: dict[str, Any], command: int = _LAN_IPC_302) -> None:
        payload = tinytuya.MessagePayload(
            command,
            json.dumps(message, separators=(",", ":")).encode(),
        )
        header = getattr(device, "version_header", None)
        try:
            # lan302Publish uses normalControl, not the versioned DP-control
            # serializer. Change only this locked device for this one send.
            if command == _LAN_IPC_302 and isinstance(header, bytes):
                device.version_header = b""
            result = device.send(payload)
        except Exception as err:
            self._discovery_cache.pop(self._device_id, None)
            raise NativeLanSignalError("Local P2P signaling transport failed") from err
        finally:
            if command == _LAN_IPC_302 and isinstance(header, bytes):
                device.version_header = header
        if _is_device_error(result):
            self._discovery_cache.pop(self._device_id, None)
            raise NativeLanSignalError("Local P2P signaling send failed")

    def _send_query(self, device: Any, message: dict[str, Any]) -> Any:
        """Wait for the LAN delivery result, retaining an immediate DP report."""
        # LocalControl3_4 in both APKs wraps the same CONTROL data in
        # protocol 5; TinyTuya's raw MessagePayload bypasses that conversion.
        body = (
            {"protocol": 5, "t": message["t"], "data": message}
            if getattr(device, "version", 0) >= 3.4 else message
        )
        self._query_protocol = "5" if getattr(device, "version", 0) >= 3.4 else "legacy"
        self._query_state = "waiting_delivery"
        payload = tinytuya.MessagePayload(
            _LAN_CONTROL, json.dumps(body, separators=(",", ":")).encode(),
        )
        device.set_socketTimeout(_LAN_TIMEOUT_SECONDS)
        deadline = time.monotonic() + _LAN_TIMEOUT_SECONDS
        try:
            result = device._send_receive(payload)
            for _ in range(32):
                retcode = getattr(device, "cmd_retcode", None)
                data = result.get("data", result) if isinstance(result, dict) else None
                dps = data.get("dps") if isinstance(data, dict) else None
                matching_report = (
                    isinstance(result, dict) and result.get("devId") in (None, "", self._device_id)
                    and isinstance(data, dict) and data.get("devId") in (None, "", self._device_id)
                    and not data.get("cid") and isinstance(dps, dict)
                    and any(dp_integer(dps.get(dp)) is not None for dp in message["dps"])
                )
                if _is_device_error(result) or retcode is not None or matching_report:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                device.set_socketTimeout(remaining)
                result = device.receive()
        except Exception as error:
            self._query_state = "delivery_failed"
            self._discovery_cache.pop(self._device_id, None)
            raise NativeLanSignalError("Local DP query delivery failed") from error
        retcode = getattr(device, "cmd_retcode", None)
        data = result.get("data", result) if isinstance(result, dict) else None
        dps = data.get("dps") if isinstance(data, dict) else None
        valid_report = (
            isinstance(result, dict) and result.get("devId") in (None, "", self._device_id)
            and isinstance(data, dict) and data.get("devId") in (None, "", self._device_id)
            and not data.get("cid") and isinstance(dps, dict)
            and any(dp_integer(dps.get(dp)) is not None for dp in message["dps"])
        )
        if (_is_device_error(result) or (type(retcode) is int and retcode != 0)
                or (retcode is None and not valid_report)):
            self._query_state = "delivery_failed"
            self._discovery_cache.pop(self._device_id, None)
            raise NativeLanSignalError("Local DP query delivery failed")
        self._query_state = "report_received" if valid_report else "acknowledged"
        return result

    def _dispatch(self, message: dict[str, Any] | None) -> bool:
        if message is None:
            return False
        self._received_messages += 1
        if not isinstance(message, dict):
            self._invalid_messages += 1
            return False
        header = message.get("header")
        body = message.get("msg")
        if not isinstance(header, dict) or not isinstance(body, dict):
            self._invalid_messages += 1
            return False
        if header.get("sessionid") != self._session_id:
            self._session_mismatches += 1
            return False
        message_type = header.get("type")
        bucket = message_type if message_type in ("answer", "candidate", "disconnect") else "other"
        self._signals[bucket] = self._signals.get(bucket, 0) + 1
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
