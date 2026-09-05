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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ayla_client import AylaDevice, CloudError, DelonghiAylaClient, normalize_signed_app_id
from .command_builder import (
    app_id_from_signature,
    builder_structural_b64,
    build_session_refresh_encoded,
    build_standby_encoded,
    build_standby_with_session_tail_encoded,
    build_wake_encoded,
    build_wake_with_session_tail_encoded,
    decode_command,
    deserialize_learned_frames,
    first_device_signature,
    is_wake_power_frame,
    learnable_beverage_id,
    recipe_dump_lines,
    replay_with_timestamp,
    serialize_learned_frames,
    summarize_decoded,
    validate_replayed_wake_frame,
)
from .catalog import (
    build_catalog,
    catalog_beverage_ids,
    catalog_fingerprint,
    catalog_summary,
)
from .const import (
    ACTION_STOP,
    APP_ID_PROPERTY,
    COMMAND_PROPERTY_CANDIDATES,
    CONNECT_CONFIRM_POLL_INTERVAL,
    CONNECT_CONFIRM_TIMEOUT,
    CONNECT_REFRESH_INTERVAL,
    CONNECT_SETTLE_DELAY,
    CONNECTED_PROPERTY_CANDIDATES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    INTEGRATION_CLOUD_APP_ID,
    MONITOR_KEEPALIVE_INTERVAL,
    MONITOR_PROPERTY_CANDIDATES,
    REACHABILITY_MAX_AGE,
    RECIPE_STORE_SAVE_DELAY,
    RECIPE_STORE_VERSION,
    RESPONSE_PROPERTY_CANDIDATES,
    normalize_connection_status,
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
        self.monitor_property: str | None = None
        # When the device record (and therefore connection_status) was last
        # refreshed. Set here because __init__.py hands us a freshly listed
        # device, then updated on every successful poll.
        self._device_seen_at: float = time.time()
        # Cloud session (ECAM / app_device_connected) — DlghIoT-compatible cache.
        # _integration_app_id may temporarily hold a foreign id (official app's
        # session, adopted to ride it); it reverts to the default once the app
        # releases the session - see _update_session_from_props.
        self._default_app_id = normalize_signed_app_id(INTEGRATION_CLOUD_APP_ID)
        # The machine's own cloud id, derived from the device signature carried by
        # any learned app frame. It is what an ECAM actually honours; the constant
        # above is only a fallback until a frame has been learned (issue #15).
        self._device_app_id: int | None = None
        self._integration_app_id = self._default_app_id
        self._last_connect_at: float = 0
        # Last `device_connected` keepalive; 0 = due immediately on first poll.
        self._last_monitor_session_at: float = 0
        # Keepalive health, so a persistent failure is visible from the first
        # occurrence instead of surfacing as a frozen status days later.
        self._keepalive_failures = 0
        self._keepalive_armed = False
        self._session_confirmed = False
        self._session_connect_lock = asyncio.Lock()
        self._session_cold_task: asyncio.Task[None] | None = None
        if self.profile.uses_cloud_session:
            self._last_seen_app_id: int | None = None
        else:
            self._session_refresh_task: asyncio.Task[None] | None = None
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
        # Decoded monitor state (standby/ready/...), surfaced via the Machine
        # Status sensor; which datapoint it comes from is resolved per model
        # (see MONITOR_PROPERTY_CANDIDATES). Empty dict until a blob parses.
        self.monitor: dict[str, Any] = {}
        # The machine's own beverage catalogue, parsed from the recipe datapoints
        # already present in every poll (see catalog.py). Read-only for now: it
        # widens the learn gate and feeds the diagnostic dump, and creates no
        # entities. ``None`` until the first poll produces readable blobs.
        self.catalog: dict[str, Any] | None = None
        self._catalog_fingerprint: tuple | None = None
        self._store: Store = Store(
            hass, RECIPE_STORE_VERSION, f"{DOMAIN}_recipes_{device.dsn}"
        )

    async def async_shutdown(self) -> None:
        """Cancel in-flight session work and reset session state on unload."""
        if self._session_cold_task and not self._session_cold_task.done():
            self._session_cold_task.cancel()
        self._session_cold_task = None
        self._last_connect_at = 0
        if self._integration_app_id != self._own_app_id():
            self._integration_app_id = self._own_app_id()
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
            needs_connected = (
                self.profile.uses_cloud_session or self.profile.keeps_monitor_session
            )
            if needs_connected and self.connected_property is None:
                self.connected_property = self._detect_property(
                    props, CONNECTED_PROPERTY_CANDIDATES, "connected", required=False
                )
            # Before sniffing: the learn gate consults the catalogue, so a frame
            # captured on this very poll must be able to see the ids it declares.
            self._update_catalog(props)
            self._sniff_app_traffic(props)
            self._update_monitor(props)
            self._update_session_from_props(props)
            # Refresh device connection status
            devices = await self.client.async_get_devices()
            for d in devices:
                if d.dsn == self.device.dsn:
                    self.device = d
                    self._device_seen_at = time.time()
                    break
            # Ask the machine to publish a fresh monitor blob for the NEXT poll.
            # Done last, and deliberately not before the read above: the machine
            # takes several seconds to answer, so the value it produces is the
            # one this poll's successor will pick up.
            await self._refresh_monitor_session()
            return props
        except CloudError as err:
            raise UpdateFailed(f"Ayla cloud error: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Error fetching Delonghi data: {err}") from err

    async def _refresh_monitor_session(self) -> None:
        """Keep the machine publishing its monitor datapoint (issue #14).

        The PrimaDonna Soul publishes ``d302_monitor`` - its live status - only
        when an app session is written to ``device_connected``; it does not push
        status changes on its own. Verified on ECAM610.55 / ``DL-millcore`` /
        ADA 1.5.3: a write is answered with a fresh monitor blob in ~4-7 s, and
        with no write the datapoint had not moved in **five days** while the
        machine was in daily use.

        The failure this prevents is a silent one. Counters (``d7xx_*``) and
        settings (``d2xx_*``) *are* pushed by the machine unprompted, so every
        poll succeeds, ``connection_status`` stays ``Online`` and the integration
        looks healthy - while Machine Status reports a value that may be days
        old. Automations keyed on it simply never fire.

        Never fatal: a failed keepalive costs one stale poll, so it must not take
        the whole update down with it. Never quiet either. A keepalive that
        fails every time rebuilds the exact blind spot this method exists to
        close - polls green, machine Online, counters moving, status frozen - so
        the first failure and the recovery are warnings and only the repeats are
        debug. The write is a plain POST that never enters the transient-retry
        helper, so nothing else in the stack would report it.
        """
        if not self.profile.keeps_monitor_session or not self.connected_property:
            return
        value = self.profile.monitor_session_value()
        if value is None:
            return
        now = time.time()
        if now - self._last_monitor_session_at < MONITOR_KEEPALIVE_INTERVAL:
            return
        if not self._keepalive_armed:
            self._keepalive_armed = True
            _LOGGER.info(
                "Keeping the monitor session alive for dsn=%s: writing '%s' every "
                "%ds so the machine keeps publishing its status (issue #14)",
                self.device.dsn,
                self.connected_property,
                MONITOR_KEEPALIVE_INTERVAL,
            )
        try:
            await self.client.async_set_property_value(
                self.device.dsn, self.connected_property, value
            )
        except Exception as err:  # noqa: BLE001 - a missed keepalive must not fail the poll
            # The rate-limit clock is deliberately NOT advanced here: a failure
            # is retried on the next poll rather than deferred a full interval,
            # which would turn every hiccup into guaranteed silence.
            self._keepalive_failures += 1
            if self._keepalive_failures == 1:
                _LOGGER.warning(
                    "Monitor session keepalive failed for dsn=%s: %s. Machine "
                    "Status will freeze on its last value until this recovers.",
                    self.device.dsn,
                    err,
                )
            else:
                _LOGGER.debug(
                    "Monitor session keepalive still failing for dsn=%s (%d in a row)",
                    self.device.dsn,
                    self._keepalive_failures,
                    exc_info=True,
                )
            return
        self._last_monitor_session_at = now
        if self._keepalive_failures:
            _LOGGER.warning(
                "Monitor session keepalive recovered for dsn=%s after %d failure(s)",
                self.device.dsn,
                self._keepalive_failures,
            )
            self._keepalive_failures = 0

    @staticmethod
    def _monitor_value(props: dict[str, Any], prop_name: str | None) -> str | None:
        """The non-empty string value of a monitor candidate, else None."""
        if not prop_name:
            return None
        prop = props.get(prop_name)
        value = prop.get("value") if isinstance(prop, dict) else None
        if isinstance(value, str) and value.strip():
            return value
        return None

    def _decoded_monitor_candidates(
        self, props: dict[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        """``(name, decoded)`` per monitor candidate carrying data, in priority order."""
        decoded: list[tuple[str, dict[str, Any]]] = []
        for candidate in MONITOR_PROPERTY_CANDIDATES:
            value = self._monitor_value(props, candidate)
            if value is not None:
                decoded.append((candidate, parse_monitor_b64(value)))
        return decoded

    def _update_catalog(self, props: dict[str, Any]) -> None:
        """Rebuild the machine's beverage catalogue when its recipe blobs change.

        Pure parse of properties already in hand - no extra request, no protocol
        traffic. Recipes only change when someone edits one on the machine or in
        the app, so a key over the catalogue datapoints (and only those, see
        ``catalog_fingerprint``) keeps the ~4 ms parse off every poll where
        nothing moved. The key is the values themselves, not their hash: a hash
        collision would freeze the catalogue silently, which is the one failure
        mode not worth trading for a few bytes.

        Like the monitor decode, this is diagnostic-grade: it must never be able
        to break a poll, so any unexpected failure keeps the previous catalogue.
        """
        try:
            fingerprint = catalog_fingerprint(props)
            if fingerprint == self._catalog_fingerprint and self.catalog is not None:
                return
            catalog = build_catalog(props)
            if catalog["source"] == "empty":
                # Nothing readable this time: keep whatever we had. A blank poll
                # must never look like a machine that lost its recipes.
                if self.catalog is None:
                    _LOGGER.debug(
                        "No machine beverage catalogue yet for dsn=%s "
                        "(no readable recipe blobs in %d properties)",
                        self.device.dsn,
                        len(props),
                    )
                return
            self._catalog_fingerprint = fingerprint
            first = self.catalog is None
            self.catalog = catalog
            _LOGGER.log(
                logging.INFO if first else logging.DEBUG,
                "Machine beverage catalogue for dsn=%s: %s",
                self.device.dsn,
                catalog_summary(catalog),
            )
        except Exception:  # noqa: BLE001 - diagnostic must never break the poll
            _LOGGER.debug("Beverage catalogue parse failed; keeping previous", exc_info=True)

    def _update_monitor(self, props: dict[str, Any]) -> None:
        """Decode the machine monitor blob (diagnostic; must never break the poll).

        Which datapoint carries it depends on the model (issue #14), so the
        candidates are weighed on EVERY poll rather than locked in once:

        - a candidate only wins if its blob actually decodes. Being listed
          proves nothing, and carrying bytes proves nothing either - a stale or
          truncated packet would otherwise lock the poll onto a datapoint that
          can never yield a status;
        - when nothing decodes, the first candidate that at least had data is
          kept, so its parse error reaches the sensor instead of a blank state;
        - a machine that starts publishing on the other datapoint (firmware
          update, or a one-off legacy value that goes quiet) is followed
          automatically instead of needing a restart.
        """
        try:
            available = self._decoded_monitor_candidates(props)
            chosen = next(
                ((name, mon) for name, mon in available if "error" not in mon), None
            )
            if chosen is None:
                if not available:
                    self.monitor = {}
                    return
                chosen = available[0]
            name, monitor = chosen
            if name != self.monitor_property:
                _LOGGER.info(
                    "Using monitor property '%s' for dsn=%s (oem_model=%s)",
                    name,
                    self.device.dsn,
                    self.device.oem_model,
                )
                self.monitor_property = name
            self.monitor = monitor
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
    # Reachability preflight
    # ------------------------------------------------------------------ #

    @property
    def machine_is_offline(self) -> bool:
        """True when the cloud reports this machine as disconnected.

        Only an explicit "Offline" counts - an unknown, missing or unexpected
        status must never block a command. On the reference PrimaDonna Soul a
        machine sitting in standby stays Online (its WiFi module does not
        sleep), so this says nothing about whether the machine is awake, only
        about whether the cloud can reach it at all.
        """
        return normalize_connection_status(self.device.connection_status) == "offline"

    @property
    def reachability_is_current(self) -> bool:
        """True while the cloud status is recent enough to act on.

        connection_status only moves on a successful poll, so a cloud outage or
        a stuck poll loop freezes whatever it last said. Refusing commands on a
        frozen "Offline" would keep blaming the machine long after it came back,
        so past REACHABILITY_MAX_AGE the preflight stops refusing. Failing open
        is the safe direction: the worst case is the pre-0.3.19 behaviour.
        """
        return time.time() - self._device_seen_at <= REACHABILITY_MAX_AGE

    def _ensure_machine_reachable(self) -> None:
        """Refuse to send a command to a machine the cloud cannot reach.

        Ayla happily accepts a datapoint write for an offline machine and
        answers HTTP 200/201; the frame is simply never delivered. Without this
        guard the failure is completely silent - no toast, no error log at all -
        which is exactly how a machine that had been off the network for ten
        days went unnoticed.
        """
        if not self.machine_is_offline:
            return
        if not self.reachability_is_current:
            _LOGGER.warning(
                "dsn=%s was last seen Offline %.0fs ago and the cloud has not been "
                "reachable since; sending anyway rather than blocking on a stale "
                "status.",
                self.device.dsn,
                time.time() - self._device_seen_at,
            )
            return
        _LOGGER.warning(
            "Refusing to send a command: dsn=%s is Offline on the De'Longhi cloud "
            "(last connected: %s). The cloud would accept the write and the machine "
            "would never receive it. Check that the machine is powered at the mains "
            "and joined to WiFi (Coffee Link app).",
            self.device.dsn,
            self.device.connected_at or "unknown",
        )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="machine_offline",
            translation_placeholders={"name": self.device.name or self.device.dsn},
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

    @property
    def own_cloud_app_id(self) -> int:
        """The cloud-session id this integration registers for this machine."""
        return self._own_app_id()

    @property
    def uses_device_cloud_app_id(self) -> bool:
        """True when the session id is the machine's own signature, not the fallback."""
        return self._device_app_id is not None

    def _own_app_id(self) -> int:
        """Our session id: the machine's signature when known, else the constant."""
        if self._device_app_id is not None:
            return self._device_app_id
        return self._default_app_id

    def _refresh_device_app_id(self) -> None:
        """Re-derive the machine's cloud id from the learned frames.

        An ECAM only executes commands from a session registered with its own
        4-byte device signature; a session opened with the generic constant is
        accepted by Ayla, confirmed on ``app_id``, and then silently ignored by
        the machine (issue #15). Every frame the official app sends carries that
        signature, so any learned frame - including one restored from disk at
        startup - yields it. Called after loading and after each new capture.
        """
        new_id = app_id_from_signature(self._learned_device_signature())
        if new_id == self._device_app_id:
            return
        previous_own = self._own_app_id()
        self._device_app_id = new_id
        own = self._own_app_id()
        if own == previous_own:
            return
        _LOGGER.info(
            "Cloud session id for dsn=%s is now %s (%d / 0x%08x)",
            self.device.dsn,
            "the machine's own signature" if new_id is not None else "the default constant",
            own,
            own & 0xFFFFFFFF,
        )
        # Only rebind when we were using our own id: an adopted foreign session
        # must keep riding the app's id until it is released.
        if self._integration_app_id == previous_own:
            self._integration_app_id = own
            self._last_connect_at = 0
            self._session_confirmed = False

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

    async def _wait_for_session_confirmed(self, want_app_id: int | None = None) -> bool:
        """Poll app_id until it matches the id we registered (DlghIoT connect loop).

        ``want_app_id`` is the id actually POSTed; it is passed explicitly so a
        concurrent re-derivation of the machine's own id (a frame learned by the
        poll while this connect is in flight) cannot make the loop wait for an id
        that was never registered.
        """
        want = self._integration_app_id if want_app_id is None else want_app_id
        started = time.time()
        last_progress = started
        poll_count = 0
        cloud_errors = 0
        _LOGGER.debug(
            "Waiting for cloud session confirm on dsn=%s (timeout=%ds, want app_id=%d)",
            self.device.dsn,
            CONNECT_CONFIRM_TIMEOUT,
            want,
        )
        while time.time() - started < CONNECT_CONFIRM_TIMEOUT:
            poll_count += 1
            app_id, fetch_ok = await self._fetch_app_id_live()
            if not fetch_ok:
                cloud_errors += 1
            elif app_id == want:
                elapsed = time.time() - started
                self._session_confirmed = want == self._integration_app_id
                _LOGGER.info(
                    "Cloud session confirmed app_id=%d (0x%08x) on dsn=%s after %.1fs "
                    "(polls=%d, cloud_errors=%d)",
                    app_id,
                    app_id & 0xFFFFFFFF,
                    self.device.dsn,
                    elapsed,
                    poll_count,
                    cloud_errors,
                )
                return True
            await asyncio.sleep(CONNECT_CONFIRM_POLL_INTERVAL)
            now = time.time()
            if now - last_progress >= 15:
                _LOGGER.debug(
                    "Still waiting for cloud session confirm on dsn=%s (%.0fs/%ds, "
                    "app_id=%s, want=%d, polls=%d, cloud_errors=%d)",
                    self.device.dsn,
                    now - started,
                    CONNECT_CONFIRM_TIMEOUT,
                    app_id if fetch_ok else "fetch_failed",
                    want,
                    poll_count,
                    cloud_errors,
                )
                last_progress = now
        last_app_id, last_ok = await self._fetch_app_id_live()
        _LOGGER.warning(
            "Connect POST sent but app_id not confirmed after %ds on dsn=%s "
            "(last app_id=%s, want=%d, polls=%d, cloud_errors=%d)",
            CONNECT_CONFIRM_TIMEOUT,
            self.device.dsn,
            last_app_id if last_ok else "fetch_failed",
            want,
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
                elif app_id == self._own_app_id():
                    holder = "ha"
                else:
                    holder = "foreign"
                unsigned = (app_id or 0) & 0xFFFFFFFF
                _LOGGER.debug(
                    "Cloud session app_id=%s (0x%08x) holder=%s dsn=%s",
                    app_id if app_id is not None else "None",
                    unsigned,
                    holder,
                    self.device.dsn,
                )
                self._last_seen_app_id = app_id
            self._session_confirmed = (
                app_id is not None and app_id == self._integration_app_id
            )
            # An adopted foreign session (official app's id) is transient: once
            # the machine reports no session holder, revert to our own id so we
            # never keep a foreign session alive on the app's behalf.
            if app_id == 0 and self._integration_app_id != self._own_app_id():
                _LOGGER.info(
                    "Foreign cloud session released on dsn=%s; reverting to own app_id",
                    self.device.dsn,
                )
                self._integration_app_id = self._own_app_id()
                self._last_connect_at = 0
        except Exception:  # noqa: BLE001 - diagnostic must not break polling
            _LOGGER.debug("Session parse failed (non-fatal)", exc_info=True)

    def _revert_foreign_app_id_if_session_clear(self, app_id: int | None) -> None:
        """Before a cold POST, use our own cloud id when no session is held."""
        if app_id in (None, 0) and self._integration_app_id != self._own_app_id():
            _LOGGER.info(
                "No cloud session holder on dsn=%s; reverting to own app_id before connect",
                self.device.dsn,
            )
            self._integration_app_id = self._own_app_id()
            self._last_connect_at = 0

    async def _send_property_command(self, value: str, label: str) -> None:
        prop = self.command_property or COMMAND_PROPERTY_CANDIDATES[0]
        self._record_sent(value)
        _LOGGER.info("Sending %s via %s (len=%d)", label, prop, len(value))
        _LOGGER.debug("Sending %s via %s: %s", label, prop, value)
        await self.client.async_set_property_value(self.device.dsn, prop, value)
        await self.async_request_refresh()

    async def _maybe_send_session_refresh(self) -> None:
        """DlghIoT refresh(): nudge deep standby before wake when monitor=0."""
        if self.monitor.get("status") != 0:
            return
        value = build_session_refresh_encoded(self._integration_app_id)
        _LOGGER.info(
            "Machine in standby; sending session refresh before wake (tail app_id=%d)",
            self._integration_app_id,
        )
        await self._send_property_command(value, "SESSION REFRESH")

    def _wake_command_value(self) -> str:
        """Build wake frame for ECAM models (session tail). Soul uses main inline path."""
        value = build_wake_with_session_tail_encoded(self._integration_app_id)
        _LOGGER.debug(
            "Wake frame session tail app_id=%d (0x%08x)",
            self._integration_app_id,
            self._integration_app_id & 0xFFFFFFFF,
        )
        return value

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

    async def _post_cloud_session(self) -> int | None:
        """Register the cloud session; returns the app id actually POSTed."""
        if not self.connected_property:
            return None
        app_id = self._integration_app_id
        await self.client.async_post_cloud_session(
            self.device.dsn,
            self.connected_property,
            app_id,
        )
        return app_id

    async def _cold_connect_then(
        self, send_fn: Callable[[], Awaitable[None]]
    ) -> None:
        try:
            # The lock serializes concurrent cold connects: the first task does
            # the POST + settle, the ones queued behind it find the session
            # fresh and go straight to their send. No command is ever dropped.
            async with self._session_connect_lock:
                if not self._session_is_fresh(None):
                    app_id = await self._read_app_id(live=True)
                    self._revert_foreign_app_id_if_session_clear(app_id)
                    _LOGGER.debug(
                        "Cold cloud session connect starting dsn=%s (app_id before=%s)",
                        self.device.dsn,
                        app_id,
                    )
                    posted_app_id = await self._post_cloud_session()
                    await asyncio.sleep(CONNECT_SETTLE_DELAY)
                    if not await self._wait_for_session_confirmed(posted_app_id):
                        return
                    self._last_connect_at = time.time()
                elif not self._session_confirmed:
                    app_id, fetch_ok = await self._fetch_app_id_live()
                    if not fetch_ok or app_id != self._integration_app_id:
                        _LOGGER.warning(
                            "Warm session cache but app_id=%s (fetch_ok=%s) != %d on dsn=%s; "
                            "skipping command",
                            app_id,
                            fetch_ok,
                            self._integration_app_id,
                            self.device.dsn,
                        )
                        return
            # The session handshake can take minutes (CONNECT_CONFIRM_TIMEOUT),
            # so the reachability checked when the command was queued may no
            # longer hold. Re-check at the point of writing.
            self._ensure_machine_reachable()
            await send_fn()
        except Exception:  # noqa: BLE001 - strict: do not send after connect failure
            _LOGGER.warning(
                "Cold cloud session connect failed for dsn=%s; command not sent",
                self.device.dsn,
                exc_info=True,
            )
        finally:
            self._session_cold_task = None

    async def _with_cloud_session(
        self, send_fn: Callable[[], Awaitable[None]]
    ) -> None:
        if not self.profile.uses_cloud_session or not self.connected_property:
            await send_fn()
            return

        app_id = await self._read_app_id()
        self._revert_foreign_app_id_if_session_clear(app_id)

        # When Coffee Link already holds the cloud session (app_id != 0 and != ours),
        # we adopt its app_id so HA commands ride the same session instead of fighting
        # the official app. The adoption is TRANSIENT: we never POST a foreign
        # session, and we revert to our own id once the app releases it (see
        # _update_session_from_props). If the user opens Coffee Link while HA
        # is commanding, behaviour is undefined (the app may hold a LAN lock);
        # close the app first.
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

        if self._session_cold_task and not self._session_cold_task.done():
            _LOGGER.warning(
                "Cloud session connect already in progress for dsn=%s; "
                "ignoring duplicate command (confirm timeout=%ds)",
                self.device.dsn,
                CONNECT_CONFIRM_TIMEOUT,
            )
            return

        _LOGGER.debug(
            "Scheduling cloud session connect for dsn=%s (confirm timeout=%ds)",
            self.device.dsn,
            CONNECT_CONFIRM_TIMEOUT,
        )
        # Each command gets its own task; the connect lock serializes them, so
        # commands pressed during a cold connect are queued, not dropped.
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
        if self.learned_wake_frame is not None:
            if self.profile.learns_from_app:
                if not validate_replayed_wake_frame(
                    replay_with_timestamp(self.learned_wake_frame)
                ):
                    _LOGGER.warning(
                        "Discarding persisted wake frame (integrity check failed): %s. "
                        "Power the machine on once from the official app to re-learn it.",
                        self.learned_wake_frame,
                    )
                    self.learned_wake_frame = None
            elif not is_wake_power_frame(decode_command(self.learned_wake_frame)):
                _LOGGER.warning(
                    "Discarding persisted wake frame (not a real power-on): %s. "
                    "Power the machine on once from the official app to re-learn it.",
                    self.learned_wake_frame,
                )
                self.learned_wake_frame = None
        # A restored frame carries the device signature, so the machine's own
        # cloud id is known again before any new capture - a clean restart with
        # the official app closed commands the machine straight away (issue #15).
        self._refresh_device_app_id()
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
            "catalogue: %s\n%s\n=== recipe datapoint dump END ===",
            self.device.dsn,
            len(lines),
            catalog_summary(self.catalog),
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
                self._refresh_device_app_id()
            return

        # The machine's own declared ids widen the gate past the hardcoded list,
        # so a saved recipe ("Perso 1", ids 0xe6-0xeb) or a bean-system drink
        # brewed from the official app is captured instead of discarded.
        bev_id = learnable_beverage_id(decoded, catalog_beverage_ids(self.catalog))
        if bev_id is None:
            if ftype == "beverage":
                _LOGGER.debug(
                    "Not learning captured beverage frame (id=%s, crc_valid=%s): "
                    "beverage unknown to both the integration and the machine "
                    "catalogue, or invalid checksum",
                    decoded.get("beverage_id"),
                    decoded.get("crc_valid"),
                )
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
            self._refresh_device_app_id()

    async def async_send_beverage(self, beverage_id: int, action: int) -> None:
        """Build + send a beverage command via the resolved command property."""
        from .command_builder import build_and_encode

        self._ensure_machine_reachable()

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
        self._ensure_machine_reachable()
        if not self.profile.uses_cloud_session:

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
            return

        async def _do() -> None:
            await self._maybe_send_session_refresh()
            value = self._wake_command_value()
            await self._send_property_command(value, "WAKE cmd")

        await self._with_cloud_session(_do)

    def _learned_device_signature(self) -> bytes | None:
        """The 4-byte per-device signature carried by learned app frames (the
        wake frame first, else any learned beverage frame)."""
        return first_device_signature(
            (
                self.learned_wake_frame,
                *self.learned_start_frames.values(),
                *self.learned_stop_frames.values(),
            )
        )

    async def async_send_standby(self) -> None:
        """Send the STANDBY / power-off command (84 0f, params 01 01).

        Always synthesized - the official app has no power-off control to
        capture. Validated live on the reference Soul; on learn-and-replay
        models the per-device signature from a learned frame is appended.
        """
        self._ensure_machine_reachable()
        if not self.profile.uses_cloud_session:

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
            return

        async def _do() -> None:
            value = self._standby_command_value()
            await self._send_property_command(value, "STANDBY cmd")

        await self._with_cloud_session(_do)

    async def async_send_raw(self, value: str) -> None:
        """Send a raw base64 command on the resolved command channel (advanced).

        Deliberately NOT gated by the reachability preflight. This is the
        field-instrumentation escape hatch, and the one thing it must keep doing
        is letting a maintainer poke a machine when the integration's own idea of
        its state is what is wrong. It warns instead of refusing.
        """
        if self.machine_is_offline:
            _LOGGER.warning(
                "Sending a raw command to dsn=%s while the cloud reports it Offline "
                "(last connected: %s). The cloud will accept the write and the "
                "machine will most likely never receive it.",
                self.device.dsn,
                self.device.connected_at or "unknown",
            )

        async def _do() -> None:
            self._record_sent(value)
            prop = self.command_property or COMMAND_PROPERTY_CANDIDATES[0]
            _LOGGER.info("Sending RAW cmd via %s: %s", prop, value)
            await self.client.async_set_property_value(self.device.dsn, prop, value)
            await self.async_request_refresh()

        await self._with_cloud_session(_do)


async def async_send_to_all(
    coordinators: list[DelonghiCoordinator],
    send: Callable[[DelonghiCoordinator], Awaitable[None]],
) -> None:
    """Run one command on every machine, then report the first failure.

    A service call addresses every machine of the config entry, so one machine
    failing - unreachable, a cloud 5xx, an expired token - must not swallow the
    others: every coordinator is attempted, and the first exception is re-raised
    afterwards so the caller still learns something did not go through.
    """
    errors: list[Exception] = []
    for coord in coordinators:
        try:
            await send(coord)
        except Exception as err:  # noqa: BLE001 - re-raised below, after the fan-out
            errors.append(err)
    if not errors:
        return
    # The first error is re-raised, so Home Assistant already surfaces it; only
    # the ones it would hide are worth a log line of their own.
    for err in errors[1:]:
        _LOGGER.warning("A further machine did not get the command: %s", err)
    raise errors[0]
