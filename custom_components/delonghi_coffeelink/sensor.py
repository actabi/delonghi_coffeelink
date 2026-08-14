"""Sensor platform for DeLonghi Coffee Link."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .ayla_client import normalize_signed_app_id
from .counters import counter_breakdown, parse_counter_value
from .const import (
    APP_ID_PROPERTY,
    COUNTER_SENSORS,
    DOMAIN,
    INFO_SENSORS,
    MANUFACTURER,
)
from .coordinator import DelonghiCoordinator

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
    coordinators = entry.runtime_data.coordinators
    entities: list[SensorEntity] = []
    for coord in coordinators:
        for candidates, key, _friendly, icon in COUNTER_SENSORS:
            prop_name = _resolve_property(coord.data, candidates)
            if prop_name is None:
                _LOGGER.debug(
                    "Skipping counter '%s' for dsn=%s: none of %s present",
                    key, coord.device.dsn, candidates,
                )
                continue
            entities.append(
                DelonghiCounterSensor(coord, prop_name, key, icon)
            )
        for candidates, key, _friendly, icon in INFO_SENSORS:
            prop_name = _resolve_property(coord.data, candidates)
            if prop_name is None:
                _LOGGER.debug(
                    "Skipping info sensor '%s' for dsn=%s: none of %s present",
                    key, coord.device.dsn, candidates,
                )
                continue
            entities.append(DelonghiInfoSensor(coord, prop_name, key, icon))
        entities.append(DelonghiConnectionSensor(coord))
        entities.append(DelonghiMachineStatusSensor(coord))
        entities.append(DelonghiLastCommandSensor(coord))
        if coord.profile.uses_cloud_session and APP_ID_PROPERTY in (coord.data or {}):
            entities.append(DelonghiCloudSessionAppIdSensor(coord))
    async_add_entities(entities)


class _Base(CoordinatorEntity[DelonghiCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coord: DelonghiCoordinator, unique_suffix: str, icon: str) -> None:
        super().__init__(coord)
        self._attr_unique_id = f"{coord.device.dsn}_{unique_suffix}"
        self._attr_translation_key = unique_suffix
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
        icon: str,
    ) -> None:
        super().__init__(coord, key, icon)
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
        # Soul exposes counters as plain integers; newer models (Eletta) publish
        # some as a JSON object of per-recipe sub-counts. parse_counter_value
        # handles both; an unparseable scalar yields None and is logged once so
        # the format can be reported and the parser extended.
        result = parse_counter_value(val)
        if result is None and not self._logged_unparseable:
            self._logged_unparseable = True
            _LOGGER.warning(
                "Counter '%s' (%s): value is not a plain integer "
                "(base_type=%s, raw=%r). Sensor left unknown - please report "
                "this raw value so the parser can be extended.",
                self._prop_name,
                self.name,
                prop.get("base_type"),
                val,
            )
        return result

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        prop = (self.coordinator.data or {}).get(self._prop_name)
        if not prop:
            return None
        return counter_breakdown(prop.get("value"))


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
        icon: str,
    ) -> None:
        super().__init__(coord, key, icon)
        self._prop_name = prop_name
        if key == "last_connected":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> Any:
        prop = (self.coordinator.data or {}).get(self._prop_name)
        if not prop:
            return None
        if self._prop_name in ("device_connected", "app_device_connected"):
            val = prop.get("data_updated_at")
            if val:
                return dt_util.parse_datetime(str(val))
            return None
        return prop.get("value")


class DelonghiConnectionSensor(_Base):
    """Exposes Ayla connection status (Online / Offline)."""

    def __init__(self, coord: DelonghiCoordinator) -> None:
        super().__init__(coord, "connection_status", "mdi:cloud")

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
        super().__init__(coord, "machine_status", "mdi:coffee-maker")

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
        # Surface the raw switches/alarms bitfields as hex for troubleshooting,
        # but only on ECAM models: the monitor parser may fill these keys for any
        # model, so this uses_cloud_session gate is what keeps them off the Soul.
        if self.coordinator.profile.uses_cloud_session:
            if "switches" in monitor:
                attrs["switches"] = f"0x{monitor['switches']:04X}"
            if "alarms" in monitor:
                attrs["alarms"] = f"0x{monitor['alarms']:08X}"
        return attrs


def _parse_cloud_session_app_id(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return normalize_signed_app_id(int(str(raw).strip()))
    except (TypeError, ValueError):
        return None


class DelonghiCloudSessionAppIdSensor(_Base):
    """Privacy-safe diagnostic for the current cloud-session holder."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: DelonghiCoordinator) -> None:
        super().__init__(
            coord, "cloud_session_app_id", "mdi:key-chain"
        )

    @property
    def native_value(self) -> str:
        prop = (self.coordinator.data or {}).get(APP_ID_PROPERTY)
        if not isinstance(prop, dict):
            return "unknown"
        return self.coordinator.cloud_session_holder(
            _parse_cloud_session_app_id(prop.get("value"))
        )


class DelonghiLastCommandSensor(_Base):
    """Privacy-safe diagnostic result for the latest command transaction."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: DelonghiCoordinator) -> None:
        super().__init__(
            coord, "last_captured_command", "mdi:radar"
        )

    @property
    def native_value(self) -> str:
        return self.coordinator.last_command_result

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rec = self.coordinator.last_captured_command or {}
        keys = (
            "origin",
            "type",
            "style",
            "beverage_name",
            "action_name",
            "crc_valid",
            "matches_integration",
            "captured_at",
        )
        attrs = {k: rec[k] for k in keys if k in rec}
        resp = self.coordinator.last_machine_response
        if resp:
            attrs["last_machine_response_at"] = resp.get("captured_at")
        return attrs
