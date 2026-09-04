"""Config flow for the DeLonghi Coffee Link integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .ayla_client import AuthError, CloudError, DelonghiAylaClient
from .const import CONF_EMAIL, CONF_PASSWORD, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class DelonghiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
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
                errors["base"] = "invalid_auth"
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
