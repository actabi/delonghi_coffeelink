"""DataUpdateCoordinator for DeLonghi Coffee Link."""
from __future__ import annotations

import logging
from collections import deque
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ayla_client import AylaDevice, CloudError, DelonghiAylaClient
from .command_builder import (
    builder_structural_b64,
    decode_command,
    deserialize_learned_frames,
    is_valid_wake_frame,
    recipe_dump_lines,
    serialize_learned_frames,
    summarize_decoded,
)
from .const import (
    ACTION_STOP,
    COMMAND_PROPERTY_CANDIDATES,
    CONNECTED_PROPERTY_CANDIDATES,
    DEFAULT_CLOUD_APP_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MONITOR_PROPERTY,
    POWER_WAKE_PARAMS,
    RECIPE_STORE_SAVE_DELAY,
    RECIPE_STORE_VERSION,
    RESPONSE_PROPERTY_CANDIDATES,
)
from .model_profiles import profile_for
from .monitor import parse_monitor_b64

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
        self.profile = profile_for(device.oem_model)
        self.command_property: str | None = None
        self.response_property: str | None = None
        self.connected_property: str | None = None
        self._cloud_app_id: int = DEFAULT_CLOUD_APP_ID
        self._last_connect_at: float = 0.0
        self._sent_values: deque[str] = deque(maxlen=32)
        self._last_cmd_marker: Any = None
        self._last_resp_marker: Any = None
        self.last_captured_command: dict | None = None
        self.last_machine_response: dict | None = None
        self.monitor: dict[str, Any] = {}
        self.learned_start_frames: dict[int, str] = {}
        self.learned_stop_frames: dict[int, str] = {}
        self.learned_wake_frame: str | None = None
        self._store: Store = Store(
            hass, RECIPE_STORE_VERSION, f"{DOMAIN}_recipes_{device.dsn}"
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch all properties + refresh device meta."""
        try:
            props = await self.client.async_get_properties(self.device.dsn)
            if self.command_property is None:
                self.command_property = self._detect_property(
                    props, COMMAND_PROPERTY_CANDIDATES, "command"
                )
                self.profile = profile_for(self.device.oem_model, self.command_property)
            if self.response_property is None:
                self.response_property = self._detect_property(
                    props, RESPONSE_PROPERTY_CANDIDATES, "response", required=False
                )
            if self.connected_property is None and self.profile.uses_cloud_session:
                self.connected_property = self._detect_property(
                    props, CONNECTED_PROPERTY_CANDIDATES, "connected", required=False
                )
            self._update_monitor(props)
            self._sniff_app_traffic(props)
            devices = await self.client.async_get_devices()
            for d in devices:
                if d.dsn == self.device.dsn:
                    self.device = d
                    break
            props["_monitor"] = self.monitor
            return props
        except Exception as err:
            raise UpdateFailed(f"Error fetching Delonghi data: {err}") from err

    def _update_monitor(self, props: dict[str, Any]) -> None:
        prop = props.get(MONITOR_PROPERTY)
        if not isinstance(prop, dict):
            self.monitor = {}
            return
        value = prop.get("value")
        if isinstance(value, str) and value.strip():
            self.monitor = parse_monitor_b64(value)
        else:
            self.monitor = {}

    def _detect_property(
        self,
        props: dict[str, Any],
        candidates: list[str],
        kind: str,
        required: bool = True,
    ) -> str | None:
        for candidate in candidates:
            if candidate in props:
                _LOGGER.info(
                    "Using %s property '%s' for dsn=%s (oem_model=%s)",
                    kind,
                    candidate,
                    self.device.dsn,
                    self.device.oem_model,
                )
                return candidate
        if not required:
            _LOGGER.debug(
                "No %s property among %s for dsn=%s",
                kind,
                candidates,
                self.device.dsn,
            )
            return None
        raise CloudError(
            f"No known {kind} property found for dsn={self.device.dsn} "
            f"(oem_model={self.device.oem_model}). Tried {candidates}. "
            "Please open an issue with debug logs."
        )

    def _sniff_app_traffic(self, props: dict[str, Any]) -> None:
        try:
            if self.command_property:
                self._capture_channel(props, self.command_property, channel="command")
            if self.response_property:
                self._capture_channel(props, self.response_property, channel="response")
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Command sniffer failed (non-fatal)", exc_info=True)

    def _capture_channel(
        self, props: dict[str, Any], prop_name: str, channel: str
    ) -> None:
        prop = props.get(prop_name)
        if not isinstance(prop, dict):
            return
        value = prop.get("value")
        if not isinstance(value, str) or not value.strip():
            return
        value = value.strip()
        marker = prop.get("data_updated_at", value)
        marker_attr = "_last_cmd_marker" if channel == "command" else "_last_resp_marker"
        previous = getattr(self, marker_attr)
        if marker == previous:
            return
        first_observation = previous is None
        setattr(self, marker_attr, marker)
        if first_observation:
            return

        decoded = decode_command(value)
        if channel == "command":
            origin = "integration" if value in self._sent_values else "app"
            decoded["origin"] = origin
            decoded["captured_at"] = prop.get("data_updated_at")
            structural = builder_structural_b64(decoded)
            if structural is not None and "structural_b64" in decoded:
                decoded["matches_integration"] = decoded["structural_b64"] == structural
                decoded["builder_structural_b64"] = structural
            self.last_captured_command = decoded
            if origin == "app":
                self._maybe_learn_frame(decoded)
            summary = summarize_decoded(decoded)
            if origin == "app":
                _LOGGER.warning(
                    "CAPTURED app->machine command on %s (dsn=%s): %s | %s",
                    prop_name,
                    self.device.dsn,
                    value,
                    summary,
                )
            else:
                _LOGGER.debug(
                    "Observed own command echoed on %s: %s | %s",
                    prop_name,
                    value,
                    summary,
                )
        else:
            decoded["captured_at"] = prop.get("data_updated_at")
            self.last_machine_response = decoded
            _LOGGER.debug(
                "Machine->app response on %s (dsn=%s): %s | %s",
                prop_name,
                self.device.dsn,
                value,
                summarize_decoded(decoded),
            )

    def _record_sent(self, value: str) -> None:
        self._sent_values.append(value)

    async def _ensure_cloud_session(self) -> None:
        if not self.profile.uses_cloud_session:
            return
        if not self.connected_property:
            _LOGGER.debug(
                "No connected property for dsn=%s; skipping cloud session setup",
                self.device.dsn,
            )
            return
        self._cloud_app_id, self._last_connect_at = (
            await self.client.async_ensure_device_connected(
                self.device.dsn,
                self.connected_property,
                self._cloud_app_id,
                self._last_connect_at,
            )
        )

    async def async_load_learned(self) -> None:
        try:
            data = await self._store.async_load()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not load learned recipes (non-fatal)", exc_info=True)
            return
        if not data:
            return
        (
            self.learned_start_frames,
            self.learned_stop_frames,
            self.learned_wake_frame,
        ) = deserialize_learned_frames(data)
        if self.learned_wake_frame and not is_valid_wake_frame(self.learned_wake_frame):
            _LOGGER.warning(
                "Discarding invalid learned wake frame for dsn=%s (expected params "
                "%s, got refresh or corrupt data)",
                self.device.dsn,
                POWER_WAKE_PARAMS.hex(" "),
            )
            self.learned_wake_frame = None
        total = (
            len(self.learned_start_frames)
            + len(self.learned_stop_frames)
            + (1 if self.learned_wake_frame else 0)
        )
        if total:
            _LOGGER.debug(
                "Restored %d learned Eletta frame(s) for dsn=%s", total, self.device.dsn
            )

    def log_recipe_datapoints(self) -> None:
        if not self.data:
            _LOGGER.warning("Recipe dump requested but no data fetched yet.")
            return
        lines = recipe_dump_lines(self.data)
        _LOGGER.warning(
            "=== DeLonghi recipe datapoint dump (dsn=%s, %d entries) BEGIN ===\n"
            "%s\n=== recipe datapoint dump END ===",
            self.device.dsn,
            len(lines),
            "\n".join(lines),
        )

    def _learned_storage_data(self) -> dict:
        return serialize_learned_frames(
            self.learned_start_frames, self.learned_stop_frames, self.learned_wake_frame
        )

    def _maybe_learn_frame(self, decoded: dict) -> None:
        if not self.profile.learns_from_app:
            return
        raw_b64 = decoded.get("raw_b64")
        if not raw_b64:
            return
        ftype = decoded.get("type")

        if ftype == "power":
            if decoded.get("params") != POWER_WAKE_PARAMS.hex(" "):
                _LOGGER.debug(
                    "Ignoring power frame params=%s (not wake %s)",
                    decoded.get("params"),
                    POWER_WAKE_PARAMS.hex(" "),
                )
                return
            if self.learned_wake_frame != raw_b64:
                self.learned_wake_frame = raw_b64
                _LOGGER.info("Learned %s wake/power-on frame: %s", self.profile.key, raw_b64)
                self._store.async_delay_save(
                    self._learned_storage_data, RECIPE_STORE_SAVE_DELAY
                )
            return

        if ftype != "beverage" or decoded.get("style") != "eletta":
            return
        bev_hex = decoded.get("beverage_id")
        if not bev_hex:
            return
        try:
            bev_id = int(bev_hex, 16)
        except (ValueError, TypeError):
            return
        table = (
            self.learned_stop_frames
            if decoded.get("action") == ACTION_STOP
            else self.learned_start_frames
        )
        if table.get(bev_id) != raw_b64:
            table[bev_id] = raw_b64
            _LOGGER.info(
                "Learned %s %s frame for beverage 0x%02x (%s): %s",
                self.profile.key,
                "stop" if decoded.get("action") == ACTION_STOP else "start",
                bev_id,
                decoded.get("beverage_name"),
                raw_b64,
            )
            self._store.async_delay_save(
                self._learned_storage_data, RECIPE_STORE_SAVE_DELAY
            )

    async def async_send_beverage(self, beverage_id: int, action: int) -> None:
        from .command_builder import build_and_encode

        table = self.learned_stop_frames if action == ACTION_STOP else self.learned_start_frames
        learned = table.get(beverage_id)
        await self._ensure_cloud_session()
        value = self.profile.beverage_value(
            beverage_id,
            action,
            learned,
            cloud_app_id=self._cloud_app_id,
        )
        if value is None:
            value = build_and_encode(beverage_id, action)
            _LOGGER.warning(
                "No learned %s frame for beverage 0x%02x yet (%s). Trigger this "
                "drink once from the official Coffee Link app so Home Assistant "
                "can capture and replay its exact bytes. Sending a best-effort "
                "frame meanwhile (the machine will likely ignore it).",
                "stop" if action == ACTION_STOP else "start",
                beverage_id,
                self.profile.label,
            )
        else:
            _LOGGER.info(
                "Sending %s beverage 0x%02x (%s): %s",
                self.profile.key,
                beverage_id,
                "stop" if action == ACTION_STOP else "start",
                value,
            )

        self._record_sent(value)
        prop = self.command_property or COMMAND_PROPERTY_CANDIDATES[0]
        await self.client.async_set_property_value(self.device.dsn, prop, value)
        await self.async_request_refresh()

    async def async_send_wake(self) -> None:
        await self._ensure_cloud_session()
        value = self.profile.wake_value(
            self.learned_wake_frame,
            cloud_app_id=self._cloud_app_id,
        )
        if value is None:
            _LOGGER.error("Profile %s returned no wake command", self.profile.key)
            return
        self._record_sent(value)
        prop = self.command_property or COMMAND_PROPERTY_CANDIDATES[0]
        _LOGGER.info("Sending WAKE cmd via %s: %s", prop, value)
        await self.client.async_set_property_value(self.device.dsn, prop, value)
        await self.async_request_refresh()

    async def async_send_standby(self) -> None:
        await self._ensure_cloud_session()
        value = self.profile.standby_value(cloud_app_id=self._cloud_app_id)
        if value is None:
            _LOGGER.error("Profile %s does not support standby", self.profile.key)
            return
        self._record_sent(value)
        prop = self.command_property or COMMAND_PROPERTY_CANDIDATES[0]
        _LOGGER.info("Sending STANDBY cmd via %s: %s", prop, value)
        await self.client.async_set_property_value(self.device.dsn, prop, value)
        await self.async_request_refresh()

    async def async_send_raw(self, value: str) -> None:
        await self._ensure_cloud_session()
        self._record_sent(value)
        prop = self.command_property or COMMAND_PROPERTY_CANDIDATES[0]
        await self.client.async_set_property_value(self.device.dsn, prop, value)
        await self.async_request_refresh()
