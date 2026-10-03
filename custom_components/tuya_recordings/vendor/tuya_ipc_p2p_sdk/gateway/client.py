"""The Tuya mobile app gateway: signed, encrypted calls and what they return."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Self
from uuid import uuid4

import aiohttp
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ..const import (
    APP_VERSION,
    CH_KEY,
    CLIENT_ID,
    COMPOSITE_KEY,
    DEFAULT_DEVICE_FINGERPRINT,
    DEFAULT_REGION,
    LANGUAGE,
    SDK_VERSION,
    TTID,
    gateway_url,
)
from ..crypto import md5_hex
from ..exceptions import (
    TuyaIpcP2pAuthenticationError,
    TuyaIpcP2pConnectionError,
    TuyaIpcP2pError,
    TuyaIpcP2pGatewayError,
    TuyaIpcP2pProtocolError,
)
from ..json_types import (
    JsonObject,
    JsonValue,
    dump_json,
    optional_str,
    parse_json_object,
    require_str,
)
from ..models import AccountSession, MqttIdentity, StreamConfig, TuyaDevice
from .body import decrypt_post_data, encrypt_post_data
from .identity import build_mqtt_identity
from .signing import body_key, build_sign_string, post_data_sign_field, pre_login_body_key, sign

if TYPE_CHECKING:
    from types import TracebackType

_REQUEST_TIMEOUT_SECONDS = 20
_LOGIN_TOKEN_API = "smartlife.m.user.username.token.get"  # noqa: S105
_LOGIN_API = "smartlife.m.user.email.password.login"
_RTC_CONFIG_API = "m.ipc.v4.rtc.config.get"
_HOME_LIST_API = "tuya.m.location.list"
_DEVICE_LIST_API = "tuya.m.my.group.device.list"


def _encrypt_password(modulus_decimal: str, exponent: str, password_md5: str) -> str:
    """
    Encrypt the password MD5 under the key the token call hands out.

    The modulus arrives as a decimal string, the padding is PKCS#1 v1.5 and the
    output is hex rather than base64.
    """
    public_key = rsa.RSAPublicNumbers(int(exponent), int(modulus_decimal)).public_key()
    return public_key.encrypt(password_md5.encode(), padding.PKCS1v15()).hex()


class GatewayClient:
    """
    Calls the mobile API gateway on behalf of one account.

    Every call is signed, its body is AES-GCM encrypted (``et=3``) and its
    response envelope is encrypted the same way. The client owns no session
    state: callers pass the :class:`AccountSession` back in, so a caller can
    hold several accounts against one client.
    """

    def __init__(
        self,
        region: str = DEFAULT_REGION,
        device_fingerprint: str = DEFAULT_DEVICE_FINGERPRINT,
        session: aiohttp.ClientSession | None = None,
        composite_key: str = COMPOSITE_KEY,
    ) -> None:
        """Point the client at a region and adopt an HTTP session, if one was given."""
        self._region = region
        self._url = gateway_url(region)
        self._device_fingerprint = device_fingerprint
        self._composite_key = composite_key
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> Self:
        """Enter the context manager."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the HTTP session this client created."""
        await self.async_close()

    async def async_close(self) -> None:
        """Release the HTTP session, but only the one this client opened itself."""
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def async_call(
        self,
        api: str,
        version: str,
        post: JsonValue,
        session: AccountSession | None = None,
        extra_params: dict[str, str] | None = None,
    ) -> JsonValue:
        """
        Issue one signed ``et=3`` request and return the decrypted result.

        Without a session the call is a pre-login one, whose body key is
        derived from the composite key alone.
        """
        request_id = str(uuid4())
        key = (
            body_key(request_id, session.ecode, self._composite_key)
            if session
            else pre_login_body_key(request_id, self._composite_key)
        )
        encrypted = encrypt_post_data(key, dump_json(post))
        params = self._build_params(api, version, request_id, encrypted, session, extra_params)
        return self._unwrap(api, key, await self._post(api, params))

    def _build_params(
        self,
        api: str,
        version: str,
        request_id: str,
        encrypted: str,
        session: AccountSession | None,
        extra_params: dict[str, str] | None,
    ) -> dict[str, str]:
        """Assemble the form parameters, signing the whitelisted ones."""
        params = {
            "a": api,
            "v": version,
            "clientId": CLIENT_ID,
            "time": str(int(time.time())),
            "requestId": request_id,
            "lang": LANGUAGE,
            "deviceId": self._device_fingerprint,
            "appVersion": APP_VERSION,
            "ttid": TTID,
            "os": "Android",
            "sdkVersion": SDK_VERSION,
            "chKey": CH_KEY,
            "et": "3",
            "postData": encrypted,
        }
        if session:
            params["sid"] = session.sid
        if extra_params:
            params.update(extra_params)
        signed = dict(params)
        signed["postData"] = post_data_sign_field(encrypted)
        params["sign"] = sign(build_sign_string(signed), self._composite_key)
        return params

    async def _post(self, api: str, params: dict[str, str]) -> str:
        """Send the form and return the raw response text."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            async with asyncio.timeout(_REQUEST_TIMEOUT_SECONDS):
                response = await self._session.post(
                    self._url,
                    data=params,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": f"TY/{APP_VERSION}",
                    },
                )
                response.raise_for_status()
                return await response.text()
        except TimeoutError as exception:
            raise TuyaIpcP2pConnectionError(f"Failed to call {api}: timed out") from exception
        except aiohttp.ClientError as exception:
            raise TuyaIpcP2pConnectionError(f"Failed to call {api}: {exception}") from exception

    def _unwrap(self, api: str, key: str, raw: str) -> JsonValue:
        """Decrypt the response envelope and return the inner result."""
        envelope = parse_json_object(raw)
        result = envelope.get("result")
        if isinstance(result, str) and result:
            inner = parse_json_object(decrypt_post_data(key, result))
            envelope = {**envelope, **inner}
            result = inner.get("result")
        error_code = optional_str(envelope, "errorCode")
        if error_code or envelope.get("success") is False:
            message = optional_str(envelope, "errorMsg") or "request rejected"
            raise TuyaIpcP2pGatewayError(api, error_code or "", message)
        if result is None:
            raise TuyaIpcP2pProtocolError(f"Failed to call {api}: empty result")
        return result

    async def _async_call_object(
        self,
        api: str,
        version: str,
        post: JsonValue,
        session: AccountSession | None = None,
        extra_params: dict[str, str] | None = None,
    ) -> JsonObject:
        """Call an API whose result must be an object."""
        result = await self.async_call(api, version, post, session, extra_params)
        if not isinstance(result, dict):
            raise TuyaIpcP2pProtocolError(f"Failed to call {api}: result is not an object")
        return result

    async def _async_call_object_list(
        self,
        api: str,
        version: str,
        post: JsonValue,
        session: AccountSession,
        extra_params: dict[str, str] | None = None,
    ) -> list[JsonObject]:
        """Call an API whose result must be a list of objects."""
        result = await self.async_call(api, version, post, session, extra_params)
        if not isinstance(result, list):
            raise TuyaIpcP2pProtocolError(f"Failed to call {api}: result is not a list")
        return [item for item in result if isinstance(item, dict)]

    async def async_login(self, email: str, password: str, country_code: str) -> AccountSession:
        """
        Log in with email and password.

        The password's MD5 is RSA-encrypted under the key the token call hands
        out, and posted to the login call.
        """
        try:
            token = await self._async_call_object(
                _LOGIN_TOKEN_API,
                "2.0",
                {"countryCode": country_code, "username": email, "isUid": False},
            )
            encrypted_password = _encrypt_password(
                require_str(token, "publicKey"),
                require_str(token, "exponent"),
                md5_hex(password),
            )
            account = await self._async_call_object(
                _LOGIN_API,
                "3.0",
                {
                    "countryCode": country_code,
                    "email": email,
                    "ifencrypt": 1,
                    "options": '{"group": 1}',
                    "passwd": encrypted_password,
                    "token": require_str(token, "token"),
                },
            )
        except TuyaIpcP2pGatewayError as exception:
            raise TuyaIpcP2pAuthenticationError(f"Failed to log in: {exception}") from exception
        return AccountSession(
            sid=require_str(account, "sid"),
            ecode=require_str(account, "ecode"),
            uid=require_str(account, "uid"),
            device_fingerprint=self._device_fingerprint,
        )

    async def async_stream_config(
        self, session: AccountSession, device_id: str, local_key: str
    ) -> StreamConfig:
        """
        Fetch the RTC config a session is built from.

        Each fetch mints a fresh P2P session, media key and relay token, so a
        reconnect has to call this again rather than reuse the last result.
        """
        result = await self._async_call_object(
            _RTC_CONFIG_API, "1.0", {"devId": device_id}, session
        )
        return StreamConfig.from_json(result, device_id, local_key)

    async def async_list_devices(self, session: AccountSession) -> list[TuyaDevice]:
        """Return every device on the account, across all of its homes."""
        homes = await self._async_call_object_list(_HOME_LIST_API, "2.1", {}, session)
        devices: list[TuyaDevice] = []
        seen: set[str] = set()
        for home in homes:
            group_id = home.get("gid")
            if not isinstance(group_id, int):
                continue
            for raw in await self._async_call_object_list(
                _DEVICE_LIST_API, "1.0", {}, session, {"gid": str(group_id)}
            ):
                device = _parse_device(raw)
                if device and device.device_id not in seen:
                    seen.add(device.device_id)
                    devices.append(device)
        return devices

    async def async_discover_cameras(self, session: AccountSession) -> list[TuyaDevice]:
        """
        Return the account's devices that answer the IPC config API.

        The API is the filter: only a camera has an RTC config, so a device
        that answers it is one this SDK can stream.
        """
        cameras: list[TuyaDevice] = []
        for device in await self.async_list_devices(session):
            try:
                await self.async_stream_config(session, device.device_id, device.local_key)
            except TuyaIpcP2pError:
                continue
            cameras.append(device)
        return cameras

    def mqtt_identity(self, session: AccountSession) -> MqttIdentity:
        """Return the signaling broker identity of a logged-in account."""
        return build_mqtt_identity(session, self._region)


def _parse_device(raw: JsonObject) -> TuyaDevice | None:
    """Read one device list entry, skipping anything without an id."""
    device_id = optional_str(raw, "devId")
    local_key = optional_str(raw, "localKey")
    if not device_id or not local_key:
        return None
    return TuyaDevice(
        device_id=device_id,
        name=optional_str(raw, "name") or device_id,
        category=optional_str(raw, "category") or "",
        local_key=local_key,
        product_id=optional_str(raw, "productId"),
        online=raw.get("isOnline") is not False,
    )
