"""Config flow for the DeLonghi Coffee Link integration."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .ayla_client import (
    GIGYA_ACCOUNT_LOCKED,
    AuthError,
    CloudError,
    DelonghiAylaClient,
)
from .const import CONF_EMAIL, CONF_PASSWORD, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

STEP_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})


def _auth_error_key(err: AuthError) -> str:
    """Map a sign-in failure to the form error the user can act on.

    A locked account gets its own message: "invalid password" there invites
    exactly the further attempts that keep it locked.
    """
    if err.code == GIGYA_ACCOUNT_LOCKED:
        return "account_locked"
    return "cannot_connect" if err.transient else "invalid_auth"


class DelonghiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]
            session = async_get_clientsession(self.hass)
            client = DelonghiAylaClient(session, email, password)
            try:
                await client.async_authenticate()
                devices = await client.async_get_devices()
            except AuthError as err:
                _LOGGER.error("Auth failed: %s", err)
                errors["base"] = _auth_error_key(err)
            except CloudError as err:
                _LOGGER.error("Cloud error: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during auth")
                errors["base"] = "unknown"
            else:
                if not devices:
                    errors["base"] = "no_devices"
                else:
                    # Use the user's email as unique_id to prevent duplicate entries
                    await self.async_set_unique_id(email.lower())
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"DeLonghi ({devices[0].name})",
                        data={CONF_EMAIL: email, CONF_PASSWORD: password},
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
            description_placeholders={"email": "Coffee Link account email"},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Started by Home Assistant when the cloud refuses the stored credentials.

        Polling has already stopped at this point (ConfigEntryAuthFailed), so no
        further sign-in is attempted until the user enters a password here.
        """
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again; the account (email) stays the same."""
        entry = self._get_reauth_entry()
        email = entry.data[CONF_EMAIL]
        errors: dict[str, str] = {}
        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            client = DelonghiAylaClient(
                async_get_clientsession(self.hass), email, password
            )
            try:
                await client.async_authenticate()
            except AuthError as err:
                _LOGGER.error("Reauth failed: %s", err)
                errors["base"] = _auth_error_key(err)
            except CloudError as err:
                _LOGGER.error("Cloud error during reauth: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during reauth")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: password}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"email": email},
        )
