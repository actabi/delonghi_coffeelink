"""Unit tests for the pure counter-value parsing (counters.py).

Loads only the dependency-free ``counters`` module (no Home Assistant import),
matching the approach in test_command_builder.py. Covers both value shapes seen
in the field: plain integers (PrimaDonna Soul) and JSON-object aggregated
counters (Eletta Explore, #7).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "delonghi_coffeelink"


def _load(modname: str, filename: str):
    full = f"delonghi_coffeelink.{modname}"
    spec = importlib.util.spec_from_file_location(full, PKG_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


counters = _load("counters", "counters.py")
parse_counter_value = counters.parse_counter_value
parse_water_volume_liters = counters.parse_water_volume_liters
counter_breakdown = counters.counter_breakdown


# --- parse_counter_value -------------------------------------------------- #

@pytest.mark.parametrize(
    "value,expected",
    [
        # Soul: plain integers / numeric strings
        (314, 314),
        (0, 0),
        ("314", 314),
        ("  42 ", 42),
        # Eletta: JSON object of per-recipe sub-counts -> sum
        ('{"espresso": 12, "coffee": 3}', 15),
        ('{"a": 1, "b": 2, "c": 3}', 6),
        ('{"only": 7}', 7),
        ('{}', 0),
        # JSON where some sub-values are not integers -> ignored, rest summed
        ('{"good": 5, "bad": "x", "also": 2}', 7),
        # Unparseable / unexpected -> None (sensor stays unknown)
        (None, None),
        (True, None),
        (False, None),
        ("", None),
        ("not-a-number", None),
        ('{"broken": ', None),   # malformed JSON
        ("[1, 2, 3]", None),     # JSON array, not an object
    ],
)
def test_parse_counter_value(value, expected):
    assert parse_counter_value(value) == expected


def test_parse_counter_value_bool_is_not_int():
    # bool is a subtype of int in Python; ensure we never treat it as a count.
    assert parse_counter_value(True) is None
    assert parse_counter_value(False) is None


@pytest.mark.parametrize(
    "value,expected",
    [
        (387213, 387.213),
        ("387213", 387.213),
        (0, 0.0),
        (1, 0.001),
        (None, None),
        (True, None),
        ("not-a-number", None),
        ('{"part_a": 1000, "part_b": 250}', 1.25),
    ],
)
def test_parse_water_volume_liters(value, expected):
    assert parse_water_volume_liters(value) == expected


# --- counter_breakdown ---------------------------------------------------- #

def test_counter_breakdown_returns_json_object():
    assert counter_breakdown('{"espresso": 12, "coffee": 3}') == {
        "espresso": 12,
        "coffee": 3,
    }


@pytest.mark.parametrize(
    "value",
    [None, 0, 314, True, "", "314", "not-json", "[1,2]", '{"broken": '],
)
def test_counter_breakdown_none_for_non_objects(value):
    assert counter_breakdown(value) is None


# --- declarative measurement table ---------------------------------------- #

def test_measured_counters_reference_real_counter_keys():
    """A typo in COUNTER_MEASUREMENTS would silently drop the conversion."""
    const = _load("const", "const.py")

    keys = {key for _candidates, key, _friendly, _icon in const.COUNTER_SENSORS}
    assert set(const.COUNTER_MEASUREMENTS) <= keys


def test_water_counters_are_declared_as_litres():
    """The mL -> L conversion is data, not a branch inside the entity."""
    const = _load("const", "const.py")

    assert const.COUNTER_MEASUREMENTS == {
        "water_total_quantity": const.MEASUREMENT_WATER_LITERS,
        "water_filter_quantity": const.MEASUREMENT_WATER_LITERS,
    }


# --- the b / bw / w / other buckets ----------------------------------------
#
# These four suffixes are BLACK / BLACK+WHITE / WHITE / OTHER. They were read as
# "beverages" / "milk drinks" / "water", which made d700 look like the machine's
# lifetime total when it counts only black drinks. Verified by arithmetic on a
# live PrimaDonna Soul (2026-09-01):
#
#     d700_tot_bev_b  4638 == espresso 4 + coffee 4633 + doppio 1   (exact)
#     d703_tot_bev_w    24 == hot_milk 24                           (exact)
#     lifetime total  4917 == d700 4638 + d701 248 + d703 24 + d702 7
#
# so "Total Beverages" under-reported the machine by ~6%. This pins the mapping
# so a future edit cannot quietly re-label them again.

const = _load("const", "const.py")

BUCKET_KEYS = {
    "d700_tot_bev_b": "total_beverages",
    "d701_tot_bev_bw": "total_milk_drinks",
    "d703_tot_bev_w": "total_water",
    "d702_tot_bev_other": "total_other_beverages",
}


def _counter_key_for(datapoint: str) -> str | None:
    for candidates, key, _name, _icon in const.COUNTER_SENSORS:
        if datapoint in candidates:
            return key
    return None


def test_each_bucket_datapoint_is_exposed_under_its_own_key():
    for datapoint, key in BUCKET_KEYS.items():
        assert _counter_key_for(datapoint) == key, datapoint


def test_the_other_bucket_is_not_dropped_on_the_floor():
    """d702 was missing entirely, so the four buckets could never reconcile."""
    assert _counter_key_for("d702_tot_bev_other") is not None


def test_the_bucket_names_no_longer_claim_to_be_totals_or_water():
    """The two labels that were actively wrong, guarded by meaning not wording."""
    names = {key: name for _c, key, name, _i in const.COUNTER_SENSORS}
    assert "Black" in names["total_beverages"]
    assert names["total_beverages"] != "Total Beverages"
    assert "Water" not in names["total_water"]
    assert "Milk" in names["total_water"]
