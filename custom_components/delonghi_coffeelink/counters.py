"""Pure counter-value parsing for DeLonghi datapoints.

Kept free of Home Assistant imports so the parsing logic is unit-testable on its
own (see tests/test_counters.py). Two value shapes exist across models:

- PrimaDonna Soul (DL-millcore): counters are plain integers (e.g. ``314``).
- Eletta Explore (DL-striker-cb): some counters are published as a JSON object
  of per-recipe sub-counts (e.g. ``{"espresso": 12, "coffee": 3}``), which left
  the sensor ``unknown`` before #7. For those, the sensor state is the sum of
  the integer sub-values and the raw object is exposed as attributes.
"""
from __future__ import annotations

import json
from typing import Any


def _looks_like_json_object(val_str: str) -> bool:
    return val_str.startswith("{") and val_str.endswith("}")


def parse_counter_value(val: Any) -> int | None:
    """Return the integer state for a counter datapoint value, or ``None``.

    Plain integers pass through. A JSON object is summed over its integer
    sub-values. Anything else (booleans, unparseable strings, malformed JSON)
    yields ``None`` so the sensor stays unknown rather than guessing.
    """
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    val_str = str(val).strip()
    if _looks_like_json_object(val_str):
        try:
            data = json.loads(val_str)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        total = 0
        for sub in data.values():
            try:
                total += int(sub)
            except (ValueError, TypeError):
                pass
        return total
    try:
        return int(val_str)
    except (TypeError, ValueError):
        return None


def counter_breakdown(val: Any) -> dict | None:
    """Return the per-recipe JSON breakdown of a counter value, else ``None``.

    Only JSON-object values (Eletta aggregated counters) have a breakdown; plain
    integers and unparseable values return ``None``.
    """
    if not val or isinstance(val, (int, bool)):
        return None
    val_str = str(val).strip()
    if _looks_like_json_object(val_str):
        try:
            data = json.loads(val_str)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None
    return None
