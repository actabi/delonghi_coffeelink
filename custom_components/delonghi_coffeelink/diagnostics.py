"""Privacy-safe diagnostics for DeLonghi Coffee Link."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from . import DelonghiRuntimeData
from .const import DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry[DelonghiRuntimeData]
) -> dict[str, Any]:
    """Return diagnostics without credentials, identifiers, values or raw frames."""
    integration = await async_get_integration(hass, DOMAIN)
    devices: list[dict[str, Any]] = []
    for coordinator in entry.runtime_data.coordinators:
        devices.append(
            {
                "model": coordinator.device.model,
                "oem_model": coordinator.device.oem_model,
                "software_version": coordinator.device.sw_version,
                "profile": coordinator.profile.key,
                "connection_status": coordinator.device.connection_status,
                "detected_properties": {
                    "command": coordinator.command_property,
                    "response": coordinator.response_property,
                    "connected": coordinator.connected_property,
                },
                "property_names": sorted((coordinator.data or {}).keys()),
                "coordinator": {
                    "last_update_success": coordinator.last_update_success,
                    "last_command_result": coordinator.last_command_result,
                    "active_beverage_known": coordinator.active_beverage_id is not None,
                    "monitor_status": coordinator.monitor.get("status_name"),
                },
            }
        )
    return {
        "integration": {
            "domain": DOMAIN,
            "version": integration.version,
            "config_entry_version": entry.version,
        },
        "devices": devices,
    }
