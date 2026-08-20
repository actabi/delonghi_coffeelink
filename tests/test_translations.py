"""Regression tests for English and Czech translation completeness."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

COMPONENT_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "delonghi_coffeelink"
)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def _leaf_keys(value: dict[str, Any], prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            keys.update(_leaf_keys(item, path))
        else:
            keys.add(path)
    return keys


def test_english_and_czech_match_source_translation_keys():
    source = _load_json(COMPONENT_DIR / "strings.json")
    expected = _leaf_keys(source)

    for language in ("en", "cs"):
        translation = _load_json(COMPONENT_DIR / "translations" / f"{language}.json")
        assert _leaf_keys(translation) == expected


def test_enum_state_keys_are_complete():
    source = _load_json(COMPONENT_DIR / "strings.json")
    sensors = source["entity"]["sensor"]

    assert set(sensors["connection_status"]["state"]) == {
        "online",
        "offline",
        "unknown",
    }
    assert set(sensors["machine_status"]["state"]) == {
        "standby",
        "waking_up",
        "going_to_sleep",
        "descaling",
        "preparing_steam",
        "recovering",
        "ready",
        "rinsing",
        "preparing_milk",
        "dispensing_hot_water",
        "cleaning_milk",
        "preparing_chocolate",
        "preparing_milk_alt",
        "unknown",
    }


def test_services_use_translated_metadata_and_selector_options():
    services_yaml = (COMPONENT_DIR / "services.yaml").read_text(encoding="utf-8")
    source = _load_json(COMPONENT_DIR / "strings.json")

    assert set(source["services"]) == {
        "start_beverage",
        "stop_beverage",
        "send_raw_command",
    }
    assert "translation_key: beverage" in services_yaml
    assert set(source["selector"]["beverage"]["options"]) == {
        "espresso",
        "coffee",
        "long_coffee",
        "double_espresso",
        "doppio",
        "americano",
        "cappuccino",
        "latte_macchiato",
        "caffelatte",
        "flat_white",
        "espresso_macchiato",
        "hot_milk",
        "cappuccino_doppio",
        "cappuccino_reverse",
        "hot_water",
        "tea",
        "coffee_pot",
        "cortado",
        "long_black",
        "mug_to_go",
        "brew_over_ice",
    }
