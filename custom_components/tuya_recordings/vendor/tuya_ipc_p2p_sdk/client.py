"""The SDK's entry point: one account, its cameras, and the streams they serve."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Self

from .camera_stream import (
    DEFAULT_BUSY_REFUSAL_LIMIT,
    DEFAULT_REFUSED_RETRY_SECONDS,
    DEFAULT_RETRY_MAX_SECONDS,
    DEFAULT_RETRY_MIN_SECONDS,
    DEFAULT_SESSION_COOLDOWN_SECONDS,
    DEFAULT_STALL_TIMEOUT_SECONDS,
    CameraStream,
)
from .const import DEFAULT_DEVICE_FINGERPRINT, DEFAULT_REGION, LOGGER
from .exceptions import TuyaIpcP2pGatewayError
from .gateway import GatewayClient
from .motion_detector import DEFAULT_SENSITIVITY

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import TracebackType

    import aiohttp

    from .models import AccountSession, MqttIdentity, StreamConfig, TuyaDevice


class TuyaIpcP2pClient:
    """
    Holds one account's login and hands out the cameras it can stream.

    The login session is refreshed on demand: a call the gateway rejects
    because the session expired is retried once behind a fresh login, so a
    long-running consumer never has to notice.
    """

    def __init__(
        self,
        email: str,
        password: str,
        country_code: str,
        region: str = DEFAULT_REGION,
        device_fingerprint: str = DEFAULT_DEVICE_FINGERPRINT,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Describe the account and the region its gateway lives in."""
        self._email = email
        self._password = password
        self._country_code = country_code
        self._gateway = GatewayClient(region, device_fingerprint, session)
        self._account: AccountSession | None = None
        self._login_lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        """Enter the context manager."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the gateway's HTTP session."""
        await self.async_close()

    async def async_login(self) -> AccountSession:
        """Log in, replacing any session this client already held."""
        async with self._login_lock:
            account = await self._gateway.async_login(
                self._email, self._password, self._country_code
            )
            self._account = account
            LOGGER.debug("Logged in as uid %s", account.uid)
            return account

    async def async_account(self) -> AccountSession:
        """Return the current session, logging in the first time it is asked for."""
        account = self._account
        if account is not None:
            return account
        return await self.async_login()

    async def async_uid(self) -> str:
        """Return the account uid the signaling payloads are addressed from."""
        return (await self.async_account()).uid

    async def async_mqtt_identity(self) -> MqttIdentity:
        """Return the signaling broker identity of this account."""
        return self._gateway.mqtt_identity(await self.async_account())

    async def async_list_devices(self) -> list[TuyaDevice]:
        """Return every device on the account, across all of its homes."""
        return await self._async_with_session(self._gateway.async_list_devices)

    async def async_discover_cameras(self) -> list[TuyaDevice]:
        """
        Return the devices that answer the IPC config API.

        Only a camera has an RTC config, so the API itself is the filter. Each
        probe mints a session on the device, which is why discovery belongs in
        setup rather than in a poll.
        """
        return await self._async_with_session(self._gateway.async_discover_cameras)

    async def async_stream_config(self, device_id: str, local_key: str) -> StreamConfig:
        """Fetch the config one session is built from, renewing the login if needed."""
        account = await self.async_account()
        try:
            return await self._gateway.async_stream_config(account, device_id, local_key)
        except TuyaIpcP2pGatewayError as exception:
            if not exception.session_expired:
                raise
            LOGGER.debug("The login session was rejected (%s); logging in again", exception)
        account = await self.async_login()
        return await self._gateway.async_stream_config(account, device_id, local_key)

    def create_camera_stream(  # noqa: PLR0913, PLR0917 -- one knob per timing the device imposes
        self,
        device_id: str,
        local_key: str,
        motion_sensitivity: float = DEFAULT_SENSITIVITY,
        stall_timeout_seconds: float = DEFAULT_STALL_TIMEOUT_SECONDS,
        retry_min_seconds: float = DEFAULT_RETRY_MIN_SECONDS,
        retry_max_seconds: float = DEFAULT_RETRY_MAX_SECONDS,
        session_cooldown_seconds: float = DEFAULT_SESSION_COOLDOWN_SECONDS,
        busy_refusal_limit: int = DEFAULT_BUSY_REFUSAL_LIMIT,
        refused_retry_seconds: float = DEFAULT_REFUSED_RETRY_SECONDS,
    ) -> CameraStream:
        """Build a supervised stream for one camera; nothing connects until it is started."""
        return CameraStream(
            self,
            device_id,
            local_key,
            motion_sensitivity=motion_sensitivity,
            stall_timeout_seconds=stall_timeout_seconds,
            retry_min_seconds=retry_min_seconds,
            retry_max_seconds=retry_max_seconds,
            session_cooldown_seconds=session_cooldown_seconds,
            busy_refusal_limit=busy_refusal_limit,
            refused_retry_seconds=refused_retry_seconds,
        )

    async def _async_with_session[T](self, call: Callable[[AccountSession], Awaitable[T]]) -> T:
        """Run a session-scoped call, logging in again once if the session expired."""
        account = await self.async_account()
        try:
            return await call(account)
        except TuyaIpcP2pGatewayError as exception:
            if not exception.session_expired:
                raise
            LOGGER.debug("The login session was rejected (%s); logging in again", exception)
        return await call(await self.async_login())

    async def async_close(self) -> None:
        """Release the gateway's HTTP session, if this client opened it."""
        await self._gateway.async_close()
