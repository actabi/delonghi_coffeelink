"""Button platform for DeLonghi Coffee Link - one button per beverage."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ACTION_START,
    ACTION_STOP,
    BEVERAGES,
    DOMAIN,
    MANUFACTURER,
    PROJECT_URL,
)
from .coordinator import DelonghiCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinators: list[DelonghiCoordinator] = hass.data[DOMAIN][entry.entry_id]
    entities: list[ButtonEntity] = []
    for coord in coordinators:
        entities.append(DelonghiWakeButton(coord))
        entities.append(DelonghiStandbyButton(coord))
        for bev_id, key, friendly, icon in BEVERAGES:
            entities.append(DelonghiStartBeverageButton(coord, bev_id, key, friendly, icon))
        entities.append(DelonghiStopButton(coord))
        entities.append(DelonghiDumpRecipesButton(coord))
        # One button per profile the machine's display offers; a machine that
        # offers none simply gets none.
        for slot, label in coord.user_profile_labels().items():
            entities.append(DelonghiSetProfileButton(coord, slot, label))
    async_add_entities(entities)


class _Base(CoordinatorEntity[DelonghiCoordinator], ButtonEntity):
    _attr_has_entity_name = True

    @property
    def available(self) -> bool:
        """Buttons stay available across a failed poll - on purpose.

        A button has no state of its own to lose: its state is the timestamp of
        the last press, and `unknown` until one happens. Letting the coordinator
        take it unavailable therefore buys no information, and costs a real bug.
        The logbook labels *every* state change of a `button` entity "Pressed"
        (frontend `src/data/logbook.ts`, STATE_ACTION_MESSAGES), so the trip
        through `unavailable` and back after any cloud hiccup was rendered as
        every button on the machine being pressed in the same second, and fired
        every `state` trigger watching them. Observed on a single Ayla 504.

        Refusing a command to a machine that cannot receive it is the
        coordinator preflight's job (`_ensure_machine_reachable`), which raises
        with an error the user actually sees. That check reads the machine's
        connection status, not our poll health, so nothing is lost by keeping
        the buttons pressable here.
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


class DelonghiStartBeverageButton(_Base):
    """Press to START a specific beverage."""

    def __init__(
        self,
        coord: DelonghiCoordinator,
        bev_id: int,
        key: str,
        friendly: str,
        icon: str,
    ) -> None:
        super().__init__(coord)
        self._bev_id = bev_id
        self._attr_unique_id = f"{coord.device.dsn}_start_{key}"
        self._attr_translation_key = f"start_{key}"
        self._attr_icon = icon

    async def async_press(self) -> None:
        _LOGGER.info("Start beverage 0x%02x (%s)", self._bev_id, self.name)
        await self.coordinator.async_send_beverage(self._bev_id, ACTION_START)


class DelonghiWakeButton(_Base):
    """Wake the machine from standby (captured cmd family 0x84 0x0f)."""

    def __init__(self, coord: DelonghiCoordinator) -> None:
        super().__init__(coord)
        self._attr_unique_id = f"{coord.device.dsn}_wake"
        self._attr_translation_key = "wake"
        self._attr_icon = "mdi:power"

    async def async_press(self) -> None:
        _LOGGER.info("Sending WAKE to machine")
        await self.coordinator.async_send_wake()


class DelonghiStandbyButton(_Base):
    """Put the machine in standby / power it off (cmd family 0x84 0x0f, params 01 01).

    Same effect as pressing the physical power button. Validated live on the
    PrimaDonna Soul; on Eletta-style models the learned device signature is
    appended (see coordinator.async_send_standby).
    """

    def __init__(self, coord: DelonghiCoordinator) -> None:
        super().__init__(coord)
        self._attr_unique_id = f"{coord.device.dsn}_standby"
        self._attr_translation_key = "standby"
        self._attr_icon = "mdi:power-standby"

    async def async_press(self) -> None:
        _LOGGER.info("Sending STANDBY to machine")
        await self.coordinator.async_send_standby()


class DelonghiStopButton(_Base):
    """Press to STOP currently-running beverage (uses hot_water id + stop action as generic)."""

    def __init__(self, coord: DelonghiCoordinator) -> None:
        super().__init__(coord)
        self._attr_unique_id = f"{coord.device.dsn}_stop"
        self._attr_translation_key = "stop"
        self._attr_icon = "mdi:stop"

    async def async_press(self) -> None:
        # NOTE: the exact beverage_id used to stop may matter; using 0x10 (hot water)
        # since that's the captured example. If machine needs the running beverage id,
        # a future version can track current bev and stop it appropriately.
        _LOGGER.info("Generic stop command")
        await self.coordinator.async_send_beverage(0x10, ACTION_STOP)


class DelonghiDumpRecipesButton(_Base):
    """Diagnostic: log the machine's stored recipe datapoints (read-only).

    Sends nothing to the machine - only dumps the recipe definitions it already
    reports, so the recipe->command mapping can be confirmed (zero-touch work).
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: DelonghiCoordinator) -> None:
        super().__init__(coord)
        self._attr_unique_id = f"{coord.device.dsn}_dump_recipes"
        self._attr_translation_key = "dump_recipes"
        self._attr_icon = "mdi:bug-outline"

    async def async_press(self) -> None:
        self.coordinator.log_recipe_datapoints()


class DelonghiSetProfileButton(_Base):
    """Switch the machine to one user profile (a9 f0), one button per profile.

    A button and deliberately not a select: a profile changed on the machine's
    own panel produces no cloud traffic at all, so nothing here may claim to
    know which profile is active - a select would show a value that can be
    silently wrong for hours. A button claims nothing and is always pressable.
    """

    def __init__(self, coord: DelonghiCoordinator, slot: int, label: str) -> None:
        super().__init__(coord)
        self._slot = slot
        self._attr_unique_id = f"{coord.device.dsn}_set_profile_{slot}"
        self._attr_translation_key = "set_profile"
        self._attr_translation_placeholders = {"profile": label}
        self._attr_icon = "mdi:account"
        self._attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        _LOGGER.info("Switching to user profile %d (%s)", self._slot, self.name)
        await self.coordinator.async_send_profile(self._slot)
