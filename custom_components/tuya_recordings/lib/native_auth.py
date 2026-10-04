"""Explicit native-app QR authorization, independent of camera/stream startup.

The caller supplies a region-configured, signed mobile gateway async_call.
Do not pass the higher-level SDK client that automatically logs in again.
This module does not establish app allowlisting or native camera compatibility.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import time
from collections.abc import Awaitable, Callable


class NativeAuthorizationError(RuntimeError):
    """Authorization stopped without retry or exposing the remote payload."""


@dataclass(frozen=True, slots=True)
class NativeAppSession:
    sid: str = field(repr=False)
    ecode: str = field(repr=False)
    uid: str = field(repr=False)
    partner_identity: str = field(repr=False)
    mobile_mqtts_url: str = field(repr=False)
    device_fingerprint: str = field(repr=False)


class NativeQrAuthorization:
    """One token creation and one explicit completion attempt per instance.

    Construct only in a user-initiated setup flow. Await finish after the user
    confirms approval, not in a timer. cancel invalidates late responses too.
    All methods belong to the constructor's event loop.
    """

    def __init__(
        self,
        call: Callable[..., Awaitable[object]],
        *,
        device_fingerprint: str,
        clock=time.monotonic,
        qr_create_api: str = "thing.m.user.qr.token.create",
        qr_finish_api: str = "thing.m.user.qr.token.user.get",
        qr_scheme: str = "tuyaSmart",
    ):
        self._loop = asyncio.get_running_loop()
        self._call = call
        self._device_fingerprint = device_fingerprint
        self._qr_create_api = qr_create_api
        self._qr_finish_api = qr_finish_api
        self._qr_scheme = qr_scheme
        self._clock = clock
        self._state = "new"
        self._token: str | None = None
        self._expires = 0.0
        self._task: asyncio.Task | None = None

    def _check_loop(self):
        if asyncio.get_running_loop() is not self._loop:
            raise NativeAuthorizationError("Authorization belongs to another event loop")

    def cancel(self):
        self._check_loop()
        self._state = "closed"
        self._token = None
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    async def _request(self, api, body, timeout):
        task = self._task = asyncio.create_task(self._call(api, "1.0", body))
        try:
            async with asyncio.timeout(timeout) as deadline:
                result = await task
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError
            if deadline.expired():
                raise TimeoutError
            if self._state == "closed":
                raise NativeAuthorizationError("Authorization cancelled")
            return result
        except asyncio.CancelledError:
            self.cancel()
            raise
        except Exception:
            self.cancel()
            # SDK errors can contain response bodies or tokens. Never echo them.
            raise NativeAuthorizationError("Native app authorization failed; no retry was made") from None
        finally:
            self._task = None

    async def begin(self) -> str:
        """Return the APK QR payload; never create another token automatically."""
        self._check_loop()
        if self._state != "new":
            raise NativeAuthorizationError("Authorization already started or closed")
        self._state = "creating"
        self._expires = self._clock() + 300
        # ThingApiParams.checkAPIName rewrites the APK's thing prefix on wire.
        result = await self._request(self._qr_create_api, {}, 20)
        if not isinstance(result, str) or not result or len(result) > 2048 or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in result
        ):
            self.cancel()
            raise NativeAuthorizationError("Invalid native QR token")
        self._token = result
        self._state = "awaiting_approval"
        return f"{self._qr_scheme}--qrLogin?token={result}"

    async def finish(self) -> NativeAppSession:
        """Check once after explicit approval; do not poll pending/error states."""
        self._check_loop()
        if self._state != "awaiting_approval":
            raise NativeAuthorizationError("No pending native authorization")
        remaining = self._expires - self._clock()
        if remaining <= 0:
            self.cancel()
            raise NativeAuthorizationError("Native authorization expired locally")
        self._state = "completing"
        token, self._token = self._token, None
        result = await self._request(
            self._qr_finish_api, {"token": token}, min(20, remaining)
        )
        self._state = "closed"
        domain = result.get("domain") if isinstance(result, dict) else None
        mobile_mqtts_url = (
            domain.get("mobileMqttsUrl") if isinstance(domain, dict) else None
        )
        if not isinstance(result, dict) or any(
            not isinstance(result.get(key), str) or not result[key].strip() or len(result[key]) > 8192
            for key in ("sid", "ecode", "uid", "partnerIdentity")
        ) or not isinstance(mobile_mqtts_url, str) or not mobile_mqtts_url.strip() or len(
            mobile_mqtts_url
        ) > 1024:
            raise NativeAuthorizationError("Native authorization did not return a complete app session")
        device_fingerprint = self._device_fingerprint
        if (
            not isinstance(device_fingerprint, str)
            or not device_fingerprint.strip()
            or len(device_fingerprint) > 256
        ):
            raise NativeAuthorizationError("Native authorization did not return a complete app session")
        return NativeAppSession(
            sid=result["sid"],
            ecode=result["ecode"],
            uid=result["uid"],
            partner_identity=result["partnerIdentity"],
            mobile_mqtts_url=mobile_mqtts_url,
            device_fingerprint=device_fingerprint,
        )
