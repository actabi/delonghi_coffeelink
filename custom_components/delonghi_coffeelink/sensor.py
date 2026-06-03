"""Sensor platform for DeLonghi Coffee Link."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import COUNTER_SENSORS, DOMAIN, INFO_SENSORS, MANUFACTURER
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
