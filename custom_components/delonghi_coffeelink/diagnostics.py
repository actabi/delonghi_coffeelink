"""Diagnostics download for DeLonghi Coffee Link (device page > Download diagnostics).

Built for one question the logs could not answer: is the monitor keepalive
running, and does the machine answer it? So this reports, per machine, which
datapoints were picked, when each monitor candidate was last published, and
what the keepalive did - from the coordinator's own state, so it works at any
log level.

Never in here: the account e-mail and password, tokens, the machine's serial
number, settings PIN, Wi-Fi MAC or LAN IP. Datapoint VALUES are only included
for the monitor candidates (machine status, no identifiers) and the command
channels (frames, no identifiers); everything else is listed by name only.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_EMAIL, CONF_PASSWORD, DOMAIN
from .coordinator import DelonghiCoordinator

TO_REDACT = {CONF_EMAIL, CONF_PASSWORD}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _published(prop: Any) -> str | None:
    return prop.get("data_updated_at") if isinstance(prop, dict) else None


def _value(prop: Any) -> Any:
    return prop.get("value") if isinstance(prop, dict) else None


def coordinator_diagnostics(coord: DelonghiCoordinator) -> dict[str, Any]:
    """Everything needed to see why a status does or does not refresh."""
    data = coord.data or {}
    channels = {
        name: {"data_updated_at": _published(data.get(name)), "value": _value(data.get(name))}
        for name in (coord.command_property, coord.response_property)
        if name
    }
    monitor_candidates = {
        name: {
            "present": name in data,
            "data_updated_at": _published(data.get(name)),
            "value": _value(data.get(name)),
        }
        for name in coord.monitor_candidate_names()
    }
    connected = coord.connected_property
    return {
        "device": {
            "oem_model": coord.device.oem_model,
            "model": coord.device.model,
            "sw_version": coord.device.sw_version,
            "connection_status": coord.device.connection_status,
            "connected_at": coord.device.connected_at,
            "profile": coord.profile.key,
        },
        "poll": {
            "last_update_success": coord.last_update_success,
            "consecutive_failures": coord._consecutive_failures,  # noqa: SLF001
            "datapoint_count": len(data),
        },
        "properties": {
            "command": coord.command_property,
            "response": coord.response_property,
            "connected": connected,
            "monitor": coord.monitor_property,
        },
        "keepalive": {
            "enabled": bool(coord.profile.keeps_monitor_session and connected),
            "writes": coord.keepalive_writes,
            "last_ok": _iso(coord.keepalive_last_ok),
            "last_error": coord.keepalive_last_error,
            "consecutive_failures": coord._keepalive_failures,  # noqa: SLF001
            "connected_datapoint_updated_at": _published(data.get(connected)) if connected else None,
        },
        "monitor": {
            "decoded": coord.monitor,
            "published_at": _iso(coord.monitor_updated_at),
            "age_seconds": coord.monitor_age,
            "stale": coord.monitor_is_stale,
            "candidates": monitor_candidates,
        },
        "channels": channels,
        "learned_frames": {
            "start": sorted(f"0x{bev:02x}" for bev in coord.learned_start_frames),
            "stop": sorted(f"0x{bev:02x}" for bev in coord.learned_stop_frames),
            "wake": coord.learned_wake_frame is not None,
        },
        "datapoint_names": sorted(data),
    }


def _coordinators(hass: HomeAssistant, entry: ConfigEntry) -> list[DelonghiCoordinator]:
    return list(hass.data.get(DOMAIN, {}).get(entry.entry_id, []))


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        # A list, not a dict keyed by DSN: the DSN is the Ayla device serial number.
        "machines": [coordinator_diagnostics(coord) for coord in _coordinators(hass, entry)],
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: dr.DeviceEntry
) -> dict[str, Any]:
    dsns = {ident for domain, ident in device.identifiers if domain == DOMAIN}
    for coord in _coordinators(hass, entry):
        if coord.device.dsn in dsns:
            return coordinator_diagnostics(coord)
    return {"error": "machine not loaded (removed from the account, or disabled in Home Assistant)"}
