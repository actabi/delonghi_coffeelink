"""A service call must reach the machine it was aimed at, and no other.

The failure this pins, which the code used to carry as a NOTE rather than a
fix: the three services were registered from inside ``async_setup_entry``,
closing over that entry's coordinators, and every handler fanned the call out
over the whole list. Two consequences, both live bugs on an account with more
than one machine:

* asking for one coffee brewed one on *every* machine, silently;
* with two config entries the second registration overwrote the first, so the
  first entry's machines could not be commanded at all.

Everything here runs against stubs - no Home Assistant, no cloud, no machine.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

COMPONENT_DIR = (
    Path(__file__).resolve().parents[1] / "custom_components" / "delonghi_coffeelink"
)


class _StubHomeAssistantError(Exception):
    def __init__(self, *args, translation_domain=None, translation_key=None,
                 translation_placeholders=None) -> None:
        super().__init__(*args)
        self.translation_key = translation_key
        self.translation_placeholders = translation_placeholders or {}


def _install_stubs() -> None:
    """Just enough of Home Assistant to import the package.

    voluptuous is NOT stubbed, deliberately: it is the real library, so the
    service schemas built at import time are the real ones and can be exercised
    below. A stubbed `vol.Schema` returning None would make every schema test
    tautological - the very failure that let `target:` ship in services.yaml
    while the schema rejected every target key but one.
    """
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.ServiceCall = object
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.HomeAssistantError = _StubHomeAssistantError
    exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})
    exceptions.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})
    exceptions.ConfigEntryError = type("ConfigEntryError", (Exception,), {})
    storage = types.ModuleType("homeassistant.helpers.storage")
    storage.Store = object
    upd = types.ModuleType("homeassistant.helpers.update_coordinator")
    upd.DataUpdateCoordinator = type(
        "DataUpdateCoordinator",
        (),
        {"__init__": lambda self, *a, **k: None,
         "__class_getitem__": classmethod(lambda cls, item: cls)},
    )
    upd.CoordinatorEntity = object
    upd.UpdateFailed = type("UpdateFailed", (Exception,), {})
    ha_const = types.ModuleType("homeassistant.const")
    ha_const.ATTR_AREA_ID = "area_id"
    ha_const.ATTR_DEVICE_ID = "device_id"
    ha_const.ATTR_ENTITY_ID = "entity_id"
    ha_const.ATTR_FLOOR_ID = "floor_id"
    ha_const.ATTR_LABEL_ID = "label_id"
    ha_const.EntityCategory = types.SimpleNamespace(DIAGNOSTIC="diagnostic")
    ha_const.Platform = types.SimpleNamespace(
        SENSOR="sensor", BINARY_SENSOR="binary_sensor", BUTTON="button"
    )
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    cv = types.ModuleType("homeassistant.helpers.config_validation")
    cv.string = str
    # The real helper wraps a scalar; `list` would explode a string into its
    # characters, which only looked harmless while the schema was never run.
    cv.ensure_list = lambda value: value if isinstance(value, list) else [value]
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = dict
    device_registry.async_get = lambda hass: hass.device_registry
    entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
    entity_registry.async_get = lambda hass: hass.entity_registry
    aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda hass: None
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.config_validation = cv
    helpers.device_registry = device_registry
    helpers.entity_registry = entity_registry
    for name, mod in (
        ("homeassistant", types.ModuleType("homeassistant")),
        ("homeassistant.config_entries", config_entries),
        ("homeassistant.const", ha_const),
        ("homeassistant.core", core),
        ("homeassistant.exceptions", exceptions),
        ("homeassistant.helpers", helpers),
        ("homeassistant.helpers.aiohttp_client", aiohttp_client),
        ("homeassistant.helpers.config_validation", cv),
        ("homeassistant.helpers.device_registry", device_registry),
        ("homeassistant.helpers.entity_registry", entity_registry),
        ("homeassistant.helpers.storage", storage),
        ("homeassistant.helpers.update_coordinator", upd),
    ):
        sys.modules[name] = mod


_install_stubs()

_PKG = "delonghi_targeting_under_test"
_package = types.ModuleType(_PKG)
_package.__path__ = [str(COMPONENT_DIR)]
sys.modules[_PKG] = _package


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        f"{_PKG}.{name}", COMPONENT_DIR / filename
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{_PKG}.{name}"] = module
    spec.loader.exec_module(module)
    return module


for _name in ("const", "command_builder", "monitor", "catalog", "model_profiles",
              "ayla_client", "coordinator"):
    try:
        _load(_name, f"{_name}.py")
    except FileNotFoundError:
        pass
dl = _load("__init__", "__init__.py")
const = sys.modules[f"{_PKG}.const"]


# --- stubs for the objects the handlers reach through ------------------------

class _FakeDevice:
    def __init__(self, dsn: str, name: str) -> None:
        self.dsn = dsn
        self.name = name


class _FakeCoordinator:
    """Records what it was asked to send, and never touches the network."""

    def __init__(self, dsn: str, name: str) -> None:
        self.device = _FakeDevice(dsn, name)
        self.sent: list[tuple] = []
        # Read by async_unload_entry to decide whether a session needs closing.
        self.profile = types.SimpleNamespace(uses_cloud_session=False)

    async def async_send_beverage(self, beverage_id: int, action: int) -> None:
        self.sent.append(("beverage", beverage_id, action))

    async def async_send_raw(self, value: str) -> None:
        self.sent.append(("raw", value))


class _RegistryEntry:
    def __init__(self, identifiers, name=None, name_by_user=None, area_id=None,
                 device_id=None) -> None:
        self.identifiers = identifiers
        self.name = name
        self.name_by_user = name_by_user
        self.area_id = area_id
        self.device_id = device_id


class _FakeDeviceRegistry:
    def __init__(self, entries: dict[str, _RegistryEntry]) -> None:
        self._entries = entries
        for device_id, entry in entries.items():
            entry.id = device_id

    @property
    def devices(self):
        return self._entries

    def async_get(self, device_id: str):
        return self._entries.get(device_id)

    def async_get_device(self, identifiers):
        return next(
            (e for e in self._entries.values() if e.identifiers & identifiers), None
        )


class _FakeEntityRegistry:
    """Entities carry a device_id and may override their device's area."""

    def __init__(self, entries: dict | None = None) -> None:
        self._entries = entries or {}

    @property
    def entities(self):
        return self._entries

    def async_get(self, entity_id: str):
        return self._entries.get(entity_id)


class _FakeServices:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.registrations = 0

    def async_register(self, domain, service, handler, schema=None) -> None:
        self.registrations += 1
        self.handlers[service] = handler

    def has_service(self, domain, service) -> bool:
        return service in self.handlers

    def async_remove(self, domain, service) -> None:
        self.handlers.pop(service, None)


class _FakeConfigEntries:
    """Only what async_unload_entry reads: which entries the integration has."""

    def __init__(self, entry_ids: list[str] | None = None) -> None:
        self.entry_ids = list(entry_ids or [])

    def async_entries(self, domain):
        return [types.SimpleNamespace(entry_id=eid) for eid in self.entry_ids]

    async def async_unload_platforms(self, entry, platforms):
        return True


class _FakeHass:
    def __init__(self) -> None:
        self.data: dict = {}
        self.services = _FakeServices()
        self.device_registry = _FakeDeviceRegistry({})
        self.entity_registry = _FakeEntityRegistry()
        self.config_entries = _FakeConfigEntries()


class _Call:
    def __init__(self, **data) -> None:
        self.data = data


def _hass_with(*entries: tuple[str, list[_FakeCoordinator]]) -> _FakeHass:
    """A hass holding the given config entries, each with its own machines."""
    hass = _FakeHass()
    hass.data[const.DOMAIN] = {entry_id: coords for entry_id, coords in entries}
    registry = {}
    for _entry_id, coords in entries:
        for coord in coords:
            registry[f"dev_{coord.device.dsn}"] = _RegistryEntry(
                {(const.DOMAIN, coord.device.dsn)}, name=coord.device.name
            )
    hass.device_registry = _FakeDeviceRegistry(registry)
    hass.config_entries = _FakeConfigEntries([entry_id for entry_id, _ in entries])
    return hass


def _machines(*names: str) -> list[_FakeCoordinator]:
    return [_FakeCoordinator(f"AC{i:03d}", name) for i, name in enumerate(names)]


# --- resolution --------------------------------------------------------------

def test_a_lone_machine_needs_no_target():
    """The common case, and the reason the target is optional.

    Almost every installation has one machine. Requiring a target would break
    every automation already written against these services, to guard a risk
    that does not exist below two machines.
    """
    soul, = _machines("PrimaDonna Soul")
    hass = _hass_with(("entry_1", [soul]))
    assert dl._resolve_targets(hass, _Call(beverage="espresso")) == [soul]


def test_an_untargeted_call_is_refused_when_there_are_several_machines():
    """The bug, stated as a test: this used to brew on every machine.

    Refusing is the point. A command with no stated target on a multi-machine
    account is ambiguous, and the previous answer to that ambiguity - send it
    everywhere - is the one answer that cannot be undone once the cups are full.
    """
    soul, eletta = _machines("PrimaDonna Soul", "Eletta Explore")
    hass = _hass_with(("entry_1", [soul, eletta]))

    with pytest.raises(_StubHomeAssistantError) as err:
        dl._resolve_targets(hass, _Call(beverage="espresso"))

    assert err.value.translation_key == "target_required"
    # The message has to name them, or the user cannot act on it.
    named = err.value.translation_placeholders["machines"]
    assert "PrimaDonna Soul" in named and "Eletta Explore" in named


def test_a_targeted_call_reaches_that_machine_only():
    soul, eletta = _machines("PrimaDonna Soul", "Eletta Explore")
    hass = _hass_with(("entry_1", [soul, eletta]))
    assert dl._resolve_targets(hass, _Call(device_id=["dev_AC001"])) == [eletta]


def test_several_targets_are_all_served_and_deduplicated():
    """Fan-out stays available - but only when it was asked for by name."""
    soul, eletta = _machines("PrimaDonna Soul", "Eletta Explore")
    hass = _hass_with(("entry_1", [soul, eletta]))
    targets = dl._resolve_targets(
        hass, _Call(device_id=["dev_AC000", "dev_AC001", "dev_AC000"])
    )
    assert targets == [soul, eletta]


def test_a_device_from_another_integration_is_refused():
    """A thermostat picked by mistake must not silently become "all machines"."""
    soul, = _machines("PrimaDonna Soul")
    hass = _hass_with(("entry_1", [soul]))
    hass.device_registry = _FakeDeviceRegistry(
        {"dev_thermostat": _RegistryEntry({("climate", "xyz")}, name="Thermostat")}
    )

    with pytest.raises(_StubHomeAssistantError) as err:
        dl._resolve_targets(hass, _Call(device_id=["dev_thermostat"]))

    assert err.value.translation_key == "target_not_a_machine"
    assert err.value.translation_placeholders["device"] == "Thermostat"
    assert "PrimaDonna Soul" in err.value.translation_placeholders["machines"]


def test_an_unknown_device_id_is_refused():
    soul, = _machines("PrimaDonna Soul")
    hass = _hass_with(("entry_1", [soul]))
    with pytest.raises(_StubHomeAssistantError) as err:
        dl._resolve_targets(hass, _Call(device_id=["dev_nonexistent"]))
    assert err.value.translation_key == "target_not_a_machine"


def test_no_machine_at_all_is_its_own_message():
    hass = _FakeHass()
    with pytest.raises(_StubHomeAssistantError) as err:
        dl._resolve_targets(hass, _Call(beverage="espresso"))
    assert err.value.translation_key == "no_machine_configured"


def test_a_machine_is_named_by_its_dsn_when_it_has_no_name():
    """The error must stay usable on a machine the cloud never named."""
    anonymous = _FakeCoordinator("AC999", "")
    hass = _hass_with(("entry_1", [anonymous, _FakeCoordinator("AC998", "Soul")]))
    with pytest.raises(_StubHomeAssistantError) as err:
        dl._resolve_targets(hass, _Call(beverage="espresso"))
    assert "AC999" in err.value.translation_placeholders["machines"]


# --- across config entries ---------------------------------------------------

def test_machines_of_every_entry_are_reachable():
    """The second half of the bug: handlers closed over ONE entry's list.

    Two accounts (two config entries) meant the second registration replaced the
    first, and the first entry's machines could not be commanded at all. The
    targets are looked up at call time now, over every entry.
    """
    soul, = _machines("PrimaDonna Soul")
    eletta = _FakeCoordinator("AC001", "Eletta Explore")
    hass = _hass_with(("entry_1", [soul]), ("entry_2", [eletta]))

    assert dl._all_coordinators(hass) == [soul, eletta]
    assert dl._resolve_targets(hass, _Call(device_id=["dev_AC000"])) == [soul]
    assert dl._resolve_targets(hass, _Call(device_id=["dev_AC001"])) == [eletta]


def test_a_machine_added_later_is_seen_without_re_registering():
    """Reading hass.data at call time is what makes one registration enough."""
    soul, = _machines("PrimaDonna Soul")
    hass = _hass_with(("entry_1", [soul]))
    dl._register_services(hass)

    eletta = _FakeCoordinator("AC001", "Eletta Explore")
    hass.data[const.DOMAIN]["entry_2"] = [eletta]
    hass.device_registry = _FakeDeviceRegistry(
        {"dev_AC000": _RegistryEntry({(const.DOMAIN, "AC000")}),
         "dev_AC001": _RegistryEntry({(const.DOMAIN, "AC001")})}
    )

    assert dl._resolve_targets(hass, _Call(device_id=["dev_AC001"])) == [eletta]


# --- the handlers themselves -------------------------------------------------

def _handler(hass, service):
    dl._register_services(hass)
    return hass.services.handlers[service]


def test_start_beverage_sends_to_the_target_and_to_nothing_else():
    """End to end through the registered handler - the regression that matters."""
    soul, eletta = _machines("PrimaDonna Soul", "Eletta Explore")
    hass = _hass_with(("entry_1", [soul, eletta]))
    handler = _handler(hass, const.SERVICE_START_BEVERAGE)

    asyncio.run(handler(_Call(beverage="espresso", device_id=["dev_AC000"])))

    assert soul.sent == [("beverage", const.BEVERAGES[0][0], const.ACTION_START)]
    assert eletta.sent == [], "the other machine must not have been touched"


def test_stop_beverage_is_targeted_too():
    soul, eletta = _machines("PrimaDonna Soul", "Eletta Explore")
    hass = _hass_with(("entry_1", [soul, eletta]))
    handler = _handler(hass, const.SERVICE_STOP_BEVERAGE)

    asyncio.run(handler(_Call(beverage="espresso", device_id=["dev_AC001"])))

    assert soul.sent == []
    assert eletta.sent == [("beverage", const.BEVERAGES[0][0], const.ACTION_STOP)]


def test_send_raw_command_stays_reachable_and_targeted():
    """The field-instrumentation escape hatch keeps working (v0.3.19).

    It is the one path never refused by the reachability preflight, so it must
    not become harder to use here either - it just has to say where it is going.
    """
    soul, eletta = _machines("PrimaDonna Soul", "Eletta Explore")
    hass = _hass_with(("entry_1", [soul, eletta]))
    handler = _handler(hass, const.SERVICE_SEND_RAW_COMMAND)

    asyncio.run(handler(_Call(value_base64="DQ2D8A==", device_id=["dev_AC001"])))

    assert soul.sent == []
    assert eletta.sent == [("raw", "DQ2D8A==")]


def test_an_untargeted_handler_call_raises_before_anything_is_sent():
    """The refusal must happen instead of the fan-out, not after part of it."""
    soul, eletta = _machines("PrimaDonna Soul", "Eletta Explore")
    hass = _hass_with(("entry_1", [soul, eletta]))
    handler = _handler(hass, const.SERVICE_START_BEVERAGE)

    with pytest.raises(_StubHomeAssistantError):
        asyncio.run(handler(_Call(beverage="espresso")))

    assert soul.sent == [] and eletta.sent == []


def test_the_three_services_are_registered_once():
    hass = _hass_with(("entry_1", _machines("PrimaDonna Soul")))
    dl._register_services(hass)
    assert hass.services.registrations == 3
    assert set(hass.services.handlers) == {
        const.SERVICE_START_BEVERAGE,
        const.SERVICE_STOP_BEVERAGE,
        const.SERVICE_SEND_RAW_COMMAND,
    }


# --- the schema, exercised for real ------------------------------------------
#
# voluptuous is not stubbed in this module, so these run the schemas the
# integration actually registers. They exist because services.yaml declaring
# `target:` and the schema accepting only `device_id` is a pair that fails only
# in production: HA merges the whole target block into `call.data` and validates
# it against a PREVENT_EXTRA schema, so a user who picked an area from the very
# picker this change added got `extra keys not allowed @ data['area_id']`.

_SCHEMAS = (
    ("start_beverage", dl.SERVICE_START_SCHEMA, {"beverage": "espresso"}),
    ("stop_beverage", dl.SERVICE_STOP_SCHEMA, {"beverage": "espresso"}),
    ("send_raw_command", dl.SERVICE_RAW_SCHEMA, {"value_base64": "DQ2D8A=="}),
)


@pytest.mark.parametrize("name,schema,payload", _SCHEMAS, ids=[s[0] for s in _SCHEMAS])
@pytest.mark.parametrize(
    "target_key", ["device_id", "entity_id", "area_id", "floor_id", "label_id"]
)
def test_every_target_key_home_assistant_can_send_is_accepted(
    name, schema, payload, target_key
):
    """Whatever the target picker produces must survive validation."""
    schema({**payload, target_key: ["something"]})


@pytest.mark.parametrize("name,schema,payload", _SCHEMAS, ids=[s[0] for s in _SCHEMAS])
def test_a_bare_string_target_is_accepted_like_a_list(name, schema, payload):
    """YAML automations write `device_id: abc`, not `device_id: [abc]`."""
    assert schema({**payload, "device_id": "abc"})["device_id"] == ["abc"]


@pytest.mark.parametrize("name,schema,payload", _SCHEMAS, ids=[s[0] for s in _SCHEMAS])
def test_the_target_stays_optional(name, schema, payload):
    """A single-machine automation written before this change must still pass."""
    schema(dict(payload))


def test_the_beverage_is_still_validated():
    """Guard the guard: an over-permissive schema would pass everything above."""
    import voluptuous as vol

    with pytest.raises(vol.Invalid):
        dl.SERVICE_START_SCHEMA({"beverage": "not_a_drink"})
    with pytest.raises(vol.Invalid):
        dl.SERVICE_START_SCHEMA({"device_id": ["abc"]})  # beverage is required


# --- areas and entities resolve to machines ----------------------------------

def test_an_entity_target_resolves_to_its_machine():
    soul, eletta = _machines("PrimaDonna Soul", "Eletta Explore")
    hass = _hass_with(("entry_1", [soul, eletta]))
    hass.entity_registry = _FakeEntityRegistry(
        {"button.soul_espresso": _RegistryEntry(set(), device_id="dev_AC000")}
    )
    targets = dl._resolve_targets(
        hass, _Call(beverage="espresso", entity_id=["button.soul_espresso"])
    )
    assert targets == [soul]


def test_an_area_target_resolves_to_the_machines_in_it():
    soul, eletta = _machines("PrimaDonna Soul", "Eletta Explore")
    hass = _hass_with(("entry_1", [soul, eletta]))
    hass.device_registry.devices["dev_AC000"].area_id = "kitchen"
    hass.device_registry.devices["dev_AC001"].area_id = "office"
    targets = dl._resolve_targets(hass, _Call(beverage="espresso", area_id=["kitchen"]))
    assert targets == [soul]


def test_an_entity_overriding_its_area_is_followed():
    """An entity can sit in an area its device does not."""
    soul, = _machines("PrimaDonna Soul")
    hass = _hass_with(("entry_1", [soul]))
    hass.device_registry.devices["dev_AC000"].area_id = "office"
    hass.entity_registry = _FakeEntityRegistry(
        {
            "button.soul_espresso": _RegistryEntry(
                set(), device_id="dev_AC000", area_id="kitchen"
            )
        }
    )
    targets = dl._resolve_targets(hass, _Call(beverage="espresso", area_id=["kitchen"]))
    assert targets == [soul]


def test_an_area_holding_no_machine_is_refused_not_widened():
    """A target that matched nothing must not fall back to every machine."""
    soul, eletta = _machines("PrimaDonna Soul", "Eletta Explore")
    hass = _hass_with(("entry_1", [soul, eletta]))
    with pytest.raises(_StubHomeAssistantError) as err:
        dl._resolve_targets(hass, _Call(beverage="espresso", area_id=["garage"]))
    assert err.value.translation_key == "target_matched_no_machine"
    assert soul.sent == [] and eletta.sent == []


def test_an_area_sweeping_in_other_devices_keeps_only_the_machines():
    """A kitchen holds a coffee machine and a dozen other things."""
    soul, = _machines("PrimaDonna Soul")
    hass = _hass_with(("entry_1", [soul]))
    hass.device_registry.devices["dev_AC000"].area_id = "kitchen"
    kettle = _RegistryEntry({("kettle", "k1")}, name="Kettle", area_id="kitchen")
    kettle.id = "dev_kettle"
    hass.device_registry.devices["dev_kettle"] = kettle
    targets = dl._resolve_targets(hass, _Call(beverage="espresso", area_id=["kitchen"]))
    assert targets == [soul]


# --- a machine whose entry is not loaded --------------------------------------

def test_a_real_machine_whose_entry_is_unloaded_says_so():
    """Not the same failure as "that is not a coffee machine", nor the same fix.

    The device stays in the registry when its entry unloads, so the target picker
    still offers it. Telling that user their own machine is not a De'Longhi
    machine sends them hunting a targeting bug that is not there.
    """
    soul, = _machines("PrimaDonna Soul")
    hass = _hass_with(("entry_1", [soul]))
    absent = _RegistryEntry({(const.DOMAIN, "AC777")}, name="Eletta Explore")
    absent.id = "dev_AC777"
    hass.device_registry.devices["dev_AC777"] = absent

    with pytest.raises(_StubHomeAssistantError) as err:
        dl._resolve_targets(hass, _Call(beverage="espresso", device_id=["dev_AC777"]))

    assert err.value.translation_key == "machine_unavailable"
    assert err.value.translation_placeholders["device"] == "Eletta Explore"


def test_a_foreign_identifier_that_looks_like_a_dsn_is_not_accepted():
    """The domain half of the identifier tuple has to be checked, not just the value.

    Without the domain filter this passes anyway on a made-up value, which is
    why the value here is deliberately a real DSN.
    """
    soul, = _machines("PrimaDonna Soul")
    hass = _hass_with(("entry_1", [soul]))
    impostor = _RegistryEntry({("some_other_integration", "AC000")}, name="Impostor")
    impostor.id = "dev_impostor"
    hass.device_registry.devices["dev_impostor"] = impostor

    with pytest.raises(_StubHomeAssistantError) as err:
        dl._resolve_targets(hass, _Call(beverage="espresso", device_id=["dev_impostor"]))
    assert err.value.translation_key == "target_not_a_machine"


# --- naming and deduplication -------------------------------------------------

def test_a_renamed_machine_is_named_as_the_user_renamed_it():
    """The error must use the name the target picker shows, or it is unusable."""
    soul, eletta = _machines("PrimaDonna Soul", "Eletta Explore")
    hass = _hass_with(("entry_1", [soul, eletta]))
    hass.device_registry.devices["dev_AC000"].name_by_user = "Kitchen machine"

    with pytest.raises(_StubHomeAssistantError) as err:
        dl._resolve_targets(hass, _Call(beverage="espresso"))
    named = err.value.translation_placeholders["machines"]
    assert "Kitchen machine" in named and "PrimaDonna Soul" not in named


def test_one_machine_set_up_twice_is_still_one_machine():
    """Two entries over the same account must not read as two machines.

    config_flow keys entries on the e-mail, so the same DSN can legitimately
    appear twice. Without deduplication the single-machine shortcut is skipped
    and the user is asked to choose between "Soul" and "Soul".
    """
    hass = _hass_with(
        ("entry_1", [_FakeCoordinator("AC000", "Soul")]),
        ("entry_2", [_FakeCoordinator("AC000", "Soul")]),
    )
    assert len(dl._all_coordinators(hass)) == 1
    assert dl._resolve_targets(hass, _Call(beverage="espresso")) == dl._all_coordinators(hass)


def test_something_that_is_not_a_coordinator_list_is_stepped_over():
    """hass.data[DOMAIN] is shared ground; a service call must not trip on it."""
    soul, = _machines("PrimaDonna Soul")
    hass = _hass_with(("entry_1", [soul]))
    hass.data[const.DOMAIN]["shared_client"] = object()
    assert dl._all_coordinators(hass) == [soul]


# --- registration lifecycle ---------------------------------------------------

def test_a_second_entry_does_not_register_the_services_again():
    hass = _hass_with(("entry_1", _machines("PrimaDonna Soul")))
    dl._register_services(hass)
    assert hass.services.registrations == 3
    dl._register_services(hass)
    assert hass.services.registrations == 3, "the second entry registered again"


def test_a_service_that_went_missing_alone_is_restored():
    """The guard is per service, not "start_beverage exists so all three do"."""
    hass = _hass_with(("entry_1", _machines("PrimaDonna Soul")))
    dl._register_services(hass)
    hass.services.async_remove(const.DOMAIN, const.SERVICE_STOP_BEVERAGE)
    dl._register_services(hass)
    assert const.SERVICE_STOP_BEVERAGE in hass.services.handlers


def test_removing_one_of_two_entries_leaves_the_services_alone():
    """Removing them here would take the other entry's machines out of reach."""
    hass = _hass_with(
        ("entry_1", _machines("PrimaDonna Soul")),
        ("entry_2", [_FakeCoordinator("AC001", "Eletta Explore")]),
    )
    dl._register_services(hass)

    entry = types.SimpleNamespace(entry_id="entry_1")
    asyncio.run(dl.async_unload_entry(hass, entry))
    hass.config_entries = _FakeConfigEntries(["entry_2"])
    asyncio.run(dl.async_remove_entry(hass, entry))

    assert set(hass.services.handlers) == {
        const.SERVICE_START_BEVERAGE,
        const.SERVICE_STOP_BEVERAGE,
        const.SERVICE_SEND_RAW_COMMAND,
    }


def test_deleting_the_last_entry_removes_the_services():
    """By async_remove_entry the entry is already out of the registry."""
    hass = _hass_with(("entry_1", _machines("PrimaDonna Soul")))
    dl._register_services(hass)
    entry = types.SimpleNamespace(entry_id="entry_1")

    asyncio.run(dl.async_unload_entry(hass, entry))
    hass.config_entries = _FakeConfigEntries([])
    asyncio.run(dl.async_remove_entry(hass, entry))

    assert hass.services.handlers == {}


def test_a_reload_of_the_only_entry_keeps_the_services():
    """A reload unloads then sets up again, and the setup can fail.

    Gating removal on hass.data - which a reload empties - meant that a setup
    raising ConfigEntryNotReady (a cloud 5xx, an auth flap: routine here) left
    the services gone for the whole retry backoff. Automations would then fail
    with "Service not found" instead of the translated machine error they are
    written against. The entry still exists during a reload, so it is the entry
    list that decides.
    """
    hass = _hass_with(("entry_1", _machines("PrimaDonna Soul")))
    dl._register_services(hass)

    asyncio.run(dl.async_unload_entry(hass, types.SimpleNamespace(entry_id="entry_1")))

    assert const.SERVICE_START_BEVERAGE in hass.services.handlers
