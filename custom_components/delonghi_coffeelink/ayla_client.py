"""Authentication and API client for DeLonghi Coffee Link via Ayla cloud.

Auth chain: Gigya email/password login -> Gigya JWT (HMAC-SHA1 signed request)
 -> Ayla SSO sign-in -> Ayla access_token.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .const import (
    APP_ID,
    APP_ID_PROPERTY,
    APP_SECRET,
    AYLA_EU_ADS_URL,
    AYLA_EU_USER_URL,
    CONNECT_POLL_TIMEOUT,
    CONNECT_REFRESH_INTERVAL,
    GIGYA_API_KEY,
    GIGYA_BASE_URL,
)

_LOGGER = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised when authentication fails."""


class CloudError(Exception):
    """Raised for Ayla API errors."""


@dataclass
class AylaDevice:
    """Minimal device info."""

    dsn: str
    name: str
    oem_model: str
    model: str
    sw_version: str
    lan_ip: str
    connection_status: str
    properties: dict[str, Any] = field(default_factory=dict)


class DelonghiAylaClient:
    """Client for Gigya + Ayla flow."""

    def __init__(self, session: aiohttp.ClientSession, email: str, password: str) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0

    @property
    def ads_url(self) -> str:
        return AYLA_EU_ADS_URL

    async def async_authenticate(self) -> None:
        """Perform full auth chain: Gigya -> JWT -> Ayla SSO."""
        jwt = await self._gigya_login_and_jwt()
        await self._ayla_sso_sign_in(jwt)

    async def async_ensure_auth(self) -> None:
        """Refresh access_token if expired."""
        if not self._access_token or time.time() > self._expires_at - 30:
            await self.async_authenticate()

    async def _gigya_login_and_jwt(self) -> str:
        """Login to Gigya + get JWT via signed request (HMAC-SHA1 with sessionSecret)."""
        # 1. accounts.login
        async with self._session.post(
            f"{GIGYA_BASE_URL}/accounts.login",
            data={
                "apiKey": GIGYA_API_KEY,
                "loginID": self._email,
                "password": self._password,
                "format": "json",
                "targetEnv": "mobile",
            },
        ) as resp:
            body = json.loads(await resp.text())
        if body.get("errorCode") != 0:
            raise AuthError(f"Gigya login failed: {body.get('errorMessage')} (code {body.get('errorCode')})")

        session_token = body["sessionInfo"]["sessionToken"]
        session_secret = body["sessionInfo"]["sessionSecret"]

        # 2. accounts.getJWT with HMAC-SHA1 signature
        timestamp = str(int(time.time()))
        nonce = f"{timestamp}_1"
        url = f"{GIGYA_BASE_URL}/accounts.getJWT"
        params = {
            "apiKey": GIGYA_API_KEY,
            "oauth_token": session_token,
            "format": "json",
            "timestamp": timestamp,
            "nonce": nonce,
        }
        sorted_params = "&".join(
            f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in sorted(params.items())
        )
        base_str = f"POST&{urllib.parse.quote(url, safe='')}&{urllib.parse.quote(sorted_params, safe='')}"
        sig = base64.b64encode(
            hmac.new(base64.b64decode(session_secret), base_str.encode(), hashlib.sha1).digest()
        ).decode()
        params["sig"] = sig
        async with self._session.post(url, data=params) as resp:
            jwt_body = json.loads(await resp.text())
        if jwt_body.get("errorCode") != 0:
            raise AuthError(f"Gigya getJWT failed: {jwt_body.get('errorMessage')}")
        return jwt_body["id_token"]

    async def _ayla_sso_sign_in(self, jwt_token: str) -> None:
        """Exchange JWT for Ayla access_token (form-urlencoded)."""
        url = f"{AYLA_EU_USER_URL}/api/v1/token_sign_in"
        data = {"token": jwt_token, "app_id": APP_ID, "app_secret": APP_SECRET}
        async with self._session.post(url, data=data) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                raise AuthError(f"Ayla SSO failed (HTTP {resp.status}): {text[:300]}")
            body = await resp.json()
        if "access_token" not in body:
            raise AuthError(f"Ayla SSO: no access_token in response: {body}")
        self._access_token = body["access_token"]
        self._refresh_token = body.get("refresh_token")
        self._expires_at = time.time() + body.get("expires_in", 3600)

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"auth_token {self._access_token}"}

    async def async_get_devices(self) -> list[AylaDevice]:
        """List all Ayla devices tied to this account."""
        await self.async_ensure_auth()
        url = f"{AYLA_EU_ADS_URL}/apiv1/devices.json"
        async with self._session.get(url, headers=self._auth_headers()) as resp:
            data = await resp.json()
        devices: list[AylaDevice] = []
        for wrap in data:
            d = wrap.get("device", wrap)
            devices.append(
                AylaDevice(
                    dsn=d.get("dsn", ""),
                    name=d.get("product_name") or d.get("dsn", ""),
                    oem_model=d.get("oem_model", ""),
                    model=d.get("model", ""),
                    sw_version=d.get("sw_version", ""),
                    lan_ip=d.get("lan_ip", ""),
                    connection_status=d.get("connection_status", "Unknown"),
                )
            )
        return devices

    async def async_get_properties(self, dsn: str) -> dict[str, Any]:
        """Fetch all properties of a device, keyed by property name."""
        await self.async_ensure_auth()
        url = f"{AYLA_EU_ADS_URL}/apiv1/dsns/{dsn}/properties.json"
        async with self._session.get(url, headers=self._auth_headers()) as resp:
            data = await resp.json()
        props: dict[str, Any] = {}
        for item in data:
            p = item.get("property", {})
            name = p.get("name")
            if name:
                props[name] = p
        return props

    async def async_set_property_value(
        self, dsn: str, property_name: str, value: Any
    ) -> dict[str, Any]:
        """Write a value to a device property (e.g. data_request)."""
        await self.async_ensure_auth()
        url = f"{AYLA_EU_ADS_URL}/apiv1/dsns/{dsn}/properties/{property_name}/datapoints.json"
        async with self._session.post(
            url, headers=self._auth_headers(), json={"datapoint": {"value": value}}
        ) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                raise CloudError(
                    f"set_property {property_name} failed (HTTP {resp.status}): {text[:300]}"
                )
            return await resp.json()

    @staticmethod
    def _normalize_signed_app_id(app_id: int) -> int:
        """Convert to 32-bit signed int (dlghiot convention)."""
        return ((app_id & 0xFFFFFFFF) ^ 0x80000000) - 0x80000000

    @staticmethod
    def app_id_to_bytes(app_id: int) -> bytes:
        """Encode cloud app_id as 4 signed big-endian bytes."""
        signed = DelonghiAylaClient._normalize_signed_app_id(app_id)
        return signed.to_bytes(4, byteorder="big", signed=True)

    async def async_get_property_value(self, dsn: str, property_name: str) -> Any:
        """Return the current value of a single device property."""
        props = await self.async_get_properties(dsn)
        prop = props.get(property_name)
        if isinstance(prop, dict):
            return prop.get("value")
        return None

    async def async_ensure_device_connected(
        self,
        dsn: str,
        connected_property: str,
        cloud_app_id: int,
        last_connect_at: float,
    ) -> tuple[int, float]:
        """Register (or refresh) a cloud app session before sending commands.

        Mirrors dlghiot ``connect()``: POST timestamp+app_id to
        ``app_device_connected`` / ``device_connected``, then wait until
        ``app_id`` on the device matches.
        """
        now_s = time.time()
        current_raw = await self.async_get_property_value(dsn, APP_ID_PROPERTY)
        try:
            current_app_id = int(current_raw) if current_raw is not None else 0
        except (TypeError, ValueError):
            current_app_id = 0

        if (
            current_app_id == cloud_app_id
            and last_connect_at > 0
            and last_connect_at + CONNECT_REFRESH_INTERVAL > now_s
        ):
            _LOGGER.debug(
                "Cloud app session still valid for dsn=%s (app_id=%s)", dsn, cloud_app_id
            )
            return cloud_app_id, last_connect_at

        if current_app_id not in (0, cloud_app_id):
            _LOGGER.info(
                "Another app holds session on dsn=%s (app_id=%s); adopting its id",
                dsn,
                current_app_id,
            )
            return current_app_id, last_connect_at

        timestamp = int(now_s).to_bytes(4, byteorder="big")
        payload = base64.b64encode(timestamp + self.app_id_to_bytes(cloud_app_id)).decode(
            "ascii"
        )
        _LOGGER.info(
            "Registering cloud app session on %s for dsn=%s (app_id=%s)",
            connected_property,
            dsn,
            cloud_app_id,
        )
        await self.async_set_property_value(dsn, connected_property, payload)

        deadline = now_s + CONNECT_POLL_TIMEOUT
        while time.time() < deadline:
            await asyncio.sleep(1)
            raw = await self.async_get_property_value(dsn, APP_ID_PROPERTY)
            try:
                if int(raw) == cloud_app_id:
                    _LOGGER.debug("Cloud app session confirmed for dsn=%s", dsn)
                    return cloud_app_id, time.time()
            except (TypeError, ValueError):
                pass

        _LOGGER.warning(
            "Timed out waiting for app_id=%s on dsn=%s after connect", cloud_app_id, dsn
        )
        return cloud_app_id, time.time()
