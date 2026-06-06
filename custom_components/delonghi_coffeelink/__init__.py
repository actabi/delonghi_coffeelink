"""DeLonghi Coffee Link integration (PrimaDonna Soul et autres modeles Ayla-based)."""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .ayla_client import AuthError, CloudError, DelonghiAylaClient
from .const import (
    BEVERAGES,
    CONF_EMAIL,
    CONF_PASSWORD,
    DOMAIN,
    SERVICE_SEND_RAW_COMMAND,
    SERVICE_START_BEVERAGE,
    SERVICE_STOP_BEVERAGE,
)
from .coordinator import DelonghiCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]

BEVERAGE_KEYS = [b[1] for b in BEVERAGES]

SERVICE_START_SCHEMA = vol.Schema({vol.Required("beverage"): vol.In(BEVERAGE_KEYS)})
SERVICE_STOP_SCHEMA = vol.Schema({vol.Required("beverage"): vol.In(BEVERAGE_KEYS)})
SERVICE_RAW_SCHEMA = vol.Schema({vol.Required("value_base64"): cv.string})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    client = DelonghiAylaClient(session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])

    try:
        await client.async_authenticate()
        devices = await client.async_get_devices()
    except AuthError as err:
        raise ConfigEntryNotReady(f"DeLonghi auth failed: {err}") from err
    except CloudError as err:
        raise ConfigEntryNotReady(f"DeLonghi cloud error: {err}") from err

    if not devices:
        raise ConfigEntryNotReady("No DeLonghi devices found on this account")

    for device in devices:
        _LOGGER.debug(
            "Discovered DeLonghi device: dsn=%s oem_model=%s model=%s sw_version=%s "
            "connection_status=%s lan_ip=%s",
            device.dsn,
            device.oem_model,
            device.model,
            device.sw_version,
            device.connection_status,
            device.lan_ip,
        )

    # One coordinator per device
    coordinators: list[DelonghiCoordinator] = []
    for device in devices:
        coord = DelonghiCoordinator(hass, client, device)
        # Restore any Eletta frames learned in previous runs before the first
        # refresh, so buttons can replay immediately after a restart.
        await coord.async_load_learned()
        await coord.async_config_entry_first_refresh()
        if coord.data:
            prop_names = sorted(coord.data.keys())
            _LOGGER.debug(
                "Ayla properties for dsn=%s (%d total): %s",
                device.dsn,
                len(prop_names),
                ", ".join(prop_names),
            )
        coordinators.append(coord)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinators

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _register_services(hass, coordinators)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


def _register_services(hass: HomeAssistant, coordinators: list[DelonghiCoordinator]) -> None:
    """Register integration-level services (apply to all devices for now)."""
    # Build lookup for beverage_id by key
    bev_by_key = {b[1]: b[0] for b in BEVERAGES}

    async def _start_beverage(call: ServiceCall) -> None:
        bev_id = bev_by_key[call.data["beverage"]]
        for coord in coordinators:
            await coord.async_send_beverage(bev_id, 0x01)

    async def _stop_beverage(call: ServiceCall) -> None:
        bev_id = bev_by_key[call.data["beverage"]]
        for coord in coordinators:
            await coord.async_send_beverage(bev_id, 0x02)

    async def _send_raw(call: ServiceCall) -> None:
        value = call.data["value_base64"]
        for coord in coordinators:
            prop = coord.command_property or "data_request"
            coord._record_sent(value)
            await coord.client.async_set_property_value(
                coord.device.dsn, prop, value
            )
            await coord.async_request_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_START_BEVERAGE, _start_beverage, schema=SERVICE_START_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_STOP_BEVERAGE, _stop_beverage, schema=SERVICE_STOP_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SEND_RAW_COMMAND, _send_raw, schema=SERVICE_RAW_SCHEMA
    )
