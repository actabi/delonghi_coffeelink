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
