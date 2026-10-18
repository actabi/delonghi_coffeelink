"""Binary sensor platform for DeLonghi Coffee Link (maintenance alarms).

Maintenance state is decoded from the ``switches``/``alarms`` bitfields of the
machine monitor MonitorV2 blob (see ``monitor.py``).

Upstream created these only for ECAM models (``uses_cloud_session``). This fork
creates them for every machine, the PrimaDonna Soul included: the reference
Soul dump carries a full-length MonitorV2 blob (13-byte contents, so switches
and alarms are present), and the bit layout is the one the BLE integration uses
for the same EcamPacket (see const.ALARM_NAMES). NOT VERIFIED on a Soul beyond
the descale bit - the test plan removes the tank, the grounds container and
the beans and checks each sensor. A sensor is unavailable while the monitor
blob is missing, undecodable or stale, so it never reports a fossil.

Bit layout derived from the DlghIoT client (TischenkoArseny, PR #9 / issue #7).
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, PROJECT_URL
from .coordinator import DelonghiCoordinator

_DECALC_PERCENT_PROPERTY = "d512_percentage_to_deca"
_GROUNDS_COUNTER_PROPERTY = "d551_cnt_coffee_fondi"


def _prop_int(data: dict[str, Any] | None, prop_name: str) -> int:
    if not data:
        return 0
    prop = data.get(prop_name)
    if not isinstance(prop, dict):
        return 0
    val = prop.get("value")
    if val is None:
        return 0
    try:
        return int(str(val).strip())
    except (TypeError, ValueError):
        return 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinators: list[DelonghiCoordinator] = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = []
    for coord in coordinators:
        entities.extend(
            [
                DelonghiWaterTankBinarySensor(coord),
                DelonghiWasteContainerBinarySensor(coord),
                DelonghiDecalcificationBinarySensor(coord),
                DelonghiFilterBinarySensor(coord),
                DelonghiBeansEmptyBinarySensor(coord),
            ]
        )
    async_add_entities(entities)


class _Base(CoordinatorEntity[DelonghiCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

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
            configuration_url=PROJECT_URL,
        )

    def _monitor(self) -> dict[str, Any]:
        return self.coordinator.monitor or {}

    @property
    def available(self) -> bool:
        monitor = self._monitor()
        return (
            super().available
            and "alarms" in monitor
            and "error" not in monitor
            # A blob the machine stopped publishing is not a tank level.
            and not self.coordinator.monitor_is_stale
        )


class DelonghiWaterTankBinarySensor(_Base):
    """Water tank empty or removed (monitor alarm bit 0 / switch bit 4)."""

    def __init__(self, coord: DelonghiCoordinator) -> None:
        super().__init__(coord, "water_tank_empty", "mdi:water-off")

    @property
    def is_on(self) -> bool | None:
        monitor = self._monitor()
        if "alarms" not in monitor:
            return None
        switches = monitor.get("switches", 0)
        alarms = monitor["alarms"]
        # Switch bit 4 set = tank removed (same polarity as waste container bit 3).
        tank_missing = bool((switches >> 4) & 1)
        # Empty only: alarm bit 0 or tank missing - not bit 16 (low water warning).
        return bool((alarms >> 0) & 1 or tank_missing)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        monitor = self._monitor()
        if "alarms" not in monitor:
            return {}
        switches = monitor.get("switches", 0)
        alarms = monitor["alarms"]
        return {
            "water_tank_present": not bool((switches >> 4) & 1),
            "water_level_high": bool((switches >> 7) & 1),
            "water_level_low": bool((switches >> 6) & 1),
            "water_empty_alarm": bool((alarms >> 0) & 1),
            "water_low_alarm": bool((alarms >> 16) & 1),
            "water_tank_alarm": bool((alarms >> 13) & 1),
        }


class DelonghiWasteContainerBinarySensor(_Base):
    """Waste container full or missing."""

    def __init__(self, coord: DelonghiCoordinator) -> None:
        super().__init__(coord, "waste_container_full", "mdi:delete-empty")

    @property
    def is_on(self) -> bool | None:
        monitor = self._monitor()
        if "alarms" not in monitor:
            return None
        alarms = monitor["alarms"]
        switches = monitor.get("switches", 0)
        waste_full = bool((alarms >> 1) & 1)
        container_present = not bool((switches >> 3) & 1)
        return (not container_present) or waste_full

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        monitor = self._monitor()
        if "alarms" not in monitor:
            return {}
        switches = monitor.get("switches", 0)
        alarms = monitor["alarms"]
        return {
            "waste_container_present": not bool((switches >> 3) & 1),
            "waste_full_alarm": bool((alarms >> 1) & 1),
            "grounds_counter": _prop_int(
                self.coordinator.data, _GROUNDS_COUNTER_PROPERTY
            ),
        }


class DelonghiDecalcificationBinarySensor(_Base):
    """Decalcification needed (alarm bit or percentage threshold)."""

    def __init__(self, coord: DelonghiCoordinator) -> None:
        super().__init__(coord, "decalcification_needed", "mdi:coffee-maker")

    @property
    def is_on(self) -> bool | None:
        monitor = self._monitor()
        if "alarms" not in monitor:
            return None
        descale_alarm = bool((monitor["alarms"] >> 2) & 1)
        decalc_percent = _prop_int(self.coordinator.data, _DECALC_PERCENT_PROPERTY)
        return descale_alarm or decalc_percent >= 90

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        monitor = self._monitor()
        if "alarms" not in monitor:
            return {}
        return {
            "descale_alarm": bool((monitor["alarms"] >> 2) & 1),
            "decalc_percentage": _prop_int(
                self.coordinator.data, _DECALC_PERCENT_PROPERTY
            ),
        }


class DelonghiFilterBinarySensor(_Base):
    """Water filter replacement needed."""

    def __init__(self, coord: DelonghiCoordinator) -> None:
        super().__init__(coord, "filter_change_needed", "mdi:filter-variant")

    @property
    def is_on(self) -> bool | None:
        monitor = self._monitor()
        if "alarms" not in monitor:
            return None
        return bool((monitor["alarms"] >> 3) & 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        monitor = self._monitor()
        if "alarms" not in monitor:
            return {}
        return {
            "filter_alarm": bool((monitor["alarms"] >> 3) & 1),
        }


class DelonghiBeansEmptyBinarySensor(_Base):
    """Coffee beans empty (alarm bit 5, or bit 15 - see const.ALARM_NAMES)."""

    def __init__(self, coord: DelonghiCoordinator) -> None:
        super().__init__(coord, "beans_empty", "mdi:seed-off-outline")

    @property
    def is_on(self) -> bool | None:
        monitor = self._monitor()
        if "alarms" not in monitor:
            return None
        alarms = monitor["alarms"]
        return bool((alarms >> 5) & 1 or (alarms >> 15) & 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        monitor = self._monitor()
        if "alarms" not in monitor:
            return {}
        alarms = monitor["alarms"]
        return {
            "beans_empty_alarm": bool((alarms >> 5) & 1),
            "beans_empty_alarm_2": bool((alarms >> 15) & 1),
            "bean_hopper_absent": bool((alarms >> 17) & 1),
        }
