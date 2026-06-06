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
    serialize_learned_frames,
    summarize_decoded,
)
from .const import (
    ACTION_STOP,
    COMMAND_PROPERTY_CANDIDATES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ELETTA_OEM_PREFIX,
    RECIPE_STORE_SAVE_DELAY,
    RECIPE_STORE_VERSION,
    RESPONSE_PROPERTY_CANDIDATES,
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
        self.response_property: str | None = None
        # --- Command sniffer state ---------------------------------------
        # Values WE wrote, so a command echoed back by the cloud is not
        # mis-attributed to the official app. Bounded; only recent writes matter.
        self._sent_values: deque[str] = deque(maxlen=32)
        # Last datapoint marker seen per channel, to detect *new* writes only.
        self._last_cmd_marker: Any = None
        self._last_resp_marker: Any = None
        # Last decoded frames, surfaced via the diagnostic sensor.
        self.last_captured_command: dict | None = None
        self.last_machine_response: dict | None = None
        # Eletta (DL-striker-cb) frame replay: the Soul-style fixed recipe is
        # ignored by Eletta machines, which expect a variable-length recipe block
        # (and a different "start" action byte, plus a device signature). Rather
        # than rebuild all that, we learn the exact frame the official app sends
        # per beverage (sniffed below) and replay it verbatim with only a fresh
        # timestamp. Keyed by beverage_id; start and stop frames kept separately.
        # Persisted to disk so the learning survives Home Assistant restarts.
        self.learned_start_frames: dict[int, str] = {}
        self.learned_stop_frames: dict[int, str] = {}
        # Power-on (wake) is a single frame. The official app appends a 4-byte
        # device signature the integration's synthesized wake lacks - which is
        # why a built wake is ignored while a verbatim app replay works - so we
        # learn and replay the app's power-on frame too.
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
            if self.response_property is None:
                # Optional: absence is fine, the sniffer just skips responses.
                self.response_property = self._detect_property(
                    props, RESPONSE_PROPERTY_CANDIDATES, "response", required=False
                )
            self._sniff_app_traffic(props)
            # Refresh device connection status
            devices = await self.client.async_get_devices()
            for d in devices:
                if d.dsn == self.device.dsn:
                    self.device = d
                    break
            return props
        except Exception as err:
            raise UpdateFailed(f"Error fetching Delonghi data: {err}") from err

    def _detect_property(
        self,
        props: dict[str, Any],
        candidates: list[str],
        kind: str,
        required: bool = True,
    ) -> str | None:
        """Pick the right property name for this model from a candidate list.

        Different DeLonghi models expose the binary channels under different
        names (e.g. ``data_request`` on Soul vs ``app_data_request`` on Eletta).
        """
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
                "No %s property among %s for dsn=%s (sniffer will skip it)",
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

    # ------------------------------------------------------------------ #
    # Command sniffer
    #
    # We already fetch every property each poll, so watching the command and
    # response channels is free (no extra API calls). When the value changes to
    # something this integration did not write, it was written by the official
    # Coffee Link app - i.e. the ground-truth bytes we need to compare against.
    # ------------------------------------------------------------------ #

    def _sniff_app_traffic(self, props: dict[str, Any]) -> None:
        # The sniffer is a diagnostic; it must never break the data update and
        # take the device unavailable. Swallow and log any unexpected error.
        try:
            if self.command_property:
                self._capture_channel(props, self.command_property, channel="command")
            if self.response_property:
                self._capture_channel(props, self.response_property, channel="response")
        except Exception:  # noqa: BLE001 - diagnostic must not break polling
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
        # Ayla wraps string datapoints in whitespace (e.g. a trailing newline);
        # normalise so attribution against _sent_values and the decode succeed.
        value = value.strip()
        # Prefer the cloud's datapoint timestamp to detect a new write (it also
        # catches the app re-sending byte-identical bytes); fall back to value.
        marker = prop.get("data_updated_at", value)
        marker_attr = "_last_cmd_marker" if channel == "command" else "_last_resp_marker"
        previous = getattr(self, marker_attr)
        if marker == previous:
            return  # nothing new this poll
        first_observation = previous is None
        setattr(self, marker_attr, marker)
        if first_observation:
            # The value already present at startup is not a fresh capture.
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
                    prop_name, self.device.dsn, value, summary,
                )
            else:
                _LOGGER.debug(
                    "Observed own command echoed on %s: %s | %s",
                    prop_name, value, summary,
                )
        else:
            decoded["captured_at"] = prop.get("data_updated_at")
            self.last_machine_response = decoded
            _LOGGER.debug(
                "Machine->app response on %s (dsn=%s): %s | %s",
                prop_name, self.device.dsn, value, summarize_decoded(decoded),
            )

    def _record_sent(self, value: str) -> None:
        """Remember a value we wrote so the sniffer won't flag it as app traffic."""
        self._sent_values.append(value)

    @property
    def is_eletta(self) -> bool:
        """True for the Eletta Explore family (variable-length recipe frames).

        Detected by oem_model, with the resolved command property as a fallback
        (Eletta uses ``app_data_request``, Soul uses ``data_request``).
        """
        oem = (self.device.oem_model or "")
        if oem.startswith(ELETTA_OEM_PREFIX):
            return True
        return self.command_property == "app_data_request"

    async def async_load_learned(self) -> None:
        """Load learned Eletta frames persisted from previous runs.

        Called once at setup so a restart does not lose the per-beverage frames
        the integration learned from the official app.
        """
        try:
            data = await self._store.async_load()
        except Exception:  # noqa: BLE001 - persistence must not block setup
            _LOGGER.debug("Could not load learned recipes (non-fatal)", exc_info=True)
            return
        if not data:
            return
        (
            self.learned_start_frames,
            self.learned_stop_frames,
            self.learned_wake_frame,
        ) = deserialize_learned_frames(data)
        total = (
            len(self.learned_start_frames)
            + len(self.learned_stop_frames)
            + (1 if self.learned_wake_frame else 0)
        )
        if total:
            _LOGGER.debug(
                "Restored %d learned Eletta frame(s) for dsn=%s", total, self.device.dsn
            )

    def _learned_storage_data(self) -> dict:
        """Callback for the debounced Store save."""
        return serialize_learned_frames(
            self.learned_start_frames, self.learned_stop_frames, self.learned_wake_frame
        )

    def _maybe_learn_frame(self, decoded: dict) -> None:
        """Learn the exact frame the official app sent for a beverage.

        Eletta machines ignore the Soul-style fixed recipe; replaying the app's
        own frame verbatim is the reliable way to reproduce a beverage (quantity
        / intensity / milk, the right start-action byte, and the device signature
        are all preserved). Stop frames (action 0x02) are kept separately from
        start frames so a captured stop never gets replayed for a start press.
        The power-on (wake) frame is learned too - the app appends a device
        signature a synthesized wake lacks. New/changed frames are persisted
        (debounced) so they survive restarts.
        """
        if not self.is_eletta:
            return
        raw_b64 = decoded.get("raw_b64")
        if not raw_b64:
            return
        ftype = decoded.get("type")

        if ftype == "power":
            if self.learned_wake_frame != raw_b64:
                self.learned_wake_frame = raw_b64
                _LOGGER.info("Learned Eletta wake/power-on frame: %s", raw_b64)
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
                "Learned Eletta %s frame for beverage 0x%02x (%s): %s",
                "stop" if decoded.get("action") == ACTION_STOP else "start",
                bev_id,
                decoded.get("beverage_name"),
                raw_b64,
            )
            self._store.async_delay_save(
                self._learned_storage_data, RECIPE_STORE_SAVE_DELAY
            )

    async def async_send_beverage(self, beverage_id: int, action: int) -> None:
        """Build + send a beverage command via the resolved command property."""
        from .command_builder import build_and_encode, replay_with_timestamp

        if self.is_eletta:
            table = self.learned_stop_frames if action == ACTION_STOP else self.learned_start_frames
            learned = table.get(beverage_id)
            if learned is not None:
                value = replay_with_timestamp(learned)
                _LOGGER.info(
                    "Sending Eletta beverage 0x%02x (%s) via learned frame replay: %s",
                    beverage_id,
                    "stop" if action == ACTION_STOP else "start",
                    value,
                )
            else:
                # No capture yet: the Soul frame is known not to work on Eletta,
                # but send it best-effort so the call doesn't fail, and tell the
                # user how to teach the integration the right bytes.
                value = build_and_encode(beverage_id, action)
                _LOGGER.warning(
                    "No learned %s frame for Eletta beverage 0x%02x yet. Trigger "
                    "this drink once from the official Coffee Link app so Home "
                    "Assistant can capture and replay its exact bytes. Sending a "
                    "best-effort Soul frame meanwhile (the machine will likely "
                    "ignore it).",
                    "stop" if action == ACTION_STOP else "start",
                    beverage_id,
                )
        else:
            value = build_and_encode(beverage_id, action)

        self._record_sent(value)
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
        from .command_builder import build_wake_encoded, replay_with_timestamp

        if self.is_eletta and self.learned_wake_frame is not None:
            # The synthesized wake is ignored by Eletta (it lacks the 4-byte
            # device signature the app appends); replay the app's captured
            # power-on frame verbatim with a fresh timestamp instead.
            value = replay_with_timestamp(self.learned_wake_frame)
            _LOGGER.info("Sending Eletta wake via learned frame replay: %s", value)
        else:
            value = build_wake_encoded()
            if self.is_eletta:
                _LOGGER.warning(
                    "No learned wake frame for this Eletta yet. Power the machine "
                    "on once from the official Coffee Link app so Home Assistant "
                    "can capture and replay it. Sending a best-effort synthesized "
                    "wake meanwhile (the machine will likely ignore it - it lacks "
                    "the device signature the app appends)."
                )
        self._record_sent(value)
        prop = self.command_property or COMMAND_PROPERTY_CANDIDATES[0]
        _LOGGER.info("Sending WAKE cmd via %s: %s", prop, value)
        await self.client.async_set_property_value(self.device.dsn, prop, value)
        await self.async_request_refresh()
