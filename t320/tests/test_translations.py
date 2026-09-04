"""Regression tests for translation completeness.

Everything here is derived, never hard-coded: the expected key set comes from
``strings.json``, the languages come from whatever sits in ``translations/``,
and the enum/selector option lists come from ``const.py``. Adding a language, a
machine status or a beverage therefore fails the suite until it is translated,
which is the whole point - a hard-coded expectation only checks the languages
someone remembered to list.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

COMPONENT_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "delonghi_coffeelink"
)
TRANSLATIONS_DIR = COMPONENT_DIR / "translations"

_PKG_NAME = "delonghi_translations_under_test"
_package = types.ModuleType(_PKG_NAME)
_package.__path__ = [str(COMPONENT_DIR)]
sys.modules[_PKG_NAME] = _package
_spec = importlib.util.spec_from_file_location(
    f"{_PKG_NAME}.const", COMPONENT_DIR / "const.py"
)
assert _spec and _spec.loader
const = importlib.util.module_from_spec(_spec)
sys.modules[f"{_PKG_NAME}.const"] = const
_spec.loader.exec_module(const)


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


def _languages() -> list[str]:
    return sorted(path.stem for path in TRANSLATIONS_DIR.glob("*.json"))


def test_translation_files_are_discovered():
    """Guard the guard: a glob that matches nothing would pass every test."""
    languages = _languages()
    assert "en" in languages
    assert len(languages) >= 2


def test_every_language_matches_source_translation_keys():
    """No language may lag behind strings.json - not even the maintainer's."""
    expected = _leaf_keys(_load_json(COMPONENT_DIR / "strings.json"))

    incomplete: dict[str, list[str]] = {}
    for language in _languages():
        keys = _leaf_keys(_load_json(TRANSLATIONS_DIR / f"{language}.json"))
        if missing := sorted(expected - keys):
            incomplete[language] = missing
        assert not keys - expected, f"{language}.json has unknown keys"

    assert not incomplete, f"untranslated keys: {incomplete}"


def test_machine_status_states_cover_every_decoded_status():
    """Every MACHINE_STATUS value must have a display string."""
    sensors = _load_json(COMPONENT_DIR / "strings.json")["entity"]["sensor"]

    assert set(sensors["machine_status"]["state"]) == set(
        const.MACHINE_STATUS.values()
    )
    assert set(sensors["machine_status"]["state"]) == set(
        const.MACHINE_STATUS_OPTIONS
    )
    assert set(sensors["connection_status"]["state"]) == set(
        const.CONNECTION_STATUS_OPTIONS
    )


def test_services_use_translated_metadata_and_selector_options():
    """Action metadata lives in strings.json, and every beverage is selectable."""
    services_yaml = (COMPONENT_DIR / "services.yaml").read_text(encoding="utf-8")
    source = _load_json(COMPONENT_DIR / "strings.json")

    assert set(source["services"]) == {
        const.SERVICE_START_BEVERAGE,
        const.SERVICE_STOP_BEVERAGE,
        const.SERVICE_SEND_RAW_COMMAND,
    }
    assert "translation_key: beverage" in services_yaml
    assert set(source["selector"]["beverage"]["options"]) == {
        beverage[1] for beverage in const.BEVERAGES
    }


def test_service_names_and_descriptions_are_not_in_services_yaml():
    """Modern HA reads action metadata from strings.json, not services.yaml."""
    services_yaml = (COMPONENT_DIR / "services.yaml").read_text(encoding="utf-8")

    assert "name:" not in services_yaml
    assert "description:" not in services_yaml
