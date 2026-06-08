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
    APP_SECRET,
    APP_ID_PROPERTY,
    AYLA_EU_ADS_URL,
    AYLA_EU_USER_URL,
    CLOUD_HTTP_RETRY_BACKOFF,
    CLOUD_HTTP_RETRY_COUNT,
    CLOUD_TRANSIENT_HTTP_CODES,
    GIGYA_API_KEY,
    GIGYA_BASE_URL,
)

_LOGGER = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised when authentication fails."""


class CloudError(Exception):
    """Raised for Ayla API errors."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


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

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"auth_token {self._access_token}"}

    @staticmethod
    def _value_hint(value: Any) -> str:
        if isinstance(value, str):
            return f"len={len(value)}"
        return type(value).__name__

    def _log_http(
        self,
        method: str,
        url: str,
        status: int,
        elapsed_ms: float,
        *,
        detail: str = "",
    ) -> None:
        msg = "%s %s -> HTTP %d (%.0fms)%s"
        args: tuple[Any, ...] = (method, url, status, elapsed_ms, detail)
        if status >= 400:
            _LOGGER.warning(msg, *args)
        else:
            _LOGGER.debug(msg, *args)

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        ok_status: frozenset[int] | set[int] | None = None,
        op: str = "",
    ) -> Any:
        """HTTP request with transient retry (Eletta session paths only)."""
        await self.async_ensure_auth()
        if ok_status is None:
            ok_status = frozenset({200, 201})
        last_error: CloudError | None = None
        for attempt in range(CLOUD_HTTP_RETRY_COUNT + 1):
            started = time.monotonic()
            try:
                async with self._session.request(
                    method,
                    url,
                    headers=self._auth_headers(),
                    json=json_body,
                    data=data,
                ) as resp:
                    elapsed_ms = (time.monotonic() - started) * 1000
                    text = await resp.text()
                    detail = f" [{op}]" if op else ""
                    if json_body and "datapoint" in json_body:
                        prop_val = json_body["datapoint"].get("value")
                        detail += f" value={self._value_hint(prop_val)}"
                    self._log_http(method, url, resp.status, elapsed_ms, detail=detail)

                    if resp.status in CLOUD_TRANSIENT_HTTP_CODES and attempt < CLOUD_HTTP_RETRY_COUNT:
                        _LOGGER.warning(
                            "Ayla transient HTTP %d on %s %s; retry %d/%d in %.1fs",
                            resp.status,
                            method,
                            op or url.rsplit("/", 1)[-1],
                            attempt + 1,
                            CLOUD_HTTP_RETRY_COUNT,
                            CLOUD_HTTP_RETRY_BACKOFF * (attempt + 1),
                        )
                        await asyncio.sleep(CLOUD_HTTP_RETRY_BACKOFF * (attempt + 1))
                        continue

                    if resp.status not in ok_status:
                        raise CloudError(
                            f"{op or method} failed (HTTP {resp.status}): {text[:300]}",
                            http_status=resp.status,
                        )

                    if not text.strip():
                        return None
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError as err:
                        raise CloudError(
                            f"{op or method}: expected JSON, got {resp.content_type!r}: "
                            f"{text[:200]}",
                            http_status=resp.status,
                        ) from err
            except aiohttp.ClientError as err:
                elapsed_ms = (time.monotonic() - started) * 1000
                last_error = CloudError(
                    f"{op or method} network error after {elapsed_ms:.0f}ms: {err}"
                )
                if attempt < CLOUD_HTTP_RETRY_COUNT:
                    _LOGGER.warning(
                        "Ayla network error on %s %s; retry %d/%d: %s",
                        method,
                        op or url.rsplit("/", 1)[-1],
                        attempt + 1,
                        CLOUD_HTTP_RETRY_COUNT,
                        err,
                    )
                    await asyncio.sleep(CLOUD_HTTP_RETRY_BACKOFF * (attempt + 1))
                    continue
                raise last_error from err

        if last_error:
            raise last_error
        raise CloudError(f"{op or method} failed after retries")

    async def _gigya_login_and_jwt(self) -> str:
        """Login to Gigya + get JWT via signed request (HMAC-SHA1 with sessionSecret)."""
        login_url = f"{GIGYA_BASE_URL}/accounts.login"
        async with self._session.post(
            login_url,
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

    async def async_get_property(self, dsn: str, property_name: str) -> dict[str, Any]:
        """Fetch a single device property (fallback when coordinator.data is empty)."""
        await self.async_ensure_auth()
        url = f"{AYLA_EU_ADS_URL}/apiv1/dsns/{dsn}/properties/{property_name}.json"
        async with self._session.get(url, headers=self._auth_headers()) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise CloudError(
                    f"get_property {property_name} failed (HTTP {resp.status}): {text[:300]}"
                )
            data = await resp.json()
        prop = data.get("property")
        if not isinstance(prop, dict):
            raise CloudError(f"get_property {property_name}: unexpected response {data!r}")
        return prop

    async def async_get_property_resilient(
        self, dsn: str, property_name: str
    ) -> dict[str, Any]:
        """Eletta-only: GET with retry (confirm loop live app_id polling)."""
        url = f"{AYLA_EU_ADS_URL}/apiv1/dsns/{dsn}/properties/{property_name}.json"
        data = await self._request_json(
            "GET",
            url,
            ok_status=frozenset({200}),
            op=f"get {property_name} dsn={dsn}",
        )
        prop = data.get("property")
        if not isinstance(prop, dict):
            raise CloudError(f"get_property {property_name}: unexpected response {data!r}")
        raw = prop.get("value")
        _LOGGER.debug(
            "Property %s dsn=%s value=%s",
            property_name,
            dsn,
            raw if property_name == APP_ID_PROPERTY else self._value_hint(raw),
        )
        return prop

    async def async_post_cloud_session(
        self, dsn: str, connected_property: str, integration_app_id: int
    ) -> dict[str, Any]:
        """Register a cloud app session (app_device_connected / device_connected).

        Payload: base64(timestamp_4bytes + signed_app_id_4bytes), per DlghIoT.
        """
        now_s = int(time.time())
        payload = base64.b64encode(
            now_s.to_bytes(4, "big", signed=False)
            + integration_app_id_to_bytes(integration_app_id)
        ).decode("utf-8")
        _LOGGER.info(
            "POST cloud session connect dsn=%s property=%s app_id=%d (0x%08x) payload_len=%d",
            dsn,
            connected_property,
            integration_app_id,
            integration_app_id & 0xFFFFFFFF,
            len(payload),
        )
        url = (
            f"{AYLA_EU_ADS_URL}/apiv1/dsns/{dsn}/properties/"
            f"{connected_property}/datapoints.json"
        )
        result = await self._request_json(
            "POST",
            url,
            json_body={"datapoint": {"value": payload}},
            ok_status=frozenset({200, 201}),
            op=f"set {connected_property} dsn={dsn}",
        )
        return result or {}


def normalize_signed_app_id(app_id: int) -> int:
    """Convert an app id to signed int32 (matches machine property decimal form)."""
    return ((app_id & 0xFFFFFFFF) ^ 0x80000000) - 0x80000000


def integration_app_id_to_bytes(app_id: int) -> bytes:
    """Encode app id as signed int32 big-endian (DlghIoT convention)."""
    return normalize_signed_app_id(app_id).to_bytes(4, "big", signed=True)
