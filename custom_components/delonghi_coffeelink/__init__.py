"""DeLonghi Coffee Link integration (PrimaDonna Soul et autres modeles Ayla-based)."""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_AREA_ID,
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    ATTR_FLOOR_ID,
    ATTR_LABEL_ID,
    Platform,
)
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service import async_register_admin_service

from .ayla_client import AuthError, CloudError, DelonghiAylaClient
from .const import (
    ACTION_START,
    ACTION_STOP,
    BEVERAGES,
    CONF_EMAIL,
    CONF_PASSWORD,
    DOMAIN,
    SERVICE_SEND_RAW_COMMAND,
    SERVICE_START_BEVERAGE,
    SERVICE_STOP_BEVERAGE,
)
from .coordinator import DelonghiCoordinator, async_send_to_each

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
]

BEVERAGE_KEYS = [b[1] for b in BEVERAGES]

# The target is OPTIONAL on purpose, and it is NOT a free pass. With one machine
# set up it is used, so single-machine automations - almost all of them - keep
# working untouched. With several machines and no target, the call is refused by
# name (see _resolve_targets); that IS a behaviour change for multi-machine
# accounts, and a deliberate one: the previous answer to the ambiguity was to
# brew on every machine, which is the one outcome nobody can undo.
#
# Every target key Home Assistant can put in `data` is declared here, even the
# ones only expanded further down. These are plain services, not entity
# services, so HA passes the whole target block through into `call.data` and
# validates it against this schema - and vol.Schema defaults to PREVENT_EXTRA.
# Declaring `target:` in services.yaml while accepting only device_id would make
# HA reject `area_id`/`entity_id` with "extra keys not allowed" before any
# handler ran, on a target its own UI offered.
_TARGET_SCHEMA = {
    vol.Optional(key): vol.All(cv.ensure_list, [cv.string])
    for key in (ATTR_DEVICE_ID, ATTR_ENTITY_ID, ATTR_AREA_ID, ATTR_FLOOR_ID, ATTR_LABEL_ID)
}

SERVICE_START_SCHEMA = vol.Schema(
    {vol.Required("beverage"): vol.In(BEVERAGE_KEYS), **_TARGET_SCHEMA}
)
SERVICE_STOP_SCHEMA = vol.Schema(
    {vol.Required("beverage"): vol.In(BEVERAGE_KEYS), **_TARGET_SCHEMA}
)
SERVICE_RAW_SCHEMA = vol.Schema(
    {vol.Required("value_base64"): cv.string, **_TARGET_SCHEMA}
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    client = DelonghiAylaClient(session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])

    try:
        await client.async_authenticate()
        devices = await client.async_get_devices()
    except AuthError as err:
        # Refused credentials must NOT be retried: ConfigEntryNotReady retries
        # forever, one full password login each time, until Gigya locks the
        # account. ConfigEntryAuthFailed stops and asks the user to sign in.
        if not err.transient:
            raise ConfigEntryAuthFailed(f"DeLonghi sign-in refused: {err}") from err
        raise ConfigEntryNotReady(f"DeLonghi auth failed: {err}") from err
    except CloudError as err:
        raise ConfigEntryNotReady(f"DeLonghi cloud error: {err}") from err

    if not devices:
        raise ConfigEntryNotReady("No DeLonghi devices found on this account")

    for device in devices:
        _LOGGER.debug(
            "Discovered DeLonghi device: dsn=%s oem_model=%s model=%s sw_version=%s "
            "connection_status=%s lan_ip=%s",
            device.dsn,
            device.oem_model,
            device.model,
            device.sw_version,
            device.connection_status,
            device.lan_ip,
        )

    # One coordinator per device - except devices the user disabled in Home
    # Assistant: those are not polled at all (an old registration on the
    # account otherwise costs a full poll and a keepalive every 30 s). Disabling
    # a device disables its entities, which makes HA reload this entry.
    device_registry = dr.async_get(hass)
    coordinators: list[DelonghiCoordinator] = []
    for device in devices:
        registered = device_registry.async_get_device({(DOMAIN, device.dsn)})
        if registered is not None and registered.disabled:
            _LOGGER.info("Skipping dsn=%s: device disabled in Home Assistant", device.dsn)
            continue
        coord = DelonghiCoordinator(hass, client, device, entry)
        # Restore any Eletta frames learned in previous runs before the first
        # refresh, so buttons can replay immediately after a restart.
        await coord.async_load_learned()
        await coord.async_config_entry_first_refresh()
        if coord.data:
            prop_names = sorted(coord.data.keys())
            _LOGGER.debug(
                "Ayla properties for dsn=%s (%d total): %s",
                device.dsn,
                len(prop_names),
                ", ".join(prop_names),
            )
        coordinators.append(coord)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinators

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # The coordinators had to be published before the forward - the platforms
        # read them during their own setup. If the forward fails, unload is never
        # called for this entry, so nothing else would ever clear them, and the
        # ghosts would be counted as machines by every later service call.
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise

    _register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # Popped BEFORE the shutdown, not after: the service handlers resolve
        # their targets from hass.data live, so a call landing during the await
        # would otherwise reach a coordinator whose session is already closed
        # and fail with whatever the client raises - not a HomeAssistantError,
        # so not a message anybody can read.
        coordinators = hass.data.get(DOMAIN, {}).pop(entry.entry_id, [])
        for coord in coordinators:
            if coord.profile.uses_cloud_session:
                await coord.async_shutdown()
    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Let the user delete a device this entry no longer serves.

    Allowed only for a machine that is not loaded right now - removed from the
    De'Longhi account, or disabled - so a live machine cannot be deleted from
    under its own entities. Without this hook HA offers no delete at all, and a
    machine taken off the account stays in the device list forever.
    """
    loaded = {coord.device.dsn for coord in hass.data.get(DOMAIN, {}).get(entry.entry_id, [])}
    return not any(
        domain == DOMAIN and ident in loaded for domain, ident in device_entry.identifiers
    )


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the integration services once the last account is gone.

    Removal belongs HERE rather than in async_unload_entry. A reload unloads the
    entry too, and the entry is still listed at that moment, so unload cannot
    tell a reload from a deletion. Removing there meant that a reload whose setup
    then failed - ConfigEntryNotReady on a cloud 5xx or an auth flap, both
    routine on this integration - left the three services missing for the whole
    retry backoff, and automations failed with "Service not found" instead of the
    translated machine error they are written against.

    By the time Home Assistant calls this, the entry is already out of the
    registry, so an empty list means the integration itself is going away.
    """
    if hass.config_entries.async_entries(DOMAIN):
        return
    for service in (
        SERVICE_START_BEVERAGE,
        SERVICE_STOP_BEVERAGE,
        SERVICE_SEND_RAW_COMMAND,
    ):
        hass.services.async_remove(DOMAIN, service)


def _all_coordinators(hass: HomeAssistant) -> list[DelonghiCoordinator]:
    """Every machine currently set up, across ALL config entries, once each.

    Read at call time rather than captured: the handlers used to close over one
    entry's list, which is half of why a two-entry account could not reach its
    first entry's machines at all.

    Deduplicated on the DSN, because the device registry is. Two entries over the
    same account produce two coordinators per machine but ONE device, so without
    this a single machine set up twice would be counted as several and the user
    told to choose between "Soul" and "Soul".

    `isinstance` guards the flatten: this walks whatever lives under
    hass.data[DOMAIN], and the day something that is not a list of coordinators
    is stored there - a shared client, an unsub callable - a service call must
    not be what discovers it.
    """
    seen: dict[str, DelonghiCoordinator] = {}
    for coords in hass.data.get(DOMAIN, {}).values():
        if not isinstance(coords, list):
            continue
        for coord in coords:
            seen.setdefault(coord.device.dsn, coord)
    return list(seen.values())


def _named_devices(hass: HomeAssistant, call: ServiceCall) -> set[str] | None:
    """The device ids this call points at, or None if it points at nothing.

    None and the empty set mean different things, which is the reason for the
    return type: None is "no target was given" (fall back to the lone machine),
    an empty set is "a target was given and it matched no device" (refuse - the
    user asked for something specific and it is not there).

    Areas and entities are expanded here rather than refused. HA offers them in
    the target picker of any service that declares `target:`, so a call arriving
    with `area_id: kitchen` is the UI working as designed, not misuse.
    """
    data = call.data
    if not any(
        data.get(key)
        for key in (ATTR_DEVICE_ID, ATTR_ENTITY_ID, ATTR_AREA_ID, ATTR_FLOOR_ID, ATTR_LABEL_ID)
    ):
        return None

    devices: set[str] = set(data.get(ATTR_DEVICE_ID) or [])
    entity_ids = set(data.get(ATTR_ENTITY_ID) or [])
    areas = set(data.get(ATTR_AREA_ID) or [])

    entities = er.async_get(hass)
    for entity_id in entity_ids:
        entry = entities.async_get(entity_id)
        if entry and entry.device_id:
            devices.add(entry.device_id)

    if areas:
        registry = dr.async_get(hass)
        devices.update(
            device.id for device in registry.devices.values() if device.area_id in areas
        )
        # An entity can override its device's area, so an area can hold a machine
        # whose device record points elsewhere.
        devices.update(
            entry.device_id
            for entry in entities.entities.values()
            if entry.area_id in areas and entry.device_id
        )
    return devices


def _resolve_targets(hass: HomeAssistant, call: ServiceCall) -> list[DelonghiCoordinator]:
    """Which machines this call is for.

    Devices are matched on the DSN, which is what every platform already uses as
    its device-registry identifier (`{(DOMAIN, dsn)}`).

    Refusing is the point of this function. The previous behaviour sent a call to
    every machine of one config entry, so one espresso brewed a cup on each of
    them, silently - and the machines of any OTHER entry got nothing at all,
    since the second registration replaced the first entry's handlers.
    """
    known = _all_coordinators(hass)
    if not known:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="no_machine_configured"
        )
    machines = ", ".join(sorted(_label(hass, coord) for coord in known))

    device_ids = _named_devices(hass, call)
    if device_ids is None:
        if len(known) == 1:
            return known
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="target_required",
            translation_placeholders={"machines": machines},
        )

    by_dsn = {coord.device.dsn: coord for coord in known}
    registry = dr.async_get(hass)
    targets: list[DelonghiCoordinator] = []
    for device_id in sorted(device_ids):
        entry = registry.async_get(device_id)
        # `identifiers` is a set, so sort it: after a device merge an entry can
        # carry two of ours, and the machine picked must not vary between runs.
        dsns = sorted(
            ident
            for domain, ident in (entry.identifiers if entry else ())
            if domain == DOMAIN
        )
        coord = next((by_dsn[dsn] for dsn in dsns if dsn in by_dsn), None)
        if coord is not None:
            if coord not in targets:
                targets.append(coord)
            continue
        if dsns:
            # Ours, but its config entry is not loaded right now (disabled,
            # reloading, failed setup). Telling this user their own machine is
            # "not a De'Longhi machine" would send them hunting the wrong bug.
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="machine_unavailable",
                translation_placeholders={
                    "device": _device_label(entry, device_id),
                    "machines": machines,
                },
            )
        if device_id in (call.data.get(ATTR_DEVICE_ID) or []):
            # Named outright, and it is not one of ours.
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="target_not_a_machine",
                translation_placeholders={
                    "device": _device_label(entry, device_id),
                    "machines": machines,
                },
            )
        # Swept in by an area or an entity: not an error on its own, the target
        # simply covered more than the machines. Only an EMPTY result is refused.

    if not targets:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="target_matched_no_machine",
            translation_placeholders={"machines": machines},
        )
    return targets


def _label(hass: HomeAssistant, coord: DelonghiCoordinator) -> str:
    """How a machine is named back to the user in an error.

    The device registry first, because that is the name the target picker shows.
    A user who renamed their machine to "Kitchen" must not be handed a list of
    cloud names ("Eletta Explore") they cannot find anywhere in the UI.
    """
    device = dr.async_get(hass).async_get_device({(DOMAIN, coord.device.dsn)})
    if device is not None:
        named = device.name_by_user or device.name
        if named:
            return named
    return coord.device.name or coord.device.dsn


def _device_label(entry, device_id: str) -> str:
    """How a device-registry entry is named back to the user.

    Falls back to the raw id: ugly in a toast, but it is the only handle the
    user can act on when the registry has nothing.
    """
    if entry is None:
        return device_id
    return entry.name_by_user or entry.name or device_id


def _register_services(hass: HomeAssistant) -> None:
    """Register the integration services, once, whatever the entry count.

    Idempotent per service rather than gated on one of the three: probing only
    `start_beverage` would skip re-registering the other two if they ever went
    missing on their own.
    """
    # Build lookup for beverage_id by key
    bev_by_key = {b[1]: b[0] for b in BEVERAGES}

    async def _start_beverage(call: ServiceCall) -> None:
        bev_id = bev_by_key[call.data["beverage"]]
        await async_send_to_each(
            _resolve_targets(hass, call),
            lambda coord: coord.async_send_beverage(bev_id, ACTION_START),
        )

    async def _stop_beverage(call: ServiceCall) -> None:
        bev_id = bev_by_key[call.data["beverage"]]
        await async_send_to_each(
            _resolve_targets(hass, call),
            lambda coord: coord.async_send_beverage(bev_id, ACTION_STOP),
        )

    async def _send_raw(call: ServiceCall) -> None:
        value = call.data["value_base64"]
        await async_send_to_each(
            _resolve_targets(hass, call),
            lambda coord: coord.async_send_raw(value),
        )

    # Registered for the integration, not per entry: the names are global, so a
    # second entry used to overwrite the first one's handlers and take its
    # machines out of reach entirely.
    for service, handler, schema in (
        (SERVICE_START_BEVERAGE, _start_beverage, SERVICE_START_SCHEMA),
        (SERVICE_STOP_BEVERAGE, _stop_beverage, SERVICE_STOP_SCHEMA),
    ):
        if not hass.services.has_service(DOMAIN, service):
            hass.services.async_register(DOMAIN, service, handler, schema=schema)
    # Admin-only: a raw frame can do anything the protocol can - write machine
    # settings, the PIN, recipes - so a non-admin Home Assistant user must not
    # be able to send one. Brewing stays open to every user.
    if not hass.services.has_service(DOMAIN, SERVICE_SEND_RAW_COMMAND):
        async_register_admin_service(
            hass, DOMAIN, SERVICE_SEND_RAW_COMMAND, _send_raw, schema=SERVICE_RAW_SCHEMA
        )
