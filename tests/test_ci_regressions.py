"""Focused CI regressions captured from field reports."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

PKG_DIR = (
    Path(__file__).resolve().parents[1] / "custom_components" / "delonghi_coffeelink"
)
PKG_NAME = "delonghi_ci_regressions"

package = types.ModuleType(PKG_NAME)
package.__path__ = [str(PKG_DIR)]
sys.modules[PKG_NAME] = package


def _load(name: str):
    full_name = f"{PKG_NAME}.{name}"
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


_load("const")
command_builder = _load("command_builder")


def test_eletta_field_frame_signature_and_app_id():
    """Keep the exact valid frame, signature and decimal app id from issue #15."""
    captured = "DQ+D8AIDAQBuAgMnAQa/qWp4qtoAxYYh"
    decoded = command_builder.decode_command(captured)

    assert decoded["type"] == "beverage"
    assert decoded["beverage_id"] == "0x02"
    assert decoded["recipe"] == "01 00 6e 02 03 27 01 06"
    assert decoded["crc_valid"] is True

    signature = command_builder.device_signature_from_frame(captured)
    assert signature == bytes.fromhex("00 c5 86 21")
    assert int.from_bytes(signature, "big") == 12_944_929
