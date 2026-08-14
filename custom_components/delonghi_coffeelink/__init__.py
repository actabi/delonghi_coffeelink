"""DeLonghi Coffee Link integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType

from .ayla_client import AuthError, CloudError, DelonghiAylaClient
from .const import (
    ACTION_START,
    ACTION_STOP,
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

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]
BEVERAGE_KEYS = [beverage[1] for beverage in BEVERAGES]
BEVERAGE_IDS = {beverage[1]: beverage[0] for beverage in BEVERAGES}

DEVICE_TARGET = {vol.Required(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string])}
SERVICE_START_SCHEMA = vol.Schema(
    {**DEVICE_TARGET, vol.Required("beverage"): vol.In(BEVERAGE_KEYS)}
)
SERVICE_STOP_SCHEMA = vol.Schema(
    {**DEVICE_TARGET, vol.Optional("beverage"): vol.In(BEVERAGE_KEYS)}
)
SERVICE_RAW_SCHEMA = vol.Schema(
    {**DEVICE_TARGET, vol.Required("value_base64"): cv.string}
)


@dataclass(slots=True)
class DelonghiRuntimeData:
    """Runtime objects owned by one config entry."""

    client: DelonghiAylaClient
    coordinators: list[DelonghiCoordinator]


def _coordinators(hass: HomeAssistant) -> list[DelonghiCoordinator]:
    """Return coordinators from loaded config entries only."""
    result: list[DelonghiCoordinator] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        runtime = getattr(entry, "runtime_data", None)
        if isinstance(runtime, DelonghiRuntimeData):
            result.extend(runtime.coordinators)
    return result


def _target_coordinator(hass: HomeAssistant, call: ServiceCall) -> DelonghiCoordinator:
    """Resolve one explicit Home Assistant device target."""
    device_ids = call.data[ATTR_DEVICE_ID]
    if len(device_ids) != 1:
        raise HomeAssistantError("Select exactly one DeLonghi coffee maker")

    device = dr.async_get(hass).async_get(device_ids[0])
    if device is None:
        raise HomeAssistantError("The selected Home Assistant device no longer exists")

    dsns = {
        identifier[1]
        for identifier in device.identifiers
        if identifier[0] == DOMAIN
    }
    matches = [coord for coord in _coordinators(hass) if coord.device.dsn in dsns]
    if len(matches) != 1:
        raise HomeAssistantError(
            "The selected target does not resolve to exactly one loaded DeLonghi coffee maker"
        )
    return matches[0]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register integration actions once for the lifetime of Home Assistant."""

    async def _start_beverage(call: ServiceCall) -> None:
        coordinator = _target_coordinator(hass, call)
        await coordinator.async_send_beverage(
            BEVERAGE_IDS[call.data["beverage"]], ACTION_START
        )

    async def _stop_beverage(call: ServiceCall) -> None:
        coordinator = _target_coordinator(hass, call)
        if beverage := call.data.get("beverage"):
            await coordinator.async_send_beverage(BEVERAGE_IDS[beverage], ACTION_STOP)
        else:
            await coordinator.async_stop_active_beverage()

    async def _send_raw(call: ServiceCall) -> None:
        coordinator = _target_coordinator(hass, call)
        await coordinator.async_send_raw(call.data["value_base64"])

    hass.services.async_register(
        DOMAIN,
        SERVICE_START_BEVERAGE,
        _start_beverage,
        schema=SERVICE_START_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_STOP_BEVERAGE,
        _stop_beverage,
        schema=SERVICE_STOP_SCHEMA,
    )
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_SEND_RAW_COMMAND,
        _send_raw,
        schema=SERVICE_RAW_SCHEMA,
    )
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry[DelonghiRuntimeData]
) -> bool:
    """Set up one Coffee Link account and refresh every discovered machine."""
    session = async_get_clientsession(hass)
    client = DelonghiAylaClient(session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])

    try:
        await client.async_authenticate()
        devices = await client.async_get_devices()
    except AuthError as err:
        raise ConfigEntryAuthFailed("Coffee Link credentials were rejected") from err
    except CloudError as err:
        raise ConfigEntryNotReady(f"DeLonghi cloud is temporarily unavailable: {err}") from err

    if not devices:
        raise ConfigEntryNotReady("No DeLonghi devices found on this account")

    coordinators: list[DelonghiCoordinator] = []
    try:
        for device in devices:
            coordinator = DelonghiCoordinator(hass, client, device)
            await coordinator.async_load_learned()
            await coordinator.async_config_entry_first_refresh()
            coordinators.append(coordinator)
    except Exception:
        await asyncio.gather(
            *(coordinator.async_shutdown() for coordinator in coordinators),
            return_exceptions=True,
        )
        raise

    entry.runtime_data = DelonghiRuntimeData(client, coordinators)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ConfigEntry[DelonghiRuntimeData]
) -> bool:
    """Unload platforms and stop coordinator work for one account."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await asyncio.gather(
            *(coordinator.async_shutdown() for coordinator in entry.runtime_data.coordinators),
            return_exceptions=True,
        )
    return unload_ok
