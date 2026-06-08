"""Sensor platform for DeLonghi Coffee Link."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .ayla_client import normalize_signed_app_id
from .const import (
    APP_ID_PROPERTY,
    COUNTER_SENSORS,
    DOMAIN,
    INFO_SENSORS,
    INTEGRATION_CLOUD_APP_ID,
    MANUFACTURER,
)
from .coordinator import DelonghiCoordinator

_HA_CLOUD_SESSION_APP_ID = normalize_signed_app_id(INTEGRATION_CLOUD_APP_ID)

_LOGGER = logging.getLogger(__name__)


def _resolve_property(data: dict[str, Any] | None, candidates: list[str]) -> str | None:
    """Return the first candidate property name present on the device, else None.

    Property names differ across DeLonghi models (e.g. d700_tot_bev_b on Soul vs
    d701_tot_bev_b on Eletta Explore), so each sensor declares a candidate list.
    """
    data = data or {}
    for candidate in candidates:
        if candidate in data:
            return candidate
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinators: list[DelonghiCoordinator] = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for coord in coordinators:
        for candidates, key, friendly, icon in COUNTER_SENSORS:
            prop_name = _resolve_property(coord.data, candidates)
            if prop_name is None:
                _LOGGER.debug(
                    "Skipping counter '%s' for dsn=%s: none of %s present",
                    key, coord.device.dsn, candidates,
                )
                continue
            entities.append(
                DelonghiCounterSensor(coord, prop_name, key, friendly, icon)
            )
        for candidates, key, friendly, icon in INFO_SENSORS:
            prop_name = _resolve_property(coord.data, candidates)
            if prop_name is None:
                _LOGGER.debug(
                    "Skipping info sensor '%s' for dsn=%s: none of %s present",
                    key, coord.device.dsn, candidates,
                )
                continue
            entities.append(DelonghiInfoSensor(coord, prop_name, key, friendly, icon))
        entities.append(DelonghiConnectionSensor(coord))
        entities.append(DelonghiMachineStatusSensor(coord))
        entities.append(DelonghiLastCommandSensor(coord))
        if coord.profile.uses_cloud_session and APP_ID_PROPERTY in (coord.data or {}):
            entities.append(DelonghiCloudSessionAppIdSensor(coord))
    async_add_entities(entities)


class _Base(CoordinatorEntity[DelonghiCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coord: DelonghiCoordinator, unique_suffix: str, name: str, icon: str) -> None:
        super().__init__(coord)
        self._attr_unique_id = f"{coord.device.dsn}_{unique_suffix}"
        self._attr_name = name
        self._attr_icon = icon

    @property
    def device_info(self) -> DeviceInfo:
        d = self.coordinator.device
        return DeviceInfo(
            identifiers={(DOMAIN, d.dsn)},
            name=d.name or f"DeLonghi {d.dsn}",
            manufacturer=MANUFACTURER,
            model=d.oem_model or d.model,
            sw_version=d.sw_version,
            configuration_url=f"http://{d.lan_ip}" if d.lan_ip else None,
        )


class DelonghiCounterSensor(_Base):
    """Integer counter sensor with TOTAL_INCREASING state class."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        coord: DelonghiCoordinator,
        prop_name: str,
        key: str,
        friendly: str,
        icon: str,
    ) -> None:
        super().__init__(coord, key, friendly, icon)
        self._prop_name = prop_name
        self._logged_unparseable = False

    @property
    def native_value(self) -> int | None:
        prop = (self.coordinator.data or {}).get(self._prop_name)
        if not prop:
            return None
        val = prop.get("value")
        if val is None:
            return None
        # Soul exposes counters as plain integers. Some models may wrap the value
        # differently (e.g. a base64 binary blob); don't guess the layout - parse
        # what we can and log the raw value once so the format can be confirmed.
        if isinstance(val, bool):
            return None
        if isinstance(val, int):
            return val
        try:
            return int(str(val).strip())
        except (TypeError, ValueError):
            if not self._logged_unparseable:
                self._logged_unparseable = True
                _LOGGER.warning(
                    "Counter '%s' (%s): value is not a plain integer "
                    "(base_type=%s, raw=%r). Sensor left unknown - please report "
                    "this raw value so the parser can be extended.",
                    self._prop_name,
                    self._attr_name,
                    prop.get("base_type"),
                    val,
                )
            return None


class DelonghiInfoSensor(_Base):
    """Generic info sensor (version string, timestamp, etc.).

    The concrete property name is resolved per-model at setup time (see
    INFO_SENSORS candidate lists), so no name fallback is needed here.
    """

    def __init__(
        self,
        coord: DelonghiCoordinator,
        prop_name: str,
        key: str,
        friendly: str,
        icon: str,
    ) -> None:
        super().__init__(coord, key, friendly, icon)
        self._prop_name = prop_name

    @property
    def native_value(self) -> Any:
        prop = (self.coordinator.data or {}).get(self._prop_name)
        if not prop:
            return None
        return prop.get("value")


class DelonghiConnectionSensor(_Base):
    """Exposes Ayla connection status (Online / Offline)."""

    def __init__(self, coord: DelonghiCoordinator) -> None:
        super().__init__(coord, "connection_status", "Connection Status", "mdi:cloud")

    @property
    def native_value(self) -> str:
        return self.coordinator.device.connection_status


class DelonghiMachineStatusSensor(_Base):
    """Machine operational state decoded from ``d302_monitor_machine``
    (standby, ready, rinsing, ...). Contributed via PR #5 (@TischenkoArseny,
    based on the DlghIoT client). ``None``/unknown if the blob doesn't parse
    on this model - the parse error is surfaced as an attribute.
    """

    def __init__(self, coord: DelonghiCoordinator) -> None:
        super().__init__(coord, "machine_status", "Machine Status", "mdi:coffee-maker")

    @property
    def native_value(self) -> str | None:
        monitor = self.coordinator.monitor or {}
        if "error" in monitor:
            return None
        return monitor.get("status_name")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        monitor = self.coordinator.monitor or {}
        attrs: dict[str, Any] = {}
        for key in ("status", "progress", "action", "accessory", "error"):
            if key in monitor:
                attrs[key] = monitor[key]
        return attrs


def _parse_cloud_session_app_id(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return normalize_signed_app_id(int(str(raw).strip()))
    except (TypeError, ValueError):
        return None


def _cloud_session_holder(app_id: int | None) -> str:
    if app_id is None:
        return "unknown"
    if app_id == 0:
        return "free"
    if app_id == _HA_CLOUD_SESSION_APP_ID:
        return "ha"
    return "foreign"


class DelonghiCloudSessionAppIdSensor(_Base):
    """Diagnostic: machine property ``app_id`` (current cloud session holder).

    Unlike ``Last Connected`` (``app_device_connected``), this is the slot the
    official Coffee Link app checks before connecting — not the last connect POST.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: DelonghiCoordinator) -> None:
        super().__init__(
            coord, "cloud_session_app_id", "Cloud Session app_id", "mdi:key-chain"
        )

    @property
    def native_value(self) -> int | None:
        prop = (self.coordinator.data or {}).get(APP_ID_PROPERTY)
        if not isinstance(prop, dict):
            return None
        return _parse_cloud_session_app_id(prop.get("value"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        app_id = self.native_value
        attrs: dict[str, Any] = {"holder": _cloud_session_holder(app_id)}
        if app_id is not None:
            attrs["app_id_hex"] = f"{app_id & 0xFFFFFFFF:08x}"
        return attrs


class DelonghiLastCommandSensor(_Base):
    """Diagnostic: last command seen on the binary channel.

    Surfaces the command sniffer (see coordinator). When the official Coffee
    Link app sends a command, its exact base64 bytes appear here as the state,
    decoded in the attributes - including ``matches_integration`` which tells
    whether the app's bytes match what this integration would generate. This is
    the ground-truth needed to debug models where commands are silently ignored.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: DelonghiCoordinator) -> None:
        super().__init__(
            coord, "last_captured_command", "Last Captured Command", "mdi:radar"
        )

    @property
    def native_value(self) -> str | None:
        rec = self.coordinator.last_captured_command
        if not rec:
            return None
        # Frames are short (<= ~24 base64 chars), well within the 255 limit.
        return rec.get("raw_b64")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rec = self.coordinator.last_captured_command or {}
        keys = (
            "origin",
            "type",
            "style",
            "beverage_name",
            "beverage_id",
            "action_name",
            "recipe",
            "params",
            "crc",
            "crc_valid",
            "matches_integration",
            "builder_structural_b64",
            "structural_b64",
            "timestamp",
            "captured_at",
            "hex",
        )
        attrs = {k: rec[k] for k in keys if k in rec}
        resp = self.coordinator.last_machine_response
        if resp:
            attrs["last_machine_response_hex"] = resp.get("hex")
            attrs["last_machine_response_at"] = resp.get("captured_at")
        return attrs
