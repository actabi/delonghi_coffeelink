"""DataUpdateCoordinator for DeLonghi Coffee Link."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ayla_client import AylaDevice, CloudError, DelonghiAylaClient, normalize_signed_app_id
from .command_builder import (
    builder_structural_b64,
    decode_command,
    deserialize_learned_frames,
    is_wake_power_frame,
    recipe_dump_lines,
    serialize_learned_frames,
    summarize_decoded,
)
from .const import (
    ACTION_STOP,
    APP_ID_PROPERTY,
    COMMAND_PROPERTY_CANDIDATES,
    CONNECT_REFRESH_INTERVAL,
    CONNECT_SETTLE_DELAY,
    CONNECTED_PROPERTY_CANDIDATES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    INTEGRATION_CLOUD_APP_ID,
    MONITOR_PROPERTY,
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
        # Per-model behaviour (synthesize vs learn-and-replay). All model-specific
        # differences live in model_profiles.py; this object is the single source.
        self.profile = profile_for(device.oem_model)
        self.command_property: str | None = None
        self.response_property: str | None = None
        self.connected_property: str | None = None
        # Cloud session (ECAM / app_device_connected) — DlghIoT-compatible cache.
        self._integration_app_id = normalize_signed_app_id(INTEGRATION_CLOUD_APP_ID)
        self._last_connect_at: float = 0
        self._session_confirmed = False
        self._session_connect_lock = asyncio.Lock()
        self._session_refresh_task: asyncio.Task[None] | None = None
        self._session_cold_task: asyncio.Task[None] | None = None
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
        # Decoded d302_monitor_machine state (standby/ready/...), surfaced via
        # the Machine Status sensor. Empty dict until a blob parses.
        self.monitor: dict[str, Any] = {}
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
                # Refine the model profile now the live command channel is known
                # (only matters for an unrecognised oem_model; idempotent for the
                # PrimaDonna Soul / Eletta Explore which match by oem_model).
                self.profile = profile_for(self.device.oem_model, self.command_property)
            if self.response_property is None:
                # Optional: absence is fine, the sniffer just skips responses.
                self.response_property = self._detect_property(
                    props, RESPONSE_PROPERTY_CANDIDATES, "response", required=False
                )
            if self.profile.uses_cloud_session and self.connected_property is None:
                self.connected_property = self._detect_property(
                    props, CONNECTED_PROPERTY_CANDIDATES, "connected", required=False
                )
            self._sniff_app_traffic(props)
            self._update_monitor(props)
            self._update_session_from_props(props)
            self._maybe_schedule_session_refresh()
            # Refresh device connection status
            devices = await self.client.async_get_devices()
            for d in devices:
                if d.dsn == self.device.dsn:
                    self.device = d
                    break
            return props
        except Exception as err:
            raise UpdateFailed(f"Error fetching Delonghi data: {err}") from err

    def _update_monitor(self, props: dict[str, Any]) -> None:
        """Decode the machine monitor blob (diagnostic; must never break the poll)."""
        try:
            prop = props.get(MONITOR_PROPERTY)
            value = prop.get("value") if isinstance(prop, dict) else None
            if isinstance(value, str) and value.strip():
                self.monitor = parse_monitor_b64(value)
            else:
                self.monitor = {}
        except Exception:  # noqa: BLE001 - diagnostic must not break polling
            _LOGGER.debug("Monitor parse failed (non-fatal)", exc_info=True)
            self.monitor = {}

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
    # Cloud session (app_device_connected)
    #
    # ECAM models require a registered cloud session before commands are
    # relayed. Logic follows DlghIoT connect(): 4 min cache, adopt foreign
    # app_id, POST + settle delay on cold connect. Cold path runs in a
    # background task so button/service handlers return immediately.
    # ------------------------------------------------------------------ #

    def _parse_app_id_value(self, raw: Any) -> int | None:
        if raw is None:
            return None
        try:
            return normalize_signed_app_id(int(str(raw).strip()))
        except (TypeError, ValueError):
            return None

    def _app_id_from_props(self, props: dict[str, Any]) -> int | None:
        prop = props.get(APP_ID_PROPERTY)
        if isinstance(prop, dict):
            return self._parse_app_id_value(prop.get("value"))
        return None

    async def _read_app_id(self) -> int | None:
        if self.data:
            app_id = self._app_id_from_props(self.data)
            if app_id is not None:
                return app_id
        try:
            prop = await self.client.async_get_property(self.device.dsn, APP_ID_PROPERTY)
        except CloudError:
            _LOGGER.debug("Could not fetch %s (non-fatal)", APP_ID_PROPERTY, exc_info=True)
            return None
        return self._parse_app_id_value(prop.get("value"))

    def _update_session_from_props(self, props: dict[str, Any]) -> None:
        """Parse app_id from poll data; must never break the poll."""
        try:
            app_id = self._app_id_from_props(props)
            if app_id is not None and app_id == self._integration_app_id:
                self._session_confirmed = True
            else:
                self._session_confirmed = False
        except Exception:  # noqa: BLE001 - diagnostic must not break polling
            _LOGGER.debug("Session parse failed (non-fatal)", exc_info=True)

    def _session_is_fresh(self, app_id: int | None) -> bool:
        now = time.time()
        if self._last_connect_at + CONNECT_SETTLE_DELAY > now:
            return True
        if self._last_connect_at + CONNECT_REFRESH_INTERVAL > now:
            if app_id not in (None, 0) and app_id != self._integration_app_id:
                return False
            return True
        return False

    async def _post_cloud_session(self) -> None:
        if not self.connected_property:
            return
        await self.client.async_post_cloud_session(
            self.device.dsn,
            self.connected_property,
            self._integration_app_id,
        )

    async def _background_refresh_session(self) -> None:
        try:
            async with self._session_connect_lock:
                await self._post_cloud_session()
                await asyncio.sleep(CONNECT_SETTLE_DELAY)
                self._last_connect_at = time.time()
                _LOGGER.debug(
                    "Background cloud session refresh completed for dsn=%s",
                    self.device.dsn,
                )
        except Exception:  # noqa: BLE001 - session errors are non-fatal
            _LOGGER.warning(
                "Background cloud session refresh failed for dsn=%s (non-fatal)",
                self.device.dsn,
                exc_info=True,
            )
        finally:
            self._session_refresh_task = None

    def _maybe_schedule_session_refresh(self) -> None:
        if not self.profile.uses_cloud_session or not self.connected_property:
            return
        app_id = self._app_id_from_props(self.data) if self.data else None
        if self._session_is_fresh(app_id):
            _LOGGER.debug("Background cloud session refresh skipped (fresh)")
            return
        if self._session_refresh_task is not None and not self._session_refresh_task.done():
            return
        if self._session_cold_task is not None and not self._session_cold_task.done():
            return
        self._session_refresh_task = self.hass.async_create_background_task(
            self._background_refresh_session(),
            "delonghi cloud session refresh",
        )

    async def _cold_connect_then(
        self, send_fn: Callable[[], Awaitable[None]]
    ) -> None:
        try:
            async with self._session_connect_lock:
                await self._post_cloud_session()
                await asyncio.sleep(CONNECT_SETTLE_DELAY)
                self._last_connect_at = time.time()
            await send_fn()
        except Exception:  # noqa: BLE001 - best-effort send after failed connect
            _LOGGER.warning(
                "Cold cloud session connect failed for dsn=%s (non-fatal); "
                "sending command anyway",
                self.device.dsn,
                exc_info=True,
            )
            await send_fn()
        finally:
            self._session_cold_task = None

    async def _with_cloud_session(
        self, send_fn: Callable[[], Awaitable[None]]
    ) -> None:
        if not self.profile.uses_cloud_session or not self.connected_property:
            await send_fn()
            return

        app_id = await self._read_app_id()

        # When Coffee Link already holds the cloud session (app_id != 0 and != ours),
        # we adopt its app_id so HA commands ride the same session instead of fighting
        # the official app. If the user opens Coffee Link while HA is commanding,
        # behaviour is undefined (the app may hold a LAN lock); close the app first.
        if app_id not in (None, 0) and app_id != self._integration_app_id:
            _LOGGER.info(
                "Adopting foreign cloud session app_id=%d for dsn=%s",
                app_id,
                self.device.dsn,
            )
            self._integration_app_id = app_id
            self._last_connect_at = time.time()
            await send_fn()
            return

        if self._session_is_fresh(app_id):
            _LOGGER.debug("Cloud session warm cache hit for dsn=%s", self.device.dsn)
            await send_fn()
            return

        if self._session_cold_task is not None and not self._session_cold_task.done():
            _LOGGER.info(
                "Cold cloud session connect already in progress for dsn=%s; "
                "command not queued",
                self.device.dsn,
            )
            return

        _LOGGER.info(
            "Scheduling cloud session connect for dsn=%s; command in ~%ds",
            self.device.dsn,
            CONNECT_SETTLE_DELAY,
        )
        self._session_cold_task = self.hass.async_create_background_task(
            self._cold_connect_then(send_fn),
            "delonghi cloud session cold connect",
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
        # Sanitize a wake frame persisted BEFORE the params guard existed: a
        # session-refresh packet (e.g. params 03 02) stored as the wake frame
        # would otherwise be replayed forever. Drop it so a real power-on from
        # the app re-teaches it.
        if self.learned_wake_frame is not None and not is_wake_power_frame(
            decode_command(self.learned_wake_frame)
        ):
            _LOGGER.warning(
                "Discarding persisted wake frame (not a real power-on): %s. "
                "Power the machine on once from the official app to re-learn it.",
                self.learned_wake_frame,
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
        """Dump the machine's stored recipe datapoints to the log (read-only).

        Diagnostic for the "zero-touch" work: lets a tester surface the recipes
        the machine stores so the recipe->command mapping can be confirmed.
        Sends nothing to the machine.
        """
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
        """Callback for the debounced Store save."""
        return serialize_learned_frames(
            self.learned_start_frames, self.learned_stop_frames, self.learned_wake_frame
        )

    def _maybe_learn_frame(self, decoded: dict) -> None:
        """Learn the exact frame the official app sent for a beverage.

        Models that ``learns_from_app`` ignore the Soul-style fixed recipe;
        replaying the app's own frame verbatim is the reliable way to reproduce a
        beverage (quantity / intensity / milk, the right start-action byte, and
        the device signature are all preserved). Stop frames (action 0x02) are
        kept separately from start frames so a captured stop never gets replayed
        for a start press. The power-on (wake) frame is learned too - the app
        appends a device signature a synthesized wake lacks. New/changed frames
        are persisted (debounced) so they survive restarts.
        """
        if not self.profile.learns_from_app:
            return
        raw_b64 = decoded.get("raw_b64")
        if not raw_b64:
            return
        ftype = decoded.get("type")

        if ftype == "power":
            # The app also emits 0x84 0x0f frames that are NOT a power-on (e.g.
            # session-refresh packets with params 03 02, seen in issue #1
            # captures). Only the real wake params may be learned, otherwise a
            # refresh packet would overwrite the learned power-on frame.
            if not is_wake_power_frame(decoded):
                _LOGGER.debug(
                    "Ignoring power-family frame with params [%s] "
                    "(not a wake/power-on, keeping learned wake frame)",
                    decoded.get("params"),
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
        """Build + send a beverage command via the resolved command property."""
        from .command_builder import build_and_encode

        async def _do() -> None:
            table = (
                self.learned_stop_frames if action == ACTION_STOP else self.learned_start_frames
            )
            learned = table.get(beverage_id)
            value = self.profile.beverage_value(beverage_id, action, learned)
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
            _LOGGER.info(
                "Sending beverage cmd via %s: bev_id=0x%02x action=%d value=%s",
                prop,
                beverage_id,
                action,
                value,
            )
            await self.client.async_set_property_value(self.device.dsn, prop, value)
            await self.async_request_refresh()

        await self._with_cloud_session(_do)

    async def async_send_wake(self) -> None:
        """Send the WAKE / power-on command to bring the machine out of standby."""
        from .command_builder import build_wake_encoded

        async def _do() -> None:
            value = self.profile.wake_value(self.learned_wake_frame)
            if value is None:
                value = build_wake_encoded()
                _LOGGER.warning(
                    "No learned wake frame for this %s yet. Power the machine on once "
                    "from the official Coffee Link app so Home Assistant can capture "
                    "and replay it. Sending a best-effort synthesized wake meanwhile "
                    "(the machine will likely ignore it - it lacks the device "
                    "signature the app appends).",
                    self.profile.label,
                )
            self._record_sent(value)
            prop = self.command_property or COMMAND_PROPERTY_CANDIDATES[0]
            _LOGGER.info("Sending WAKE cmd via %s: %s", prop, value)
            await self.client.async_set_property_value(self.device.dsn, prop, value)
            await self.async_request_refresh()

        await self._with_cloud_session(_do)

    def _learned_device_signature(self) -> bytes | None:
        """The 4-byte per-device signature carried by learned app frames (the
        wake frame first, else any learned beverage frame)."""
        from .command_builder import device_signature_from_frame

        for frame in (
            self.learned_wake_frame,
            *self.learned_start_frames.values(),
            *self.learned_stop_frames.values(),
        ):
            sig = device_signature_from_frame(frame)
            if sig is not None:
                return sig
        return None

    async def async_send_standby(self) -> None:
        """Send the STANDBY / power-off command (84 0f, params 01 01).

        Always synthesized - the official app has no power-off control to
        capture. Validated live on the reference Soul; on learn-and-replay
        models the per-device signature from a learned frame is appended.
        """
        from .command_builder import build_standby_encoded

        async def _do() -> None:
            value = self.profile.standby_value(self._learned_device_signature())
            if value is None:
                value = build_standby_encoded()
                _LOGGER.warning(
                    "No learned frame for this %s yet, so the standby command is "
                    "sent without the device signature and the machine may ignore "
                    "it. Trigger any command once from the official Coffee Link "
                    "app (e.g. power-on) so Home Assistant can learn the signature.",
                    self.profile.label,
                )
            self._record_sent(value)
            prop = self.command_property or COMMAND_PROPERTY_CANDIDATES[0]
            _LOGGER.info("Sending STANDBY cmd via %s: %s", prop, value)
            await self.client.async_set_property_value(self.device.dsn, prop, value)
            await self.async_request_refresh()

        await self._with_cloud_session(_do)

    async def async_send_raw(self, value: str) -> None:
        """Send a raw base64 command on the resolved command channel (advanced)."""

        async def _do() -> None:
            self._record_sent(value)
            prop = self.command_property or COMMAND_PROPERTY_CANDIDATES[0]
            _LOGGER.info("Sending RAW cmd via %s: %s", prop, value)
            await self.client.async_set_property_value(self.device.dsn, prop, value)
            await self.async_request_refresh()

        await self._with_cloud_session(_do)
