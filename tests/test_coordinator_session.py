"""Coordinator wiring for the Eletta cloud-session identity (issue #15).

The pure helpers are covered in ``test_eletta_session``; what matters here is
that the coordinator actually *uses* them - the original bug was precisely a
correct-looking session that carried the wrong identity. Home Assistant is not
installed in this suite, so the three symbols the coordinator imports from it
are stubbed with the minimum surface these tests exercise.
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import sys
import types
from pathlib import Path

import pytest

if importlib.util.find_spec("homeassistant") is not None:  # pragma: no cover
    # A real Home Assistant is installed (CI with pytest-homeassistant-custom-
    # component): the stubs below would shadow it for every later import, so this
    # module steps aside - port these cases to the HA fixtures instead.
    pytest.skip(
        "real Home Assistant present; stub-based coordinator tests skipped",
        allow_module_level=True,
    )

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "delonghi_coffeelink"


# --- minimal Home Assistant stubs ------------------------------------------

class _StubStore:
    """helpers.storage.Store: in-memory, records delayed saves."""

    def __init__(self, hass=None, version=None, key=None) -> None:
        self.data: dict | None = None
        self.saves = 0

    async def async_load(self) -> dict | None:
        return self.data

    def async_delay_save(self, data_func, delay: float = 0) -> None:
        self.saves += 1
        self.data = data_func()


class _StubCoordinator:
    """helpers.update_coordinator.DataUpdateCoordinator."""

    def __init__(self, hass, logger, name=None, update_interval=None) -> None:
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data: dict | None = None

    def __class_getitem__(cls, item):  # DataUpdateCoordinator[dict[str, Any]]
        return cls

    async def async_shutdown(self) -> None:
        return None

    async def async_request_refresh(self) -> None:
        return None


def _install_stubs() -> None:
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    storage = types.ModuleType("homeassistant.helpers.storage")
    storage.Store = _StubStore
    upd = types.ModuleType("homeassistant.helpers.update_coordinator")
    upd.DataUpdateCoordinator = _StubCoordinator
    upd.UpdateFailed = type("UpdateFailed", (Exception,), {})
    for name, mod in (
        ("homeassistant", types.ModuleType("homeassistant")),
        ("homeassistant.core", core),
        ("homeassistant.helpers", types.ModuleType("homeassistant.helpers")),
        ("homeassistant.helpers.storage", storage),
        ("homeassistant.helpers.update_coordinator", upd),
    ):
        sys.modules[name] = mod


def _load(modname: str, filename: str):
    full = f"delonghi_coffeelink.{modname}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, PKG_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


if "delonghi_coffeelink" not in sys.modules:
    _pkg = types.ModuleType("delonghi_coffeelink")
    _pkg.__path__ = [str(PKG_DIR)]
    sys.modules["delonghi_coffeelink"] = _pkg

_install_stubs()
const = _load("const", "const.py")
cb = _load("command_builder", "command_builder.py")
ac = _load("ayla_client", "ayla_client.py")
coordinator = _load("coordinator", "coordinator.py")

COFFEE_FRAME = "DQ+D8AIDAQBuAgMnAQa/qWp4qtoAxYYh"
COFFEE_SIGNATURE = bytes.fromhex("00c58621")
COFFEE_APP_ID = 12944929
DEFAULT_APP_ID = ac.normalize_signed_app_id(const.INTEGRATION_CLOUD_APP_ID)
SOUL_HOT_WATER = "DQ2D8BABDwD6GwEGgSRqILPb"


def _coord(oem_model: str):
    device = ac.AylaDevice(
        dsn="AC000W046513715",
        name="Coffee Maker",
        oem_model=oem_model,
        model="ECAM450.65.S",
        sw_version="1.0",
        lan_ip="192.168.1.10",
        connection_status="Online",
    )
    return coordinator.DelonghiCoordinator(object(), None, device)


def _decoded(frame: str) -> dict:
    decoded = cb.decode_command(frame)
    decoded["origin"] = "app"
    return decoded


# --- Eletta -----------------------------------------------------------------

def test_eletta_starts_on_the_fallback_constant():
    coord = _coord("DL-striker-cb")
    assert coord.own_cloud_app_id == DEFAULT_APP_ID
    assert coord.uses_device_cloud_app_id is False


def test_learning_a_frame_switches_the_session_to_the_machine_id():
    coord = _coord("DL-striker-cb")
    coord._maybe_learn_frame(_decoded(COFFEE_FRAME))
    assert coord.learned_start_frames == {0x02: COFFEE_FRAME}
    assert coord.own_cloud_app_id == COFFEE_APP_ID
    assert coord.uses_device_cloud_app_id is True
    # the id actually used for the session POST follows
    assert coord._integration_app_id == COFFEE_APP_ID
    # and a session confirmed under the old id must be re-established
    assert coord._session_confirmed is False
    assert coord._last_connect_at == 0


def test_restart_restores_the_machine_id_before_any_new_capture():
    coord = _coord("DL-striker-cb")
    coord._store.data = cb.serialize_learned_frames({0x02: COFFEE_FRAME}, {}, None)
    asyncio.run(coord.async_load_learned())
    assert coord.learned_start_frames == {0x02: COFFEE_FRAME}
    assert coord.own_cloud_app_id == COFFEE_APP_ID
    assert coord._integration_app_id == COFFEE_APP_ID


def test_adopted_foreign_session_is_not_rebound_by_a_new_capture():
    """A foreign session must keep riding the app's id until it is released."""
    coord = _coord("DL-striker-cb")
    coord._integration_app_id = 777
    coord._maybe_learn_frame(_decoded(COFFEE_FRAME))
    assert coord.own_cloud_app_id == COFFEE_APP_ID
    assert coord._integration_app_id == 777
    # once the machine reports a free slot, we revert to the derived id
    coord._update_session_from_props({const.APP_ID_PROPERTY: {"value": 0}})
    assert coord._integration_app_id == COFFEE_APP_ID


def test_session_holder_is_ours_once_the_machine_id_is_known():
    coord = _coord("DL-striker-cb")
    coord._maybe_learn_frame(_decoded(COFFEE_FRAME))
    coord._update_session_from_props(
        {const.APP_ID_PROPERTY: {"value": str(COFFEE_APP_ID)}}
    )
    assert coord._session_confirmed is True


def test_wake_and_standby_carry_the_machine_signature():
    coord = _coord("DL-striker-cb")
    coord._maybe_learn_frame(_decoded(COFFEE_FRAME))
    for value in (coord._wake_command_value(), coord._standby_command_value()):
        assert base64.b64decode(value)[-4:] == COFFEE_SIGNATURE


def test_unlearnable_frame_leaves_the_session_id_alone():
    coord = _coord("DL-striker-cb")
    raw = bytearray(base64.b64decode(COFFEE_FRAME))
    raw[8] ^= 0xFF  # corrupt the bytes the CRC covers
    coord._maybe_learn_frame(_decoded(base64.b64encode(bytes(raw)).decode()))
    assert coord.learned_start_frames == {}
    assert coord.own_cloud_app_id == DEFAULT_APP_ID


def _capture(coord, frame: str, prop: str = "app_data_request") -> None:
    """Drive a frame through the sniffer the way a poll would."""
    coord.command_property = prop
    coord._capture_channel(
        {prop: {"value": "AA==", "data_updated_at": "t0"}}, prop, channel="command"
    )
    coord._capture_channel(
        {prop: {"value": frame, "data_updated_at": "t1"}}, prop, channel="command"
    )


def test_sniffer_learns_the_coffee_frame_end_to_end():
    """Issue #15 bug 2, through the real capture path: Coffee used to be dropped."""
    coord = _coord("DL-striker-cb")
    _capture(coord, COFFEE_FRAME)
    assert coord.learned_start_frames == {0x02: COFFEE_FRAME}
    assert coord.own_cloud_app_id == COFFEE_APP_ID


def test_replayed_learned_frame_keeps_crc_and_signature():
    coord = _coord("DL-striker-cb")
    _capture(coord, COFFEE_FRAME)
    replayed = coord.profile.beverage_value(
        0x02, const.ACTION_START, coord.learned_start_frames[0x02]
    )
    decoded = cb.decode_command(replayed)
    assert decoded["crc_valid"] is True
    assert decoded["recipe"] == "01 00 6e 02 03 27 01 06"
    assert base64.b64decode(replayed)[-4:] == COFFEE_SIGNATURE
    assert replayed != COFFEE_FRAME  # only the timestamp moved


def test_sniffer_ignores_our_own_echoed_command():
    coord = _coord("DL-striker-cb")
    coord._record_sent(COFFEE_FRAME)
    _capture(coord, COFFEE_FRAME)
    assert coord.learned_start_frames == {}


def test_sniffer_does_not_learn_our_own_fallback_frame():
    """A Soul-shaped fallback seen on the wire is ours, not something to learn."""
    coord = _coord("DL-striker-cb")
    _capture(coord, SOUL_HOT_WATER)
    assert coord.last_captured_command["matches_integration"] is True
    assert coord.learned_start_frames == {}
    assert coord.own_cloud_app_id == DEFAULT_APP_ID


# --- Soul (reference machine) ----------------------------------------------

def test_soul_never_learns_and_keeps_the_default_id():
    coord = _coord("DL-millcore")
    coord._maybe_learn_frame(_decoded(COFFEE_FRAME))
    coord._maybe_learn_frame(_decoded(SOUL_HOT_WATER))
    assert coord.learned_start_frames == {}
    assert coord.learned_stop_frames == {}
    assert coord.own_cloud_app_id == DEFAULT_APP_ID
    assert coord.uses_device_cloud_app_id is False


def test_soul_command_bytes_are_unchanged():
    """The Soul path synthesizes its frame and never touches the session id."""
    coord = _coord("DL-millcore")
    assert coord.profile.uses_cloud_session is False
    built = coord.profile.beverage_value(0x10, const.ACTION_START, None)
    assert base64.b64decode(built)[:12].hex(" ") == "0d 0d 83 f0 10 01 0f 00 fa 1b 01 06"
