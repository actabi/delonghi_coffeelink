"""DataUpdateCoordinator for DeLonghi Coffee Link."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ayla_client import AylaDevice, CloudError, DelonghiAylaClient
from .const import (
    COMMAND_PROPERTY_CANDIDATES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class DelonghiCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Periodically fetch device properties from Ayla cloud."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: DelonghiAylaClient,
        device: AylaDevice,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{device.dsn}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client
        self.device = device
        self.command_property: str | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch all properties + refresh device meta."""
        try:
            props = await self.client.async_get_properties(self.device.dsn)
            if self.command_property is None:
                self.command_property = self._detect_command_property(props)
            # Refresh device connection status
            devices = await self.client.async_get_devices()
            for d in devices:
                if d.dsn == self.device.dsn:
                    self.device = d
                    break
            return props
        except Exception as err:
            raise UpdateFailed(f"Error fetching Delonghi data: {err}") from err

    def _detect_command_property(self, props: dict[str, Any]) -> str:
        """Pick the right command property for this model.

        Different DeLonghi models expose the binary command channel under
        different names (see const.COMMAND_PROPERTY_CANDIDATES).
        """
        for candidate in COMMAND_PROPERTY_CANDIDATES:
            if candidate in props:
                _LOGGER.info(
                    "Using command property '%s' for dsn=%s (oem_model=%s)",
                    candidate,
                    self.device.dsn,
                    self.device.oem_model,
                )
                return candidate
        raise CloudError(
            f"No known command property found for dsn={self.device.dsn} "
            f"(oem_model={self.device.oem_model}). Tried {COMMAND_PROPERTY_CANDIDATES}. "
            "Please open an issue with debug logs."
        )

    async def async_send_beverage(self, beverage_id: int, action: int) -> None:
        """Build + send a beverage command via the resolved command property."""
        from .command_builder import build_and_encode

        value = build_and_encode(beverage_id, action)
        prop = self.command_property or COMMAND_PROPERTY_CANDIDATES[0]
        _LOGGER.info(
            "Sending beverage cmd via %s: bev_id=0x%02x action=%d value=%s",
            prop,
            beverage_id,
            action,
            value,
        )
        await self.client.async_set_property_value(self.device.dsn, prop, value)
        await self.async_request_refresh()

    async def async_send_wake(self) -> None:
        """Send the WAKE / power-on command to bring the machine out of standby."""
        from .command_builder import build_wake_encoded

        value = build_wake_encoded()
        prop = self.command_property or COMMAND_PROPERTY_CANDIDATES[0]
        _LOGGER.info("Sending WAKE cmd via %s: %s", prop, value)
        await self.client.async_set_property_value(self.device.dsn, prop, value)
        await self.async_request_refresh()
