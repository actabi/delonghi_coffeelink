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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinators: list[DelonghiCoordinator] = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for coord in coordinators:
        for prop_name, key, friendly, icon in COUNTER_SENSORS:
            entities.append(
                DelonghiCounterSensor(coord, prop_name, key, friendly, icon)
            )
        for prop_name, key, friendly, icon in INFO_SENSORS:
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

    @property
    def native_value(self) -> int | None:
        prop = (self.coordinator.data or {}).get(self._prop_name)
        if not prop:
            return None
        val = prop.get("value")
        try:
            return int(val) if val is not None else None
        except (TypeError, ValueError):
            return None


class DelonghiInfoSensor(_Base):
    """Generic info sensor (version string, timestamp, etc.)."""

    # Some property names differ across DeLonghi models (e.g. device_connected
    # vs app_device_connected). When the primary name is missing, try a fallback.
    _FALLBACKS = {
        "device_connected": "app_device_connected",
    }

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
        data = self.coordinator.data or {}
        prop = data.get(self._prop_name)
        if not prop and self._prop_name in self._FALLBACKS:
            prop = data.get(self._FALLBACKS[self._prop_name])
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
