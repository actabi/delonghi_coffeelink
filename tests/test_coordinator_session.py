"""Coordinator wiring: cloud-session identity (#15), reachability, monitor (#14).

The pure helpers are covered in ``test_eletta_session`` and ``test_monitor``;
what matters here is that the coordinator actually *uses* them - the original
bug was precisely a correct-looking session that carried the wrong identity.
Home Assistant is not installed in this suite, so the handful of symbols the
coordinator imports from it are stubbed with the minimum surface these tests
exercise. This is the only module that loads ``coordinator.py``, which is why
new coordinator cases belong here: a second module installing its own stubs
would race this one for the cached ``sys.modules`` entries.
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import logging
import sys
import time
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


class _StubHomeAssistantError(Exception):
    """exceptions.HomeAssistantError, with the translation kwargs HA accepts."""

    def __init__(self, *args, translation_domain=None, translation_key=None,
                 translation_placeholders=None) -> None:
        super().__init__(*args)
        self.translation_domain = translation_domain
        self.translation_key = translation_key
        self.translation_placeholders = translation_placeholders


def _install_stubs() -> None:
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.HomeAssistantError = _StubHomeAssistantError
    storage = types.ModuleType("homeassistant.helpers.storage")
    storage.Store = _StubStore
    upd = types.ModuleType("homeassistant.helpers.update_coordinator")
    upd.DataUpdateCoordinator = _StubCoordinator
    upd.UpdateFailed = type("UpdateFailed", (Exception,), {})
    for name, mod in (
        ("homeassistant", types.ModuleType("homeassistant")),
        ("homeassistant.core", core),
        ("homeassistant.exceptions", exceptions),
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


class _RecordingClient:
    """The slice of DelonghiAylaClient the send paths touch."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str, str]] = []

    async def async_set_property_value(self, dsn: str, prop: str, value: str) -> dict:
        self.writes.append((dsn, prop, value))
        return {}


class _PollingClient(_RecordingClient):
    """Also answers a full poll, so _async_update_data can really run."""

    def __init__(self, connection_status: str = "Online", props: dict | None = None) -> None:
        super().__init__()
        self.connection_status = connection_status
        self.connected_at = "2026-08-12T04:01:46Z"
        self.props = props if props is not None else {}

    async def async_get_properties(self, dsn: str) -> dict:
        return self.props

    async def async_get_devices(self) -> list:
        return [
            ac.AylaDevice(
                dsn="AC000W046513715",
                name="Coffee Maker",
                oem_model="DL-millcore",
                model="ECAM450.65.S",
                sw_version="1.0",
                lan_ip="192.168.1.10",
                connection_status=self.connection_status,
                connected_at=self.connected_at,
            )
        ]


def _coord(oem_model: str, connection_status: str = "Online", client=None):
    device = ac.AylaDevice(
        dsn="AC000W046513715",
        name="Coffee Maker",
        oem_model=oem_model,
        model="ECAM450.65.S",
        sw_version="1.0",
        lan_ip="192.168.1.10",
        connection_status=connection_status,
        connected_at="2026-08-12T04:01:46Z",
    )
    coord = coordinator.DelonghiCoordinator(object(), client, device)
    coord.command_property = "data_request"
    return coord


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


# --- reachability preflight -------------------------------------------------
#
# An offline machine still gets HTTP 200/201 from Ayla for every datapoint
# write, so nothing anywhere reports a failure: the reference Soul sat off the
# network for ten days while Home Assistant kept "sending" wakes. These cases
# pin the guard AND its narrowness - only an explicit Offline may block.

SEND_CALLS = (
    ("wake", lambda coord: coord.async_send_wake()),
    ("standby", lambda coord: coord.async_send_standby()),
    ("beverage start", lambda coord: coord.async_send_beverage(0x01, const.ACTION_START)),
    ("beverage stop", lambda coord: coord.async_send_beverage(0x10, const.ACTION_STOP)),
)
# Both families: the Soul synthesizes its frames, the Eletta replays learned ones
# behind a cloud session. Neither may reach the cloud while the machine is gone.
GUARDED_MODELS = ("DL-millcore", "DL-striker-cb")


@pytest.mark.parametrize("model", GUARDED_MODELS)
@pytest.mark.parametrize("label,call", SEND_CALLS, ids=[case[0] for case in SEND_CALLS])
def test_offline_machine_refuses_to_send(label, call, model):
    client = _RecordingClient()
    coord = _coord(model, connection_status="Offline", client=client)

    with pytest.raises(_StubHomeAssistantError) as err:
        asyncio.run(call(coord))

    assert err.value.translation_key == "machine_offline"
    assert err.value.translation_domain == const.DOMAIN
    assert err.value.translation_placeholders == {"name": "Coffee Maker"}
    assert client.writes == [], f"{label} on {model} reached the cloud despite Offline"


def test_the_raw_channel_is_never_refused():
    """`send_raw_command` is the field-instrumentation escape hatch.

    Refusing it would remove the only way to poke a machine when what is wrong
    is the integration's own idea of its state - the instrumentation this repo
    diagnoses with must survive its own guards.
    """
    client = _RecordingClient()
    coord = _coord("DL-millcore", connection_status="Offline", client=client)

    asyncio.run(coord.async_send_raw("DQeEDwIBVRJqiYFO"))

    assert [value for _dsn, _prop, value in client.writes] == ["DQeEDwIBVRJqiYFO"]


def test_online_machine_still_sends():
    client = _RecordingClient()
    coord = _coord("DL-millcore", connection_status="Online", client=client)

    asyncio.run(coord.async_send_wake())

    assert len(client.writes) == 1
    dsn, prop, value = client.writes[0]
    assert (dsn, prop) == (coord.device.dsn, "data_request")
    assert base64.b64decode(value)[:6].hex(" ") == "0d 07 84 0f 02 01"


def test_deep_standby_machine_is_never_blocked():
    """A machine in standby is Online, and must stay wakeable (#1).

    Issue #1 is an Eletta (DL-striker-cb), the profile where standby actually
    changes the send sequence: the guard reads connection_status, never the
    monitor, so a standby machine is not blocked - it gets the deep-standby
    nudge and then the wake.
    """
    client = _RecordingClient()
    coord = _coord("DL-striker-cb", connection_status="Online", client=client)
    coord.monitor = {"status": 0, "status_name": "standby"}

    asyncio.run(coord.async_send_wake())

    assert coord.machine_is_offline is False
    families = [
        base64.b64decode(value)[2:4].hex(" ") for _dsn, _prop, value in client.writes
    ]
    assert families == ["84 0f", "84 0f"]  # session refresh, then wake
    params = [
        base64.b64decode(value)[4:6].hex(" ") for _dsn, _prop, value in client.writes
    ]
    assert params == ["03 02", "02 01"]


def test_an_awake_machine_gets_no_standby_nudge():
    """Pins the other side of the monitor gate: no nudge unless status is 0.

    Resolving the monitor datapoint (issue #14) revives this branch on machines
    where self.monitor used to stay empty forever, so both sides need a test.
    """
    client = _RecordingClient()
    coord = _coord("DL-striker-cb", connection_status="Online", client=client)
    coord.monitor = {"status": 7, "status_name": "ready"}

    asyncio.run(coord.async_send_wake())

    assert len(client.writes) == 1
    assert base64.b64decode(client.writes[0][2])[4:6].hex(" ") == "02 01"


def test_a_stale_offline_status_fails_open():
    """A frozen status must not keep blaming the machine.

    connection_status only moves on a successful poll. If the cloud (or the poll
    loop) breaks while it last said Offline, refusing forever would turn a cloud
    outage into a permanently dead integration - so past REACHABILITY_MAX_AGE
    the preflight steps aside.
    """
    client = _RecordingClient()
    coord = _coord("DL-millcore", connection_status="Offline", client=client)
    coord._device_seen_at = time.time() - const.REACHABILITY_MAX_AGE - 1

    assert coord.reachability_is_current is False
    asyncio.run(coord.async_send_wake())
    assert len(client.writes) == 1


def test_a_fresh_offline_status_still_blocks():
    coord = _coord("DL-millcore", connection_status="Offline", client=_RecordingClient())

    assert coord.reachability_is_current is True
    with pytest.raises(_StubHomeAssistantError):
        asyncio.run(coord.async_send_wake())


@pytest.mark.parametrize("status", ["Unknown", "", "Weird New Value", None])
def test_only_an_explicit_offline_blocks(status):
    client = _RecordingClient()
    coord = _coord("DL-millcore", connection_status=status, client=client)

    assert coord.machine_is_offline is False
    asyncio.run(coord.async_send_wake())
    assert len(client.writes) == 1


def test_offline_detection_is_case_insensitive():
    assert _coord("DL-millcore", connection_status="offline").machine_is_offline
    assert _coord("DL-millcore", connection_status="OFFLINE").machine_is_offline


def test_a_poll_refreshes_the_status_the_guard_reads():
    """End-to-end: the machine comes back and the next poll unblocks commands.

    The whole guard rests on _async_update_data re-listing the device, so that
    is what this drives - not a hand-assigned coord.device, which would pass
    even if the poll never refreshed anything.
    """
    client = _PollingClient(connection_status="Offline")
    coord = _coord("DL-millcore", connection_status="Offline", client=client)
    with pytest.raises(_StubHomeAssistantError):
        asyncio.run(coord.async_send_wake())

    client.connection_status = "Online"
    client.connected_at = "2026-08-22T09:15:00Z"
    asyncio.run(coord._async_update_data())

    assert coord.device.connection_status == "Online"
    assert coord.device.connected_at == "2026-08-22T09:15:00Z"
    asyncio.run(coord.async_send_wake())
    assert len(client.writes) == 1


def test_a_poll_that_never_succeeds_leaves_the_status_stale():
    """The mirror case: no refresh means the guard must eventually fail open."""
    coord = _coord("DL-millcore", connection_status="Offline", client=_RecordingClient())
    coord._device_seen_at = time.time() - const.REACHABILITY_MAX_AGE - 1

    assert coord.machine_is_offline is True
    assert coord.reachability_is_current is False


# --- monitor datapoint resolution (issue #14) -------------------------------
#
# SOUL_MONITOR_BLOB is a real d302_monitor value read from the reference
# PrimaDonna Soul (ECAM 612.55.SB, dsn AC000W019023280) on 2026-08-22: this
# machine publishes d302_monitor, while MONITOR_PROPERTY used to be hard-wired
# to d302_monitor_machine - which is why Machine Status stayed unknown forever
# on that family. The blob itself decodes fine with the existing parser.

SOUL_MONITOR_BLOB = "0BJ1DwRAAgUAAAJkAAAAAACSz2p8UbM="


def _monitor_props(**candidates):
    return {name: {"value": value} for name, value in candidates.items()}


def _build_monitor_blob(contents: bytes) -> str:
    """A valid MonitorV2 packet around a contents block (same envelope as the real ones).

    Mirrors the builder in test_monitor.py: <0x0d> <length> <data> <crc16> <ts>,
    data = [0x75, subid, *contents], CRC over raw[0:length-1].
    """
    data = bytes([0x75, 0x00]) + contents
    body = bytes([0x0D, len(data) + 3]) + data
    raw = body + cb.crc16_aug_ccitt(body).to_bytes(2, "big") + b"\x00\x00\x00\x00"
    return base64.b64encode(raw).decode()


# Synthetic, not a field capture: no real Eletta d302_monitor_machine value has
# been contributed. Built through the same envelope the machine uses so it
# exercises the real decode path - ready, 42% through, switches 0x0201.
ELETTA_MONITOR_BLOB = _build_monitor_blob(
    bytes([0x01, 0x01, 0x02, 0x00, 0x00, 0x07, 0x00, 42, 0x00, 0x00, 0x00, 0x00, 0x00])
)


def test_monitor_resolves_the_datapoint_this_model_publishes():
    coord = _coord("DL-millcore")

    coord._update_monitor(_monitor_props(d302_monitor=SOUL_MONITOR_BLOB))

    assert coord.monitor_property == "d302_monitor"
    assert coord.monitor["status_name"] == "standby"
    assert coord.monitor["progress"] == 100


def test_a_present_but_empty_candidate_does_not_win():
    """Exposing a datapoint proves nothing - the Soul carries an always-null one."""
    coord = _coord("DL-millcore")

    coord._update_monitor(
        {
            "d302_monitor_machine": {"value": None},
            "d302_monitor": {"value": SOUL_MONITOR_BLOB},
        }
    )

    assert coord.monitor_property == "d302_monitor"
    assert coord.monitor["status_name"] == "standby"


def test_candidate_priority_is_respected_when_both_carry_values():
    coord = _coord("DL-striker-cb")

    coord._update_monitor(
        _monitor_props(
            d302_monitor_machine=SOUL_MONITOR_BLOB, d302_monitor=SOUL_MONITOR_BLOB
        )
    )

    assert coord.monitor_property == "d302_monitor_machine"


def test_resolution_is_retried_until_a_value_shows_up():
    coord = _coord("DL-millcore")

    coord._update_monitor(_monitor_props(d302_monitor="   "))
    assert coord.monitor_property is None
    assert coord.monitor == {}

    coord._update_monitor(_monitor_props(d302_monitor=SOUL_MONITOR_BLOB))
    assert coord.monitor_property == "d302_monitor"


def test_the_resolved_name_is_remembered_when_a_poll_brings_nothing():
    coord = _coord("DL-millcore")
    coord._update_monitor(_monitor_props(d302_monitor=SOUL_MONITOR_BLOB))

    coord._update_monitor(_monitor_props(d302_monitor=""))

    assert coord.monitor_property == "d302_monitor"
    assert coord.monitor == {}


def test_a_candidate_that_carries_junk_never_wins_over_one_that_decodes():
    """Carrying bytes proves no more than being listed does.

    A stale or truncated blob on the priority candidate would otherwise lock the
    poll onto a datapoint that can never yield a status - Machine Status stuck
    on unknown, which is the very symptom of issue #14.
    """
    coord = _coord("DL-millcore")

    coord._update_monitor(
        _monitor_props(
            d302_monitor_machine="bm90IGEgbW9uaXRvciBibG9i",  # decodes, isn't MonitorV2
            d302_monitor=SOUL_MONITOR_BLOB,
        )
    )

    assert coord.monitor_property == "d302_monitor"
    assert coord.monitor["status_name"] == "standby"


def test_the_datapoint_moving_after_a_firmware_update_is_followed():
    coord = _coord("DL-striker-cb")
    coord._update_monitor(_monitor_props(d302_monitor_machine=ELETTA_MONITOR_BLOB))
    assert coord.monitor_property == "d302_monitor_machine"

    coord._update_monitor(
        _monitor_props(d302_monitor_machine=None, d302_monitor=SOUL_MONITOR_BLOB)
    )

    assert coord.monitor_property == "d302_monitor"
    assert coord.monitor["status_name"] == "standby"


def test_an_eletta_blob_on_its_own_datapoint_decodes_as_before():
    """Non-regression for the family that already had a working Machine Status."""
    coord = _coord("DL-striker-cb")

    coord._update_monitor(_monitor_props(d302_monitor_machine=ELETTA_MONITOR_BLOB))

    assert coord.monitor_property == "d302_monitor_machine"
    assert coord.monitor["status_name"] == "ready"
    assert coord.monitor["progress"] == 42
    assert coord.monitor["switches"] == 0x0201


def test_no_monitor_datapoint_at_all_is_harmless():
    coord = _coord("DL-millcore")

    coord._update_monitor({"software_version": {"value": "Millcore_demo 2.0.0"}})

    assert coord.monitor_property is None
    assert coord.monitor == {}


def test_a_corrupt_blob_never_breaks_the_poll():
    coord = _coord("DL-millcore")

    coord._update_monitor(_monitor_props(d302_monitor="not base64 at all !!"))

    assert "error" in coord.monitor


# --- the cloud's own view of the machine (Last Connected) -------------------


class _FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeSession:
    """aiohttp.ClientSession.get(), enough for async_get_devices.

    ``timeout`` is accepted and recorded rather than ignored: every Ayla call is
    supposed to carry one, and a stub that silently swallowed the argument would
    let that guarantee rot.
    """

    def __init__(self, payload) -> None:
        self._payload = payload
        self.timeouts: list = []

    def get(self, url, headers=None, timeout=None):
        self.timeouts.append(timeout)
        return _FakeResponse(self._payload)


def _authenticated_client(payload):
    client = ac.DelonghiAylaClient(_FakeSession(payload), "user@example.com", "secret")
    client._access_token = "token"
    client._expires_at = time.time() + 3600
    return client


def test_device_carries_the_cloud_connected_at():
    """AylaDevice must transport connected_at: it is the only honest last-seen.

    The device_connected datapoint the sensor used to read was two months stale
    on the reference machine while the cloud knew the real answer.
    """
    client = _authenticated_client(
        [
            {
                "device": {
                    "dsn": "AC000W019023280",
                    "product_name": "AC000W019023280",
                    "oem_model": "DL-millcore",
                    "model": "AY008ESP1",
                    "sw_version": "ADA 1.5.3",
                    "lan_ip": "192.168.130.63",
                    "connection_status": "Offline",
                    "connected_at": "2026-08-12T04:01:46Z",
                }
            }
        ]
    )

    devices = asyncio.run(client.async_get_devices())

    assert devices[0].connected_at == "2026-08-12T04:01:46Z"
    assert devices[0].connection_status == "Offline"


def test_a_device_record_without_connected_at_yields_empty_not_junk():
    client = _authenticated_client(
        [{"device": {"dsn": "X", "connection_status": "Online"}}]
    )

    assert asyncio.run(client.async_get_devices())[0].connected_at == ""


def test_last_connected_is_no_longer_wired_to_a_datapoint():
    """Guard against the row coming back: it is what made the sensor lie."""
    keys = {key for _candidates, key, _friendly, _icon in const.INFO_SENSORS}
    assert "last_connected" not in keys
    named = [name for candidates, *_rest in const.INFO_SENSORS for name in candidates]
    assert "device_connected" not in named
    assert "app_device_connected" not in named


# --- fan-out across the machines of one config entry ------------------------
#
# A service call reaches every machine of the entry. The failure that started
# this story was silent, but the ones that come back from Ayla are not: a 5xx or
# an expired token raises CloudError/AuthError, plain Exception subclasses. One
# machine failing - for any reason - must not cost the others their command.


def _failing_coord(exc: Exception):
    coord = _coord("DL-millcore")

    async def _boom(_self=None) -> None:
        raise exc

    coord.async_send_wake = _boom  # type: ignore[method-assign]
    return coord


def test_every_machine_is_attempted_before_the_error_surfaces():
    ok_a, ok_b = _coord("DL-millcore", client=_RecordingClient()), _coord(
        "DL-millcore", client=_RecordingClient()
    )
    boom = _failing_coord(ac.CloudError("Ayla 502"))

    with pytest.raises(ac.CloudError):
        asyncio.run(
            coordinator.async_send_to_all(
                [boom, ok_a, ok_b], lambda coord: coord.async_send_wake()
            )
        )

    assert len(ok_a.client.writes) == 1
    assert len(ok_b.client.writes) == 1


def test_a_cloud_error_is_not_swallowed():
    """The first error must reach the caller, whatever its class."""
    boom = _failing_coord(ac.AuthError("token expired"))

    with pytest.raises(ac.AuthError):
        asyncio.run(
            coordinator.async_send_to_all([boom], lambda coord: coord.async_send_wake())
        )


def test_the_offline_refusal_still_surfaces_through_the_fan_out():
    offline = _coord("DL-millcore", connection_status="Offline", client=_RecordingClient())
    online = _coord("DL-millcore", client=_RecordingClient())

    with pytest.raises(_StubHomeAssistantError) as err:
        asyncio.run(
            coordinator.async_send_to_all(
                [offline, online], lambda coord: coord.async_send_wake()
            )
        )

    assert err.value.translation_key == "machine_offline"
    assert len(online.client.writes) == 1
    assert offline.client.writes == []


def test_nothing_is_raised_when_every_machine_takes_it():
    coords = [_coord("DL-millcore", client=_RecordingClient()) for _ in range(3)]

    asyncio.run(
        coordinator.async_send_to_all(coords, lambda coord: coord.async_send_wake())
    )

    assert [len(coord.client.writes) for coord in coords] == [1, 1, 1]


def test_the_last_connected_sensor_is_registered_on_every_machine():
    """The const guard above only proves the datapoint row is gone.

    This pins the other half - that a sensor reading the cloud record replaced
    it - without needing a Home Assistant install to build the entity.
    """
    source = (PKG_DIR / "sensor.py").read_text(encoding="utf-8")

    assert "entities.append(DelonghiLastConnectedSensor(coord))" in source
    assert 'super().__init__(coord, "last_connected", "mdi:clock-outline")' in source
    assert "self.coordinator.device.connected_at" in source


# --- machine beverage catalogue (catalog.py, wired into the poll) ------------

catalog = _load("catalog", "catalog.py")


def _blob(family: bytes, payload: bytes) -> str:
    """A well-formed machine blob: d0 <len> <family> <payload> <crc16>."""
    body = family + payload
    frame = bytes([const.CMD_RESPONSE_PREFIX, len(body) + 3]) + body
    frame += cb.crc16_aug_ccitt(frame).to_bytes(2, "big")
    return base64.b64encode(frame).decode("ascii")


#: A machine declaring one custom slot (0xe6) with a per-profile recipe for it.
SLOT_PROPS = {
    "d028_rec_custom_1": {
        "value": _blob(
            catalog.FAMILY_DESCRIPTOR,
            bytes([0xE6, catalog.TAG_COFFEE_ML, 0, 20, 0, 80, 0, 120]),
        )
    },
    "d200_1_cstm_recipe_01": {
        "value": _blob(
            catalog.FAMILY_PROFILE_RECIPE,
            bytes([1, 0xE6, catalog.TAG_COFFEE_ML, 0x00, 0x50, catalog.TAG_INTENSITY, 3]),
        )
    },
}


def _slot_frame(bev_id: int = 0xE6) -> str:
    """A frame the *app* would send for a saved recipe: its own recipe bytes.

    Not ``build_beverage_command`` defaults - those are bytes this integration
    could have produced itself, which the sniffer refuses to learn on purpose
    (it would replace the "teach me from the app" prompt with a frame the
    machine ignores).
    """
    return cb.encode_command(
        cb.build_eletta_beverage_command(bev_id, 0x03, bytes.fromhex("01 00 50 02 03"))
    )


def test_a_poll_builds_the_catalogue_from_the_properties_it_already_has():
    """No extra request: discovery is a pure function of the poll's own result."""
    client = _PollingClient(props=dict(SLOT_PROPS))
    coord = _coord("DL-millcore", client=client)
    assert coord.catalog is None

    asyncio.run(coord._async_update_data())

    assert coord.catalog is not None
    assert 0xE6 in coord.catalog["beverages"]
    assert coord.catalog["beverages"][0xE6]["defined"] is True
    assert client.writes == [], "reading the catalogue must never write to the machine"


def test_an_unchanged_poll_does_not_rebuild_the_catalogue():
    """The fingerprint is what keeps the parse off the polls where nothing moved."""
    client = _PollingClient(props=dict(SLOT_PROPS))
    coord = _coord("DL-millcore", client=client)
    asyncio.run(coord._async_update_data())
    first = coord.catalog

    asyncio.run(coord._async_update_data())
    assert coord.catalog is first, "same bytes must not re-parse"

    # A recipe edited on the machine must be picked up, though - a cache that
    # cannot see a change is the failure mode this project keeps being bitten by.
    client.props = dict(SLOT_PROPS)
    client.props["d200_1_cstm_recipe_01"] = {
        "value": _blob(
            catalog.FAMILY_PROFILE_RECIPE,
            bytes([1, 0xE6, catalog.TAG_COFFEE_ML, 0x00, 0x64, catalog.TAG_INTENSITY, 3]),
        )
    }
    asyncio.run(coord._async_update_data())
    assert coord.catalog is not first
    assert coord.catalog["beverages"][0xE6]["profiles"][1][catalog.TAG_COFFEE_ML] == 0x64


def test_a_poll_with_no_readable_blobs_keeps_the_catalogue_it_had():
    """A blank poll must never look like a machine that lost its recipes."""
    client = _PollingClient(props=dict(SLOT_PROPS))
    coord = _coord("DL-millcore", client=client)
    asyncio.run(coord._async_update_data())
    known = coord.catalog

    client.props = {"software_version": {"value": "Millcore_demo 2.0.0"}}
    asyncio.run(coord._async_update_data())
    assert coord.catalog is known


def test_a_broken_catalogue_never_breaks_the_poll(monkeypatch):
    """Diagnostic-grade, like the monitor decode: the poll must still return."""
    client = _PollingClient(props=dict(SLOT_PROPS))
    coord = _coord("DL-millcore", client=client)
    monkeypatch.setattr(
        coordinator, "build_catalog", lambda props: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    props = asyncio.run(coord._async_update_data())
    assert props == SLOT_PROPS
    assert coord.catalog is None


def test_a_saved_recipe_frame_is_learned_because_the_machine_declares_it():
    """The bug: pressing "Perso 1" in the official app had its frame discarded.

    0xe6 is not in the hardcoded beverage list, so the only bytes that could
    ever reproduce that drink were dropped in silence. The catalogue built from
    the same poll is what makes the frame learnable.
    """
    frame = _slot_frame()
    coord = _coord("DL-striker-cb")
    _capture(coord, frame)
    assert coord.learned_start_frames == {}, "without a catalogue, still dropped"

    coord = _coord("DL-striker-cb")
    coord.catalog = catalog.build_catalog(SLOT_PROPS)
    _capture(coord, frame)
    assert coord.learned_start_frames == {0xE6: frame}
    assert coord._store.saves == 1, "and it is persisted, so it survives a restart"


def test_the_catalogue_is_built_before_the_frame_is_sniffed():
    """Ordering matters: a slot brewed on this very poll must be learnable now.

    If _update_catalog ran after _sniff_app_traffic, the first press of a saved
    recipe would always be lost and only the second would stick.
    """
    frame = _slot_frame()
    # Poll 1: the channel is seen for the first time (never a capture, by
    # design) and the machine has not published the slot yet.
    client = _PollingClient(
        props={"app_data_request": {"value": "AA==", "data_updated_at": "t0"}}
    )
    coord = _coord("DL-striker-cb", client=client)
    coord.command_property = "app_data_request"
    asyncio.run(coord._async_update_data())
    assert coord.catalog is None

    # Poll 2 brings the slot declaration and the frame together. Build the
    # catalogue after sniffing and this first press is lost.
    client.props = dict(SLOT_PROPS)
    client.props["app_data_request"] = {"value": frame, "data_updated_at": "t1"}
    asyncio.run(coord._async_update_data())
    assert coord.learned_start_frames == {0xE6: frame}


def test_an_id_no_one_declares_is_still_refused():
    """Widening the gate must not turn it into no gate at all."""
    frame = _slot_frame(bev_id=0x7F)
    coord = _coord("DL-striker-cb")
    coord.catalog = catalog.build_catalog(SLOT_PROPS)
    _capture(coord, frame)
    assert coord.learned_start_frames == {}

# --- Soul monitor-session keepalive (#14) -----------------------------------
#
# The field failure these pin down: on a PrimaDonna Soul (ECAM610.55,
# DL-millcore, ADA 1.5.3) `d302_monitor` had not moved in five days while the
# machine was in daily use, so Machine Status read `standby` throughout. Every
# poll succeeded and `connection_status` stayed `Online` - the stale value was in
# the cloud. The Soul publishes its monitor blob only when an app session is
# written to `device_connected`, and that happened once, at setup. Counters kept
# flowing the whole time, which is exactly what made it invisible.

def _soul_poll_props():
    props = _monitor_props(d302_monitor=SOUL_MONITOR_BLOB)
    props["device_connected"] = {"value": "1788209091"}
    return props


def _keepalives(client):
    return [w for w in client.writes if w[1] == "device_connected"]


def test_a_soul_poll_refreshes_the_monitor_session():
    """Without this write the machine simply stops publishing its status."""
    client = _PollingClient(props=_soul_poll_props())
    coord = _coord("DL-millcore", client=client)

    asyncio.run(coord._async_update_data())

    assert coord.connected_property == "device_connected"
    assert len(_keepalives(client)) == 1
    # A plain unix timestamp - NOT the base64(ts + app_id) blob that ECAM's
    # app_device_connected takes (ayla_client.async_post_cloud_session). The two
    # payloads are not interchangeable.
    value = _keepalives(client)[0][2]
    assert isinstance(value, int)
    assert abs(value - int(time.time())) < 5


def test_the_keepalive_is_rate_limited():
    """Two polls inside one interval must still cost exactly one write."""
    client = _PollingClient(props=_soul_poll_props())
    coord = _coord("DL-millcore", client=client)

    asyncio.run(coord._async_update_data())
    asyncio.run(coord._async_update_data())

    assert len(_keepalives(client)) == 1


def test_the_keepalive_comes_round_again_once_the_interval_lapses():
    client = _PollingClient(props=_soul_poll_props())
    coord = _coord("DL-millcore", client=client)
    asyncio.run(coord._async_update_data())

    coord._last_monitor_session_at -= const.MONITOR_KEEPALIVE_INTERVAL + 1
    asyncio.run(coord._async_update_data())

    assert len(_keepalives(client)) == 2


def test_the_keepalive_interval_leaves_room_for_the_poll_that_carries_it():
    """The guard must not be able to reject the very poll it rides on.

    Equal to DEFAULT_SCAN_INTERVAL, `now - last < INTERVAL` is a coin flip: the
    keepalive is stamped a few hundred ms into a poll, so the *next* scheduled
    poll lands a hair under one interval later and is skipped.
    """
    assert const.MONITOR_KEEPALIVE_INTERVAL < const.DEFAULT_SCAN_INTERVAL


def test_a_poll_one_scan_interval_later_still_writes_the_keepalive():
    """The regression itself: successive writes were +61/+30/+31/+60 s, not +30.

    The gap here is a hair UNDER one scan interval, which is what a real polling
    loop produces - and is exactly what the old `< DEFAULT_SCAN_INTERVAL` guard
    rejected, deferring the write a whole cycle and halving the status
    resolution.
    """
    client = _PollingClient(props=_soul_poll_props())
    coord = _coord("DL-millcore", client=client)
    asyncio.run(coord._async_update_data())
    assert len(_keepalives(client)) == 1

    coord._last_monitor_session_at -= const.DEFAULT_SCAN_INTERVAL - 0.05
    asyncio.run(coord._async_update_data())

    assert len(_keepalives(client)) == 2, (
        "a poll one scan interval later must carry a keepalive; "
        "it was being skipped every other cycle"
    )


def test_eletta_polls_do_not_get_the_soul_keepalive():
    """Eletta takes its session per command through app_device_connected; a
    periodic timestamp write there would fight the official app for it."""
    props = _monitor_props(d302_monitor_machine=ELETTA_MONITOR_BLOB)
    props["device_connected"] = {"value": "1788209091"}
    client = _PollingClient(props=props)
    coord = _coord("DL-striker-cb", client=client)

    asyncio.run(coord._async_update_data())

    assert _keepalives(client) == []


def test_a_failed_keepalive_never_fails_the_poll():
    """A missed keepalive costs one stale poll. If it propagated, a single cloud
    hiccup would blank every entity instead."""

    class _RefusingClient(_PollingClient):
        async def async_set_property_value(self, dsn, prop, value):
            raise RuntimeError("cloud refused the datapoint write")

    coord = _coord("DL-millcore", client=_RefusingClient(props=_soul_poll_props()))

    props = asyncio.run(coord._async_update_data())

    assert props
    assert coord.monitor["status_name"] == "standby"


# --- keepalive health: the failure must not be quiet ------------------------
#
# A keepalive that fails every time rebuilds the exact blind spot it exists to
# close: polls green, machine Online, counters moving, Machine Status frozen.
# The write is a plain POST that never enters the transient-retry helper, so
# nothing else in the stack would report it either.


class _RefusingKeepaliveClient(_PollingClient):
    """Fails the connected-property write, answers every poll normally."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.refuse = True

    async def async_set_property_value(self, dsn, prop, value):
        if prop == "device_connected" and self.refuse:
            raise RuntimeError("cloud refused the datapoint write")
        return await super().async_set_property_value(dsn, prop, value)


def test_the_first_keepalive_failure_warns_and_the_repeats_do_not(caplog):
    """Loud once, then quiet: a warning per poll would be its own kind of noise."""
    client = _RefusingKeepaliveClient(props=_soul_poll_props())
    coord = _coord("DL-millcore", client=client)

    with caplog.at_level(logging.WARNING, logger=coordinator.__name__):
        asyncio.run(coord._async_update_data())
        first = [r for r in caplog.records if "keepalive failed" in r.message]
        assert len(first) == 1, "the first failure must be visible"
        assert "Machine Status will freeze" in first[0].message

        caplog.clear()
        coord._last_monitor_session_at -= const.MONITOR_KEEPALIVE_INTERVAL + 1
        asyncio.run(coord._async_update_data())
        assert [r for r in caplog.records if "keepalive failed" in r.message] == []
    assert coord._keepalive_failures == 2


def test_a_failure_is_retried_on_the_next_poll_not_deferred(caplog):
    """The rate-limit stamp must only advance on a write that landed.

    Stamping it before the attempt would turn every failure into a full interval
    of guaranteed silence, which is the opposite of what a keepalive is for.
    """
    client = _RefusingKeepaliveClient(props=_soul_poll_props())
    coord = _coord("DL-millcore", client=client)

    asyncio.run(coord._async_update_data())
    assert coord._last_monitor_session_at == 0, "a failed write leaves the clock alone"

    client.refuse = False
    asyncio.run(coord._async_update_data())
    assert len(_keepalives(client)) == 1, "the very next poll retries, no waiting"
    assert coord._last_monitor_session_at > 0


def test_recovery_is_announced(caplog):
    """Coming back matters as much as breaking: the log has to close the loop."""
    client = _RefusingKeepaliveClient(props=_soul_poll_props())
    coord = _coord("DL-millcore", client=client)

    with caplog.at_level(logging.WARNING, logger=coordinator.__name__):
        asyncio.run(coord._async_update_data())
        client.refuse = False
        caplog.clear()
        asyncio.run(coord._async_update_data())

    recovered = [r for r in caplog.records if "recovered" in r.message]
    assert len(recovered) == 1
    assert coord._keepalive_failures == 0


def test_arming_the_keepalive_is_announced_once(caplog):
    """The integration starts writing to the machine every 15 s; say so.

    Once, at INFO, naming the property and the cadence - so a user reading their
    log can tell this traffic is ours and deliberate.
    """
    client = _PollingClient(props=_soul_poll_props())
    coord = _coord("DL-millcore", client=client)

    with caplog.at_level(logging.INFO, logger=coordinator.__name__):
        asyncio.run(coord._async_update_data())
        coord._last_monitor_session_at -= const.MONITOR_KEEPALIVE_INTERVAL + 1
        asyncio.run(coord._async_update_data())

    armed = [r for r in caplog.records if "Keeping the monitor session alive" in r.message]
    assert len(armed) == 1, "announced on arming, not on every write"
    assert "device_connected" in armed[0].message


def test_an_unknown_soul_like_machine_is_never_written_to(caplog):
    """The keepalive payload is confirmed on DL-millcore and nowhere else.

    An unrecognised machine on the data_request channel keeps the Soul command
    dialect, which does generalise, and loses only the periodic write - which
    would otherwise put a bare timestamp into the property the official app uses
    to register its own session, 2880 times a day, on hardware nobody has tested.
    """
    props = _soul_poll_props()
    props["data_request"] = {"value": "AA==", "data_updated_at": "t0"}
    client = _PollingClient(props=props)
    coord = _coord("DL-future-xyz", client=client)
    coord.command_property = None  # let the poll detect the channel, as it does live

    asyncio.run(coord._async_update_data())

    assert coord.profile.key == "soul-generic", "the channel says Soul-like"
    assert _keepalives(client) == []
    assert coord._keepalive_armed is False


def test_every_ayla_read_is_bounded_by_a_timeout():
    """The session comes from Home Assistant and carries no total timeout.

    Without an explicit one, a request the far end accepts and never answers
    hangs as long as it likes. That was survivable while writes only happened on
    a user action; the monitor keepalive now writes every 15 seconds.
    """
    session = _FakeSession([])
    client = ac.DelonghiAylaClient(session, "user@example.com", "secret")
    client._access_token = "token"
    client._expires_at = time.time() + 3600

    asyncio.run(client.async_get_devices())

    assert session.timeouts, "the call must pass a timeout"
    assert all(t is not None and t.total == const.CLOUD_HTTP_TIMEOUT for t in session.timeouts)
