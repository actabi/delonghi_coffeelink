"""Event platform for DeLonghi Coffee Link: one machine event entity per device.

Fires ``status_changed``, ``alarm_raised``, ``alarm_cleared`` and
``beverage_dispensed`` by comparing successive polls (see machine_events.py).
Resolution is the poll: the Soul publishes its status only when prompted, about
every 15-30 s, so a short state the machine passes through between two polls is
never seen.
"""
from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, EVENT_TYPES, MANUFACTURER, PROJECT_URL
from .coordinator import DelonghiCoordinator
from .machine_events import Snapshot, diff, merge, take_snapshot


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinators: list[DelonghiCoordinator] = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(DelonghiMachineEvent(coord) for coord in coordinators)


class DelonghiMachineEvent(CoordinatorEntity[DelonghiCoordinator], EventEntity):
    """Machine events derived from the polled cloud state."""

    _attr_has_entity_name = True
    _attr_translation_key = "machine_event"
    _attr_icon = "mdi:coffee-maker-outline"
    _attr_event_types = list(EVENT_TYPES)

    def __init__(self, coord: DelonghiCoordinator) -> None:
        super().__init__(coord)
        self._attr_unique_id = f"{coord.device.dsn}_machine_event"
        self._last: Snapshot | None = None

    @property
    def available(self) -> bool:
        """Always available, for the reason button.py spells out.

        An event entity's state is the time of its last event. Following the
        coordinator would send it through `unavailable` and back on every
        tolerated cloud hiccup, and each return would look like a fresh state
        change to a `state` trigger. No poll means no events, which is honest.
        """
        return True

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

    def _snapshot(self) -> Snapshot:
        coord = self.coordinator
        return take_snapshot(coord.data, coord.monitor, stale=coord.monitor_is_stale)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Baseline: what is already true at startup is not an event.
        self._last = self._snapshot()

    @callback
    def _handle_coordinator_update(self) -> None:
        current = self._snapshot()
        events = diff(self._last, current)
        self._last = merge(self._last, current)
        for event_type, attributes in events:
            self._trigger_event(event_type, attributes)
            # One state write per event: the state holds only the latest one,
            # so batching would drop all but the last of the same poll.
            self.async_write_ha_state()
