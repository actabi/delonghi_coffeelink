"""Fork additions for the PrimaDonna Soul (ECAM 610.75): alarms, events, auth.

Pure modules only - no Home Assistant. Loaded under a package name of their own
so they cannot race the other modules for the cached ``delonghi_coffeelink.*``
entries (see the note at the top of test_coordinator_session.py).

Two kinds of evidence are pinned here, and they are kept apart on purpose:

* real bytes - the reference Soul dump (tests/fixtures/soul_properties.json) and
  BLE monitor captures published in Arbuzov/home_assistant_delonghi_primadonna
  (DEBUG_NOTES.md and the frame constants in its const.py). Their CRCs must
  check out, which is what makes them evidence and not typing;
* constructed snapshots - for the event logic, which is about comparing polls
  and owes nothing to the machine.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

COMPONENT_DIR = (
    Path(__file__).resolve().parents[1] / "custom_components" / "delonghi_coffeelink"
)
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "soul_properties.json"

_PKG = "delonghi_soul_under_test"
_package = types.ModuleType(_PKG)
_package.__path__ = [str(COMPONENT_DIR)]
sys.modules[_PKG] = _package


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{_PKG}.{name}", COMPONENT_DIR / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{_PKG}.{name}"] = module
    spec.loader.exec_module(module)
    return module


const = _load("const")
_load("counters")
cb = _load("command_builder")
monitor = _load("monitor")
events = _load("machine_events")
ayla = _load("ayla_client")


def _props() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _ble(hex_frame: str) -> str:
    """A BLE monitor capture, base64-encoded the way the cloud carries it."""
    import base64

    return base64.b64encode(bytes.fromhex(hex_frame)).decode()


# --- the reference Soul's own monitor blob ----------------------------------

def test_reference_soul_monitor_carries_switches_and_alarms():
    """The Soul's d302_monitor is full length, so the maintenance bits exist."""
    decoded = monitor.parse_monitor_b64(_props()["d302_monitor"]["value"])
    assert "error" not in decoded
    assert decoded["status_name"] == "ready"
    assert decoded["switches"] == 0
    assert decoded["alarms"] == 0x04
    assert const.alarm_names(decoded["alarms"]) == {"descale_needed"}


def test_reference_soul_descale_bit_matches_its_water_counters():
    """Corroboration for bit 2 on a Soul, from the same dump.

    d826 (water since the last descale) is past d827 (its threshold), so a
    descale is due - and bit 2 is the only alarm set. Not proof of the bit's
    meaning, but the one cross-check the dump allows.
    """
    props = _props()
    assert (
        props["d826_last_4_water_calc_qty"]["value"]
        > props["d827_last_4_calc_thrshld"]["value"]
    )


# --- independent BLE captures decode to the bits the sensors read ------------

# (capture, description in the source, expected switches bits, expected alarm bits)
# Left out on purpose: COFFEE_GROUNDS_CONTAINER_FULL in that const.py, whose CRC
# does not check out (43 d0, computed 43 eb) - a hand-edited constant, not a
# capture. COFFEE_GROUNDS_CONTAINER_CLEAN carries bit 1 with a valid CRC.
BLE_CAPTURES = [
    ("d0 12 75 0f 01 05 00 00 00 07 00 00 00 00 00 00 00 9d 61", "All is good", {0, 2}, set()),
    ("d0 12 75 0f 01 15 00 00 00 07 00 00 00 00 00 00 00 aa 31", "No water tank", {0, 2, 4}, set()),
    ("d0 12 75 0f 01 0d 00 00 00 07 00 00 00 00 00 00 00 86 c9", "No recycle tank", {0, 2, 3}, set()),
    ("d0 12 75 0f 01 45 00 01 00 07 00 00 00 00 00 00 00 2f 64", "Requested fresh water", {0, 2, 6}, {0}),
    ("d0 12 75 0f 01 41 00 05 00 01 00 00 00 00 00 00 00 0f 4e", "WATER_SHORTAGE2", {0, 6}, {0, 2}),
    ("d0 12 75 0f 01 05 00 0a 00 07 00 00 00 00 00 00 00 09 a0", "COFFEE_GROUNDS_CONTAINER_CLEAN", {0, 2}, {1, 3}),
    ("d0 12 75 0f 01 01 00 04 00 00 03 64 00 00 00 00 00 7b a3", "Power off device asks for cleanup", {0}, {2}),
]


def _bits(value: int) -> set[int]:
    return {bit for bit in range(32) if (value >> bit) & 1}


@pytest.mark.parametrize(("frame", "label", "switches", "alarms"), BLE_CAPTURES)
def test_ble_capture_decodes_to_the_expected_bits(frame, label, switches, alarms):
    decoded = monitor.parse_monitor_b64(_ble(frame))
    assert "error" not in decoded, f"{label}: {decoded.get('error')}"  # CRC included
    assert _bits(decoded["switches"]) == switches, label
    assert _bits(decoded["alarms"]) == alarms, label


@pytest.mark.parametrize(("frame", "label", "switches", "alarms"), BLE_CAPTURES)
def test_offsets_agree_with_the_ble_integration(frame, label, switches, alarms):
    """monitor.py and Arbuzov's parse_monitor_data read the same bytes.

    That project indexes the raw packet: switches = data[5] + data[6] << 8,
    alarms = data[7] + data[8] << 8 + data[12] << 16 + data[13] << 24,
    status = data[9]. Re-implemented here from its source, not imported.
    """
    raw = bytes.fromhex(frame)
    decoded = monitor.parse_monitor_b64(_ble(frame))
    assert decoded["switches"] == raw[5] + (raw[6] << 8)
    assert decoded["alarms"] == raw[7] + (raw[8] << 8) + (raw[12] << 16) + (raw[13] << 24)
    assert decoded["status"] == raw[9]


def test_unmapped_alarm_bits_are_still_named():
    assert const.alarm_names((1 << 5) | (1 << 30)) == {"beans_empty", "alarm_bit_30"}


# --- events ------------------------------------------------------------------

def _snap(status=None, alarms=None, **counters):
    return events.Snapshot(
        status=status,
        alarms=None if alarms is None else frozenset(alarms),
        counters=counters,
    )


def test_first_snapshot_is_a_baseline():
    assert events.diff(None, _snap("ready", {"descale_needed"}, espresso=10)) == []


def test_status_alarm_and_drink_changes_in_a_stable_order():
    before = _snap("ready", {"descale_needed"}, espresso=10, tea=2)
    after = _snap("dispensing_hot_water", {"water_tank_empty"}, espresso=11, tea=2)
    assert events.diff(before, after) == [
        ("status_changed", {"from_status": "ready", "to_status": "dispensing_hot_water"}),
        ("alarm_raised", {"alarm": "water_tank_empty"}),
        ("alarm_cleared", {"alarm": "descale_needed"}),
        ("beverage_dispensed", {"beverage": "espresso", "count": 1, "total": 11}),
    ]


def test_unknown_is_not_a_change_and_the_last_value_is_kept():
    before = _snap("ready", set(), espresso=10)
    stale = _snap(None, None)  # stale monitor, counters missing from the poll
    assert events.diff(before, stale) == []
    kept = events.merge(before, stale)
    assert kept == before
    # The move that happened while stale is still reported once data is back.
    assert events.diff(kept, _snap("standby", set(), espresso=10)) == [
        ("status_changed", {"from_status": "ready", "to_status": "standby"})
    ]


def test_counter_drop_is_a_reset_not_a_drink():
    assert events.diff(_snap(espresso=10), _snap(espresso=0)) == []


def test_a_bad_counter_read_and_its_recovery_raise_no_drink():
    """Review finding: 1234 -> 0 -> 1234 used to report 1234 drinks."""
    last = _snap(espresso=1234)
    fired = []
    for value in (0, 1234, 1235):
        current = _snap(espresso=value)
        fired += events.diff(last, current)
        last = events.merge(last, current)
    assert fired == [("beverage_dispensed", {"beverage": "espresso", "count": 1, "total": 1235})]


def test_unmapped_status_is_not_a_status():
    """Status code 29 (or any unmapped code) decodes as 'unknown': not an event."""
    snap = events.take_snapshot({}, {"status_name": "unknown", "alarms": 0}, stale=False)
    assert snap.status is None
    assert snap.alarms == frozenset()


def test_snapshot_from_the_reference_soul():
    props = _props()
    decoded = monitor.parse_monitor_b64(props["d302_monitor"]["value"])
    snap = events.take_snapshot(props, decoded, stale=False)
    assert snap.status == "ready"
    assert snap.alarms == {"descale_needed"}
    assert snap.counters["espresso"] == props["d705_tot_id1_espr"]["value"]
    assert snap.counters["bean_system_5"] == props["d725_id204_bs_5"]["value"]
    # Stale or undecodable monitor: status and alarms unknown, counters kept.
    for kwargs in ({"stale": True},):
        stale = events.take_snapshot(props, decoded, **kwargs)
        assert stale.status is None and stale.alarms is None
        assert stale.counters == snap.counters
    broken = events.take_snapshot(props, {"error": "CRC mismatch"}, stale=False)
    assert broken.status is None and broken.alarms is None


def test_every_beverage_counter_exists_on_the_reference_soul():
    """A typo in BEVERAGE_COUNTERS would silently never fire an event."""
    props = _props()
    missing = sorted(
        name for name in const.BEVERAGE_COUNTERS.values() if name not in props
    )
    assert missing == []


def test_percent_sensor_datapoints_exist_on_the_reference_soul():
    props = _props()
    for candidates, *_ in const.PERCENT_SENSORS:
        assert any(name in props for name in candidates), candidates


# --- auth classification -------------------------------------------------------

@pytest.mark.parametrize(
    ("code", "transient"),
    [
        (403042, False),  # invalid login ID or password
        (403120, False),  # account temporarily locked out
        (403002, False),  # request expired: host clock off, retrying cannot help
        ("403042", False),
        (206002, False),  # account pending verification: needs the user
        (403048, True),  # rate limit exceeded: the password was not judged
        (500001, True),  # general server error
        (None, True),
        ("not-a-number", True),
    ],
)
def test_gigya_error_classification(code, transient):
    assert ayla.gigya_error_is_transient(code) is transient


@pytest.mark.parametrize(
    ("status", "transient"),
    [(400, False), (401, False), (403, False), (408, True), (429, True), (500, True), (503, True)],
)
def test_ayla_sso_status_classification(status, transient):
    assert ayla.ayla_sso_status_is_transient(status) is transient


def test_auth_error_defaults_to_refused():
    """An AuthError nobody classified must stop polling, never retry forever."""
    assert ayla.AuthError("x").transient is False
    assert ayla.AuthError("x", transient=True).transient is True
    assert ayla.AuthError("x", code=403120).code == ayla.GIGYA_ACCOUNT_LOCKED


def _client_with_login(outcomes: list):
    """A client whose Gigya leg answers from ``outcomes``; counts attempts."""
    import asyncio

    client = ayla.DelonghiAylaClient(None, "owner@example.invalid", "secret")
    calls = []

    async def _gigya() -> str:
        calls.append(1)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def _sso(jwt: str) -> None:
        client._access_token = "token"

    client._gigya_login_and_jwt = _gigya
    client._ayla_sso_sign_in = _sso
    return client, calls, asyncio.run


def test_a_refused_login_is_remembered_and_never_retried():
    refused = ayla.AuthError("Gigya login failed (code 403042)", transient=False)
    client, calls, run = _client_with_login([refused, "jwt"])
    for _ in range(3):
        with pytest.raises(ayla.AuthError) as err:
            run(client.async_authenticate())
        assert err.value is refused
    assert len(calls) == 1


def test_a_transient_failure_is_retried_on_the_next_sign_in():
    blip = ayla.AuthError("Gigya 500001", transient=True)
    client, calls, run = _client_with_login([blip, "jwt"])
    with pytest.raises(ayla.AuthError):
        run(client.async_authenticate())
    run(client.async_authenticate())
    assert len(calls) == 2
    assert client._access_token == "token"
