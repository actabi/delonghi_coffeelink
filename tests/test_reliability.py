"""Regression tests for truthful and serialized command execution."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "delonghi_coffeelink"
PKG_NAME = "delonghi_reliability_tests"


class HomeAssistantError(Exception):
    """Test double for Home Assistant's user-visible error."""


class ConfigEntryAuthFailed(Exception):
    """Test double for Home Assistant's reauth signal."""


class DataUpdateCoordinator:
    """Small test double sufficient for constructing the coordinator."""

    @classmethod
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, *, name, update_interval):
        self.hass = hass
        self.data = {}
        self.last_update_success = True

    async def async_shutdown(self):
        return None

    async def async_request_refresh(self):
        return None

    def async_update_listeners(self):
        return None


class Store:
    def __init__(self, hass, version, key):
        pass


def _install_homeassistant_stubs() -> None:
    voluptuous = types.ModuleType("voluptuous")
    voluptuous.Schema = lambda value: value
    voluptuous.Required = lambda value: value
    voluptuous.Optional = lambda value: value
    voluptuous.All = lambda *values: values[-1]
    voluptuous.In = lambda values: values
    homeassistant = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    config_entries = types.ModuleType("homeassistant.config_entries")
    data_entry_flow = types.ModuleType("homeassistant.data_entry_flow")
    exceptions = types.ModuleType("homeassistant.exceptions")
    constants = types.ModuleType("homeassistant.const")
    helpers = types.ModuleType("homeassistant.helpers")
    config_validation = types.ModuleType("homeassistant.helpers.config_validation")
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    service = types.ModuleType("homeassistant.helpers.service")
    typing_module = types.ModuleType("homeassistant.helpers.typing")
    storage = types.ModuleType("homeassistant.helpers.storage")
    update = types.ModuleType("homeassistant.helpers.update_coordinator")
    aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    loader = types.ModuleType("homeassistant.loader")

    class ConfigEntry:
        @classmethod
        def __class_getitem__(cls, item):
            return cls

    class ConfigEntryState:
        LOADED = "loaded"

    class Platform:
        SENSOR = "sensor"
        BINARY_SENSOR = "binary_sensor"
        BUTTON = "button"

    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            return super().__init_subclass__()

        def __init__(self):
            self.hass = object()
            self._reauth_entry = None

        def _get_reauth_entry(self):
            return self._reauth_entry

        def async_show_form(self, **kwargs):
            return {"type": "form", **kwargs}

        def async_update_reload_and_abort(self, entry, *, data_updates):
            return {
                "type": "abort",
                "reason": "reauth_successful",
                "entry": entry,
                "data_updates": data_updates,
            }

    async def async_get_integration(hass, domain):
        return types.SimpleNamespace(version="0.3.16")

    core.HomeAssistant = object
    core.ServiceCall = object
    config_entries.ConfigEntry = ConfigEntry
    config_entries.ConfigEntryState = ConfigEntryState
    config_entries.ConfigFlow = ConfigFlow
    constants.ATTR_DEVICE_ID = "device_id"
    constants.Platform = Platform
    data_entry_flow.FlowResult = dict
    exceptions.HomeAssistantError = HomeAssistantError
    exceptions.ConfigEntryAuthFailed = ConfigEntryAuthFailed
    exceptions.ConfigEntryNotReady = RuntimeError
    config_validation.ensure_list = lambda value: value if isinstance(value, list) else [value]
    config_validation.string = str
    device_registry.async_get = lambda hass: hass.device_registry
    service.async_register_admin_service = lambda *args, **kwargs: None
    typing_module.ConfigType = dict
    helpers.config_validation = config_validation
    helpers.device_registry = device_registry
    storage.Store = Store
    update.DataUpdateCoordinator = DataUpdateCoordinator
    update.UpdateFailed = RuntimeError
    loader.async_get_integration = async_get_integration
    aiohttp_client.async_get_clientsession = lambda hass: object()
    homeassistant.config_entries = config_entries
    sys.modules.update(
        {
            "voluptuous": voluptuous,
            "homeassistant": homeassistant,
            "homeassistant.core": core,
            "homeassistant.const": constants,
            "homeassistant.config_entries": config_entries,
            "homeassistant.data_entry_flow": data_entry_flow,
            "homeassistant.exceptions": exceptions,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.config_validation": config_validation,
            "homeassistant.helpers.device_registry": device_registry,
            "homeassistant.helpers.service": service,
            "homeassistant.helpers.typing": typing_module,
            "homeassistant.helpers.storage": storage,
            "homeassistant.helpers.update_coordinator": update,
            "homeassistant.helpers.aiohttp_client": aiohttp_client,
            "homeassistant.loader": loader,
        }
    )


_install_homeassistant_stubs()
package = types.ModuleType(PKG_NAME)
package.__path__ = [str(PKG_DIR)]
package.DelonghiRuntimeData = type("DelonghiRuntimeData", (), {})
sys.modules[PKG_NAME] = package


def _load(name: str):
    full_name = f"{PKG_NAME}.{name}"
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


const = _load("const")
command_builder = _load("command_builder")
ayla_client = _load("ayla_client")
model_profiles = _load("model_profiles")
_load("monitor")
coordinator_module = _load("coordinator")
diagnostics_module = _load("diagnostics")
config_flow_module = _load("config_flow")


def _load_integration_init():
    full_name = f"{PKG_NAME}.integration_init"
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


integration_module = _load_integration_init()


class FakeClient:
    def __init__(self):
        self.writes: list[tuple[str, str, str]] = []
        self.connect_posts = 0

    async def async_set_property_value(self, dsn, prop, value):
        self.writes.append((dsn, prop, value))

    async def async_post_cloud_session(self, dsn, prop, app_id):
        self.connect_posts += 1


def _coordinator(oem_model: str = "DL-millcore"):
    client = FakeClient()
    device = ayla_client.AylaDevice(
        dsn="private-device-id",
        name="Machine",
        oem_model=oem_model,
        model="model",
        sw_version="1",
        lan_ip="",
        connection_status="online",
    )
    coordinator = coordinator_module.DelonghiCoordinator(object(), client, device)
    coordinator.command_property = "data_request"
    return coordinator, client


def test_duplicate_command_is_rejected_not_queued():
    async def scenario():
        coordinator, _client = _coordinator()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def first_send():
            entered.set()
            await release.wait()

        first = asyncio.create_task(coordinator._run_command_transaction(first_send))
        await entered.wait()

        with pytest.raises(HomeAssistantError, match="still in progress"):
            await coordinator._run_command_transaction(lambda: asyncio.sleep(0))

        release.set()
        await first
        assert coordinator.last_command_result == "sent"

    asyncio.run(scenario())


def test_missing_learned_frame_is_rejected_without_write():
    async def scenario():
        coordinator, client = _coordinator()

        class LearnOnlyProfile:
            learns_from_app = True
            uses_cloud_session = False

            @staticmethod
            def beverage_value(beverage_id, action, learned_frame):
                return learned_frame

        coordinator.profile = LearnOnlyProfile()
        with pytest.raises(HomeAssistantError, match="until its frame has been learned"):
            await coordinator.async_send_beverage(0x02, const.ACTION_START)

        assert client.writes == []
        assert coordinator.last_command_result == "rejected"

    asyncio.run(scenario())


def test_stop_uses_active_beverage_id():
    async def scenario():
        coordinator, client = _coordinator()
        coordinator.active_beverage_id = 0x07

        await coordinator.async_stop_active_beverage()

        sent = command_builder.decode_command(client.writes[0][2])
        assert sent["beverage_id"] == "0x07"
        assert sent["action"] == const.ACTION_STOP
        assert coordinator.active_beverage_id is None

    asyncio.run(scenario())


def test_cold_connect_is_awaited_before_property_write(monkeypatch):
    async def scenario():
        coordinator, client = _coordinator("DL-striker-cb")
        coordinator.connected_property = "app_device_connected"
        coordinator.data = {const.APP_ID_PROPERTY: {"value": 0}}
        coordinator.learned_start_frames[0x02] = "DQ+D8AIDAQBuAgMnAQa/qWp4qtoAxYYh"
        monkeypatch.setattr(coordinator_module, "CONNECT_SETTLE_DELAY", 0)

        async def confirmed():
            return True

        coordinator._wait_for_session_confirmed = confirmed
        await coordinator.async_send_beverage(0x02, const.ACTION_START)

        assert client.connect_posts == 1
        assert len(client.writes) == 1
        assert coordinator.last_command_result == "sent"

    asyncio.run(scenario())


def test_cold_connect_timeout_is_visible_and_does_not_write(monkeypatch):
    async def scenario():
        coordinator, client = _coordinator("DL-striker-cb")
        coordinator.connected_property = "app_device_connected"
        coordinator.data = {const.APP_ID_PROPERTY: {"value": 0}}
        coordinator.learned_start_frames[0x02] = "DQ+D8AIDAQBuAgMnAQa/qWp4qtoAxYYh"
        monkeypatch.setattr(coordinator_module, "CONNECT_SETTLE_DELAY", 0)

        async def timed_out():
            return False

        coordinator._wait_for_session_confirmed = timed_out
        with pytest.raises(HomeAssistantError, match="Timed out"):
            await coordinator.async_send_beverage(0x02, const.ACTION_START)

        assert client.writes == []
        assert coordinator.last_command_result == "timed_out"

    asyncio.run(scenario())


def test_known_eletta_signature_restores_device_specific_app_id():
    coordinator, _client = _coordinator("DL-striker-cb")
    captured = "DQ+D8AIDAQBuAgMnAQa/qWp4qtoAxYYh"
    coordinator.learned_start_frames[0x02] = captured

    coordinator._restore_device_app_id()

    signature = command_builder.device_signature_from_frame(captured)
    assert signature == bytes.fromhex("00 c5 86 21")
    assert coordinator._integration_app_id == 12_944_929
    assert base64.b64decode(captured)[-4:] == signature


def test_downloaded_diagnostics_redact_credentials_and_identifiers():
    async def scenario():
        coordinator, _client = _coordinator("DL-striker-cb")
        coordinator.data = {
            "app_id": {"value": 12_944_929},
            "data_request": {"value": "raw-secret-frame"},
        }
        coordinator.command_property = "data_request"
        coordinator.last_captured_command = {"raw_b64": "raw-secret-frame"}
        entry = types.SimpleNamespace(
            runtime_data=types.SimpleNamespace(coordinators=[coordinator]),
            version=1,
            data={"email": "private@example.com", "password": "secret-password"},
        )

        diagnostics = await diagnostics_module.async_get_config_entry_diagnostics(
            object(), entry
        )
        rendered = json.dumps(diagnostics)

        for secret in (
            "private@example.com",
            "secret-password",
            "private-device-id",
            "raw-secret-frame",
            "12944929",
        ):
            assert secret not in rendered
        assert diagnostics["devices"][0]["property_names"] == [
            "app_id",
            "data_request",
        ]

    asyncio.run(scenario())


def test_reauth_rejects_bad_password_then_updates_existing_entry(monkeypatch):
    class FakeAuthClient:
        def __init__(self, session, email, password):
            self.password = password

        async def async_authenticate(self):
            if self.password == "wrong":
                raise ayla_client.AuthError("rejected")

        async def async_get_devices(self):
            return [types.SimpleNamespace(name="Machine")]

    monkeypatch.setattr(config_flow_module, "DelonghiAylaClient", FakeAuthClient)
    flow = config_flow_module.DelonghiConfigFlow()
    entry = types.SimpleNamespace(
        data={const.CONF_EMAIL: "private@example.com", const.CONF_PASSWORD: "old"}
    )
    flow._reauth_entry = entry

    bad = asyncio.run(
        flow.async_step_reauth_confirm({const.CONF_PASSWORD: "wrong"})
    )
    assert bad["type"] == "form"
    assert bad["errors"] == {"base": "invalid_auth"}

    good = asyncio.run(
        flow.async_step_reauth_confirm({const.CONF_PASSWORD: "replacement"})
    )
    assert good["type"] == "abort"
    assert good["reason"] == "reauth_successful"
    assert good["entry"] is entry
    assert good["data_updates"] == {const.CONF_PASSWORD: "replacement"}


def test_device_target_resolves_exactly_one_coordinator():
    coordinator_a, _client_a = _coordinator()
    coordinator_b, _client_b = _coordinator()
    coordinator_a.device.dsn = "device-a"
    coordinator_b.device.dsn = "device-b"
    runtime = integration_module.DelonghiRuntimeData(
        client=object(), coordinators=[coordinator_a, coordinator_b]
    )
    entry = types.SimpleNamespace(
        state="loaded",
        runtime_data=runtime,
    )
    device = types.SimpleNamespace(
        identifiers={(const.DOMAIN, "device-a")},
    )

    class Registry:
        @staticmethod
        def async_get(device_id):
            return device if device_id == "ha-device-a" else None

    hass = types.SimpleNamespace(
        config_entries=types.SimpleNamespace(
            async_entries=lambda domain: [entry],
        ),
        device_registry=Registry(),
    )
    call = types.SimpleNamespace(data={"device_id": ["ha-device-a"]})

    assert integration_module._target_coordinator(hass, call) is coordinator_a

    ambiguous = types.SimpleNamespace(data={"device_id": ["one", "two"]})
    with pytest.raises(HomeAssistantError, match="exactly one"):
        integration_module._target_coordinator(hass, ambiguous)
