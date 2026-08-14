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
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ayla_client import (
    AuthError,
    AylaDevice,
    CloudError,
    DelonghiAylaClient,
    normalize_signed_app_id,
)
from .command_builder import (
    builder_structural_b64,
    build_session_refresh_encoded,
    build_standby_encoded,
    build_standby_with_session_tail_encoded,
    build_wake_encoded,
    build_wake_with_session_tail_encoded,
    decode_command,
    deserialize_learned_frames,
    is_wake_power_frame,
    recipe_dump_lines,
    replay_with_timestamp,
    serialize_learned_frames,
    validate_replayed_wake_frame,
)
from .const import (
    ACTION_STOP,
    APP_ID_PROPERTY,
    COMMAND_PROPERTY_CANDIDATES,
    COMMAND_CONFIRM_POLL_INTERVAL,
    COMMAND_CONFIRM_TIMEOUT,
    CONNECT_CONFIRM_POLL_INTERVAL,
    CONNECT_CONFIRM_TIMEOUT,
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
        # _integration_app_id may temporarily hold a foreign id (official app's
        # session, adopted to ride it); it reverts to the default once the app
        # releases the session - see _update_session_from_props.
        self._default_app_id = normalize_signed_app_id(INTEGRATION_CLOUD_APP_ID)
        self._integration_app_id = self._default_app_id
        self._last_connect_at: float = 0
        self._session_confirmed = False
        self._session_connect_lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()
        self.active_beverage_id: int | None = None
        self.last_command_result = "idle"
        if self.profile.uses_cloud_session:
            self._last_seen_app_id: int | None = None
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

    async def async_shutdown(self) -> None:
        """Cancel in-flight session work and reset session state on unload."""
        self._last_connect_at = 0
        if self._integration_app_id != self._default_app_id:
            self._integration_app_id = self._default_app_id
        await super().async_shutdown()

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
            # Refresh device connection status
            devices = await self.client.async_get_devices()
            for d in devices:
                if d.dsn == self.device.dsn:
                    self.device = d
                    break
            return props
        except AuthError as err:
            raise ConfigEntryAuthFailed("Coffee Link credentials are no longer valid") from err
        except CloudError as err:
            raise UpdateFailed(f"Ayla cloud error: {err}") from err
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
    # relayed. Logic follows DlghIoT connect(): adopt foreign app_id, POST +
    # settle delay on cold connect only (on-demand before commands). Poll does
    # NOT register a session — that would block the official Coffee Link app
    # while HA is idle. After an HA command the machine keeps app_id for ~300 s
    # (protocol limit); Coffee Link may be temporarily blocked until timeout.
    # Cold path runs in a background task so button/service handlers return
    # immediately.
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

    async def _read_app_id(self, *, live: bool = False) -> int | None:
        if not live and self.data:
            app_id = self._app_id_from_props(self.data)
            if app_id is not None:
                return app_id
        app_id, _ok = await self._fetch_app_id_live()
        return app_id

    async def _fetch_app_id_live(self) -> tuple[int | None, bool]:
        """Direct GET app_id. Returns (value, fetch_ok); fetch_ok=False on cloud error."""
        try:
            prop = await self.client.async_get_property_resilient(
                self.device.dsn, APP_ID_PROPERTY
            )
        except CloudError as err:
            status = getattr(err, "http_status", None)
            _LOGGER.warning(
                "Live app_id fetch failed for dsn=%s (http=%s): %s",
                self.device.dsn,
                status,
                err,
            )
            return None, False
        return self._parse_app_id_value(prop.get("value")), True

    async def _wait_for_session_confirmed(self) -> bool:
        """Poll app_id until it matches our integration id (DlghIoT connect loop)."""
        started = time.time()
        last_progress = started
        poll_count = 0
        cloud_errors = 0
        _LOGGER.debug(
            "Waiting for cloud session confirmation (timeout=%ds)",
            CONNECT_CONFIRM_TIMEOUT,
        )
        while time.time() - started < CONNECT_CONFIRM_TIMEOUT:
            poll_count += 1
            app_id, fetch_ok = await self._fetch_app_id_live()
            if not fetch_ok:
                cloud_errors += 1
            elif app_id == self._integration_app_id:
                elapsed = time.time() - started
                self._session_confirmed = True
                _LOGGER.info(
                    "Cloud session confirmed after %.1fs (polls=%d, cloud_errors=%d)",
                    elapsed,
                    poll_count,
                    cloud_errors,
                )
                return True
            await asyncio.sleep(CONNECT_CONFIRM_POLL_INTERVAL)
            now = time.time()
            if now - last_progress >= 15:
                _LOGGER.debug(
                    "Still waiting for cloud session confirmation (%.0fs/%ds, "
                    "polls=%d, cloud_errors=%d)",
                    now - started,
                    CONNECT_CONFIRM_TIMEOUT,
                    poll_count,
                    cloud_errors,
                )
                last_progress = now
        _last_app_id, last_ok = await self._fetch_app_id_live()
        _LOGGER.warning(
            "Connect POST sent but the session was not confirmed after %ds "
            "(last fetch ok=%s, polls=%d, cloud_errors=%d)",
            CONNECT_CONFIRM_TIMEOUT,
            last_ok,
            poll_count,
            cloud_errors,
        )
        self._session_confirmed = False
        return False

    def _update_session_from_props(self, props: dict[str, Any]) -> None:
        """Parse app_id from poll data; must never break the poll."""
        try:
            app_id = self._app_id_from_props(props)
            if self.profile.uses_cloud_session and app_id != self._last_seen_app_id:
                if app_id in (None, 0):
                    holder = "free"
                elif app_id == self._default_app_id:
                    holder = "ha"
                else:
                    holder = "foreign"
                _LOGGER.debug(
                    "Cloud session holder changed to %s",
                    holder,
                )
                self._last_seen_app_id = app_id
            self._session_confirmed = (
                app_id is not None and app_id == self._integration_app_id
            )
            # An adopted foreign session (official app's id) is transient: once
            # the machine reports no session holder, revert to our own id so we
            # never keep a foreign session alive on the app's behalf.
            if app_id == 0 and self._integration_app_id != self._default_app_id:
                _LOGGER.info(
                    "Foreign cloud session released on dsn=%s; reverting to own app_id",
                    self.device.dsn,
                )
                self._integration_app_id = self._default_app_id
                self._last_connect_at = 0
        except Exception:  # noqa: BLE001 - diagnostic must not break polling
            _LOGGER.debug("Session parse failed (non-fatal)", exc_info=True)

    def cloud_session_holder(self, app_id: int | None) -> str:
        """Return a privacy-safe session-holder label."""
        if app_id is None:
            return "unknown"
        if app_id == 0:
            return "free"
        if app_id == self._integration_app_id:
            return "ha"
        return "foreign"

    def _revert_foreign_app_id_if_session_clear(self, app_id: int | None) -> None:
        """Before a cold POST, use our own cloud id when no session is held."""
        if app_id in (None, 0) and self._integration_app_id != self._default_app_id:
            _LOGGER.info(
                "No cloud session holder on dsn=%s; reverting to own app_id before connect",
                self.device.dsn,
            )
            self._integration_app_id = self._default_app_id
            self._last_connect_at = 0

    def _command_confirmation_snapshot(self) -> tuple[Any, dict[str, Any]]:
        """Return privacy-safe state used to detect a machine acknowledgement."""
        return self._last_resp_marker, dict(self.monitor)

    async def _wait_for_command_confirmation(
        self, before: tuple[Any, dict[str, Any]]
    ) -> bool | None:
        """Return True/False for supported confirmation, or None if unavailable."""
        if self.response_property is None and not self.monitor:
            return None

        response_marker, monitor = before
        deadline = time.monotonic() + COMMAND_CONFIRM_TIMEOUT
        while time.monotonic() < deadline:
            await self.async_request_refresh()
            if self.response_property and self._last_resp_marker != response_marker:
                return True
            if self.monitor and self.monitor != monitor:
                return True
            await asyncio.sleep(COMMAND_CONFIRM_POLL_INTERVAL)
        return False

    async def _send_property_command(
        self, value: str, label: str, *, confirm: bool = True
    ) -> None:
        prop = self.command_property or COMMAND_PROPERTY_CANDIDATES[0]
        before = self._command_confirmation_snapshot()
        self._record_sent(value)
        _LOGGER.info("Sending %s via %s (len=%d)", label, prop, len(value))
        await self.client.async_set_property_value(self.device.dsn, prop, value)
        self.last_command_result = "sent"
        if not confirm:
            return
        confirmed = await self._wait_for_command_confirmation(before)
        if confirmed is True:
            self.last_command_result = "acknowledged"
        elif confirmed is False:
            self.last_command_result = "timed_out"
            raise HomeAssistantError(
                "The command was sent, but the coffee maker did not acknowledge it in time"
            )

    async def _maybe_send_session_refresh(self) -> None:
        """DlghIoT refresh(): nudge deep standby before wake when monitor=0."""
        if self.monitor.get("status") != 0:
            return
        value = build_session_refresh_encoded(self._integration_app_id)
        _LOGGER.info("Machine in standby; sending a session refresh before wake")
        await self._send_property_command(value, "SESSION REFRESH", confirm=False)

    def _wake_command_value(self) -> str:
        """Build wake frame for ECAM models (session tail). Soul uses main inline path."""
        return build_wake_with_session_tail_encoded(self._integration_app_id)

    def _standby_command_value(self) -> str:
        """Build standby frame for ECAM models (session tail). Soul uses main inline path."""
        return build_standby_with_session_tail_encoded(self._integration_app_id)

    def _session_is_fresh(self, app_id: int | None) -> bool:
        now = time.time()
        if self._last_connect_at + CONNECT_SETTLE_DELAY > now:
            return True
        if self._last_connect_at + CONNECT_REFRESH_INTERVAL > now:
            if app_id == 0:
                return False
            if app_id is not None and app_id != self._integration_app_id:
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

    async def _with_cloud_session(
        self, send_fn: Callable[[], Awaitable[None]]
    ) -> None:
        if not self.profile.uses_cloud_session or not self.connected_property:
            await send_fn()
            return

        async with self._session_connect_lock:
            app_id = await self._read_app_id()
            self._revert_foreign_app_id_if_session_clear(app_id)

            # Ride an existing official-app session without registering it as ours.
            if app_id not in (None, 0) and app_id != self._integration_app_id:
                self._integration_app_id = app_id
                self._last_connect_at = time.time()
            elif self._session_is_fresh(app_id):
                if not self._session_confirmed:
                    live_app_id, fetch_ok = await self._fetch_app_id_live()
                    if not fetch_ok or live_app_id != self._integration_app_id:
                        raise HomeAssistantError(
                            "The cached Coffee Link cloud session could not be verified"
                        )
            else:
                await self._post_cloud_session()
                await asyncio.sleep(CONNECT_SETTLE_DELAY)
                if not await self._wait_for_session_confirmed():
                    self.last_command_result = "timed_out"
                    raise HomeAssistantError(
                        "Timed out while acquiring the Coffee Link cloud session"
                    )
                self._last_connect_at = time.time()

            await send_fn()

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
            summary = (
                f"type={decoded.get('type')} style={decoded.get('style')} "
                f"beverage={decoded.get('beverage_name')} "
                f"action={decoded.get('action_name')} crc_valid={decoded.get('crc_valid')}"
            )
            if origin == "app":
                _LOGGER.warning(
                    "Captured app-to-machine command on %s: %s",
                    prop_name,
                    summary,
                )
            else:
                _LOGGER.debug("Observed own command echoed on %s: %s", prop_name, summary)
        else:
            decoded["captured_at"] = prop.get("data_updated_at")
            self.last_machine_response = decoded
            _LOGGER.debug(
                "Machine-to-app response on %s: type=%s",
                prop_name,
                decoded.get("type"),
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
        if self.learned_wake_frame is not None:
            if self.profile.learns_from_app:
                if not validate_replayed_wake_frame(
                    replay_with_timestamp(self.learned_wake_frame)
                ):
                    _LOGGER.warning(
                        "Discarding persisted wake frame (integrity check failed). "
                        "Power the machine on once from the official app to re-learn it.",
                    )
                    self.learned_wake_frame = None
            elif not is_wake_power_frame(decode_command(self.learned_wake_frame)):
                _LOGGER.warning(
                    "Discarding persisted wake frame (not a real power-on). "
                    "Power the machine on once from the official app to re-learn it.",
                )
                self.learned_wake_frame = None
        total = (
            len(self.learned_start_frames)
            + len(self.learned_stop_frames)
            + (1 if self.learned_wake_frame else 0)
        )
        if total:
            _LOGGER.debug("Restored %d learned command frame(s)", total)
        self._restore_device_app_id()

    def log_recipe_datapoints(self) -> None:
        """Dump the machine's stored recipe datapoints to the log (read-only).

        Diagnostic for the "zero-touch" work: lets a tester surface the recipes
        the machine stores so the recipe->command mapping can be confirmed.
        Sends nothing to the machine.
        """
        if not self.data:
            _LOGGER.warning("Recipe dump requested but no data fetched yet.")
            return
        names = [line.partition(" = ")[0] for line in recipe_dump_lines(self.data)]
        _LOGGER.warning("Recipe datapoints detected: %s", ", ".join(names))

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
                self._restore_device_app_id()
                _LOGGER.info("Learned a %s wake/power-on frame", self.profile.key)
                self._store.async_delay_save(
                    self._learned_storage_data, RECIPE_STORE_SAVE_DELAY
                )
            return

        if ftype != "beverage":
            return
        bev_hex = decoded.get("beverage_id")
        if not bev_hex:
            return
        try:
            bev_id = int(bev_hex, 16)
        except (ValueError, TypeError):
            return
        if decoded.get("action") == ACTION_STOP:
            if self.active_beverage_id == bev_id:
                self.active_beverage_id = None
        else:
            self.active_beverage_id = bev_id
        table = (
            self.learned_stop_frames
            if decoded.get("action") == ACTION_STOP
            else self.learned_start_frames
        )
        if table.get(bev_id) != raw_b64:
            table[bev_id] = raw_b64
            self._restore_device_app_id()
            _LOGGER.info(
                "Learned a %s %s frame for beverage 0x%02x (%s)",
                self.profile.key,
                "stop" if decoded.get("action") == ACTION_STOP else "start",
                bev_id,
                decoded.get("beverage_name"),
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
                if self.profile.learns_from_app:
                    raise HomeAssistantError(
                        "This command is unavailable until its frame has been learned "
                        "from the official Coffee Link app"
                    )
                value = build_and_encode(beverage_id, action)
            await self._send_property_command(
                value,
                f"beverage 0x{beverage_id:02x} action {action}",
            )

        await self._run_command_transaction(_do)
        if action == ACTION_STOP:
            if self.active_beverage_id == beverage_id:
                self.active_beverage_id = None
        else:
            self.active_beverage_id = beverage_id
        self.async_update_listeners()

    async def async_stop_active_beverage(self) -> None:
        """Stop the tracked active beverage without guessing an identifier."""
        if self.active_beverage_id is None:
            self.last_command_result = "rejected"
            raise HomeAssistantError(
                "The active beverage is unknown; start a drink before using Stop"
            )
        await self.async_send_beverage(self.active_beverage_id, ACTION_STOP)

    async def _run_command_transaction(
        self, send_fn: Callable[[], Awaitable[None]]
    ) -> None:
        """Serialize one complete connect, write and confirmation transaction."""
        if self._command_lock.locked():
            self.last_command_result = "rejected"
            raise HomeAssistantError(
                "Another coffee maker command is still in progress; try again shortly"
            )

        async with self._command_lock:
            self.last_command_result = "pending"
            try:
                await self._with_cloud_session(send_fn)
            except ConfigEntryAuthFailed:
                self.last_command_result = "rejected"
                raise
            except AuthError as err:
                self.last_command_result = "rejected"
                raise ConfigEntryAuthFailed(
                    "Coffee Link credentials are no longer valid"
                ) from err
            except HomeAssistantError:
                if self.last_command_result != "timed_out":
                    self.last_command_result = "rejected"
                raise
            except (CloudError, asyncio.TimeoutError) as err:
                self.last_command_result = "timed_out"
                raise HomeAssistantError(
                    "The Coffee Link cloud command could not be completed"
                ) from err
            except Exception as err:
                self.last_command_result = "rejected"
                raise HomeAssistantError(
                    "The coffee maker command failed before completion"
                ) from err
            if self.last_command_result not in {"sent", "acknowledged"}:
                self.last_command_result = "sent"

    async def async_send_wake(self) -> None:
        """Send the WAKE / power-on command to bring the machine out of standby."""
        if not self.profile.uses_cloud_session:

            async def _do() -> None:
                value = self.profile.wake_value(self.learned_wake_frame)
                if value is None:
                    if self.profile.learns_from_app:
                        raise HomeAssistantError(
                            "Wake is unavailable until its frame has been learned "
                            "from the official Coffee Link app"
                        )
                    value = build_wake_encoded()
                await self._send_property_command(value, "WAKE command")

            await self._run_command_transaction(_do)
            return

        async def _do() -> None:
            await self._maybe_send_session_refresh()
            await self._send_property_command(self._wake_command_value(), "WAKE command")

        await self._run_command_transaction(_do)

    def _learned_device_signature(self) -> bytes | None:
        """Return the device signature carried by any learned app frame."""
        from .command_builder import device_signature_from_frame

        for frame in (
            self.learned_wake_frame,
            *self.learned_start_frames.values(),
            *self.learned_stop_frames.values(),
        ):
            signature = device_signature_from_frame(frame)
            if signature is not None:
                return signature
        return None

    def _restore_device_app_id(self) -> None:
        """Use the learned per-device signature for ECAM cloud sessions."""
        if not self.profile.uses_cloud_session:
            return
        signature = self._learned_device_signature()
        if signature is None:
            return
        app_id = normalize_signed_app_id(int.from_bytes(signature, "big"))
        self._default_app_id = app_id
        self._integration_app_id = app_id

    async def async_send_standby(self) -> None:
        """Send the STANDBY / power-off command."""
        if not self.profile.uses_cloud_session:

            async def _do() -> None:
                value = self.profile.standby_value(self._learned_device_signature())
                if value is None:
                    if self.profile.learns_from_app:
                        raise HomeAssistantError(
                            "Standby is unavailable until a device signature has been learned"
                        )
                    value = build_standby_encoded()
                await self._send_property_command(value, "STANDBY command")

            await self._run_command_transaction(_do)
            return

        async def _do() -> None:
            await self._send_property_command(
                self._standby_command_value(), "STANDBY command"
            )

        await self._run_command_transaction(_do)

    async def async_send_raw(self, value: str) -> None:
        """Validate and send a raw base64 command (administrator-only action)."""
        if "error" in decode_command(value):
            self.last_command_result = "rejected"
            raise HomeAssistantError("The raw command is not a valid base64 protocol frame")

        async def _do() -> None:
            await self._send_property_command(value, "RAW command")

        await self._run_command_transaction(_do)
