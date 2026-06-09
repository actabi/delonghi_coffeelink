"""Sensor platform for DeLonghi Coffee Link."""
from __future__ import annotations

import base64
import binascii
import json
import logging
from datetime import datetime
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
from .const import (
    APP_ID_PROPERTY,
    COUNTER_SENSORS,
    DOMAIN,
    ECAM_ONLY_COUNTER_KEYS,
    INFO_DIAGNOSTIC_KEYS,
    INFO_SENSORS,
    INTEGRATION_CLOUD_APP_ID,
    MANUFACTURER,
    PROPERTY_MEASUREMENT,
    PROPERTY_UNITS,
    PROPERTY_VALUE_SCALE,
)
from .coordinator import DelonghiCoordinator

_HA_CLOUD_SESSION_APP_ID = normalize_signed_app_id(INTEGRATION_CLOUD_APP_ID)
_SESSION_TS_MIN = 946684800  # 2000-01-01
_SESSION_TS_MAX = 4102444800  # 2100-01-01

_LOGGER = logging.getLogger(__name__)


def _parse_counter_value_legacy(val: Any) -> int | None:
    """Pre-PR Soul counter parser: plain integers only."""
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    try:
        return int(str(val).strip())
    except (TypeError, ValueError):
        return None


def _parse_counter_value(val: Any) -> int | None:
    """Parse an Ayla counter property value to int (ECAM may use floats/JSON)."""
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    if isinstance(val, dict):
        total = 0
        for item in val.values():
            parsed = _parse_counter_value(item)
            if parsed is not None:
                total += parsed
        return total
    text = str(val).strip()
    if not text:
        return 0
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            return _parse_counter_value(payload)
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return int(float(text))
    except ValueError:
        return None


def _resolve_property(data: dict[str, Any] | None, candidates: list[str]) -> str | None:
    """Return the first candidate property name present on the device, else None."""
    data = data or {}
    for candidate in candidates:
        if candidate in data:
            return candidate
    return None


def _parse_last_connected(value: Any) -> datetime | str | int | float | None:
    """Decode ECAM session blob (8-byte timestamp+app_id) or return value as-is."""
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    try:
        raw = base64.b64decode(value.strip(), validate=True)
        if len(raw) == 8:
            ts = int.from_bytes(raw[:4], "big")
            if _SESSION_TS_MIN <= ts <= _SESSION_TS_MAX:
                return dt_util.as_local(dt_util.utc_from_timestamp(ts))
    except (ValueError, binascii.Error):
        pass
    return value


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinators: list[DelonghiCoordinator] = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for coord in coordinators:
        ecam = coord.profile.uses_cloud_session
        for candidates, key, friendly, icon in COUNTER_SENSORS:
            if key in ECAM_ONLY_COUNTER_KEYS and not ecam:
                continue
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
            if key == "oem_model_info" and not ecam:
                continue
            if key == "last_connected":
                lc_candidates = (
                    ["app_device_connected", "device_connected"]
                    if ecam
                    else ["device_connected", "app_device_connected"]
                )
                prop_name = _resolve_property(coord.data, lc_candidates)
            else:
                prop_name = _resolve_property(coord.data, candidates)
            if prop_name is None:
                _LOGGER.debug(
                    "Skipping info sensor '%s' for dsn=%s: none of %s present",
                    key, coord.device.dsn, candidates,
                )
                continue
            if key == "last_connected" and ecam:
                entities.append(
                    DelonghiLastConnectedSensor(coord, prop_name, key, friendly, icon)
                )
            else:
                category = (
                    EntityCategory.DIAGNOSTIC
                    if key in INFO_DIAGNOSTIC_KEYS
                    else None
                )
                entities.append(
                    DelonghiInfoSensor(
                        coord, prop_name, key, friendly, icon, entity_category=category
                    )
                )
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
    """Counter sensor; TOTAL_INCREASING by default, MEASUREMENT for ECAM percentages."""

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
        self._ecam_parsing = coord.profile.uses_cloud_session
        if prop_name in PROPERTY_UNITS:
            self._attr_native_unit_of_measurement = PROPERTY_UNITS[prop_name]
        if prop_name in PROPERTY_MEASUREMENT:
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int | float | None:
        prop = (self.coordinator.data or {}).get(self._prop_name)
        if not prop:
            return None
        val = prop.get("value")
        if self._ecam_parsing:
            numeric = _parse_counter_value(val)
        else:
            numeric = _parse_counter_value_legacy(val)
        if numeric is None:
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
        scale = PROPERTY_VALUE_SCALE.get(self._prop_name)
        if scale:
            return round(numeric / scale, 1)
        return numeric


class DelonghiInfoSensor(_Base):
    """Generic info sensor (version string, serial, etc.)."""

    def __init__(
        self,
        coord: DelonghiCoordinator,
        prop_name: str,
        key: str,
        friendly: str,
        icon: str,
        *,
        entity_category: EntityCategory | None = None,
    ) -> None:
        super().__init__(coord, key, friendly, icon)
        self._prop_name = prop_name
        if entity_category is not None:
            self._attr_entity_category = entity_category

    @property
    def native_value(self) -> Any:
        prop = (self.coordinator.data or {}).get(self._prop_name)
        if not prop:
            return None
        return prop.get("value")


class DelonghiLastConnectedSensor(_Base):
    """Last connect time for ECAM models (8-byte app_device_connected session blob)."""

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

    def _raw_value(self) -> Any:
        prop = (self.coordinator.data or {}).get(self._prop_name)
        if not prop:
            return None
        return prop.get("value")

    @property
    def native_value(self) -> datetime | str | int | float | None:
        return _parse_last_connected(self._raw_value())

    @property
    def device_class(self) -> SensorDeviceClass | None:
        parsed = _parse_last_connected(self._raw_value())
        if isinstance(parsed, datetime):
            return SensorDeviceClass.TIMESTAMP
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        raw = self._raw_value()
        if not isinstance(raw, str):
            return {}
        try:
            blob = base64.b64decode(raw.strip(), validate=True)
            if len(blob) != 8:
                return {}
            ts = int.from_bytes(blob[:4], "big")
            if not (_SESSION_TS_MIN <= ts <= _SESSION_TS_MAX):
                return {}
            app_id = int.from_bytes(blob[4:8], "big", signed=True)
        except (ValueError, binascii.Error):
            return {}
        return {
            "connected_app_id": app_id,
            "connected_app_id_hex": f"{app_id & 0xFFFFFFFF:08x}",
        }


class DelonghiConnectionSensor(_Base):
    """Exposes Ayla connection status (Online / Offline)."""

    def __init__(self, coord: DelonghiCoordinator) -> None:
        super().__init__(coord, "connection_status", "Connection Status", "mdi:cloud")

    @property
    def native_value(self) -> str:
        return self.coordinator.device.connection_status


class DelonghiMachineStatusSensor(_Base):
    """Machine operational state decoded from ``d302_monitor_machine``."""

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


def _cloud_session_holder(app_id: int | None) -> str:
    if app_id is None:
        return "unknown"
    if app_id == 0:
        return "free"
    if app_id == _HA_CLOUD_SESSION_APP_ID:
        return "ha"
    return "foreign"


class DelonghiCloudSessionAppIdSensor(_Base):
    """Diagnostic: machine property ``app_id`` (current cloud session holder)."""

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
    """Diagnostic: last command seen on the binary channel."""

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
