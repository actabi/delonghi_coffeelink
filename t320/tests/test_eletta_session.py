"""Eletta Explore cloud-session identity and frame learning (issue #15).

Two independent bugs made the integration unable to drive an Eletta Explore:

1. the cloud session was registered with a made-up constant
   (``INTEGRATION_CLOUD_APP_ID``) instead of the machine's own 4-byte device
   signature, so Ayla accepted and confirmed the session while the machine
   silently ignored every command;
2. beverages whose recipe block does not end with ``01 0a`` were decoded as
   Soul-dialect frames and dropped by the learning gate, so they could never be
   taught from the official app.

The frames below are real captures from the issue threads (a signature of
``00 c5 86 21`` -> app id 12944929, confirmed working live by the reporter).

Like the other suites here, only dependency-free modules are loaded so the tests
run with plain pytest (no Home Assistant install).
"""
from __future__ import annotations

import base64
import importlib.util
import sys
import types
from pathlib import Path

import pytest

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "delonghi_coffeelink"


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

const = _load("const", "const.py")
cb = _load("command_builder", "command_builder.py")
ac = _load("ayla_client", "ayla_client.py")

# Eletta Explore 450.65.S (DL-striker-cb), captured from the official app.
# 0d 0f 83 f0 02 03 | 01 00 6e 02 03 27 01 06 | bf a9 | 6a 78 aa da | 00 c5 86 21
#   header  bev act   recipe block (ends 01 06!)  crc     timestamp    signature
COFFEE_FRAME = "DQ+D8AIDAQBuAgMnAQa/qWp4qtoAxYYh"
COFFEE_SIGNATURE = bytes.fromhex("00c58621")
COFFEE_APP_ID = 12944929  # 0x00C58621 - the id this machine actually honours

# Same family, recipe block ending with the historical 01 0a bytes.
ESPRESSO_FRAME_HEX = (
    "0d 11 83 f0 01 02 01 00 28 02 04 08 00 1b 01 0a 7e 68 6a 24 68 ef 00 d3 2f 8c"
)
ESPRESSO_FRAME = base64.b64encode(bytes.fromhex(ESPRESSO_FRAME_HEX.replace(" ", ""))).decode()

# Integration-built Soul frame: fixed 14-byte shape, no signature appended.
SOUL_FRAME = "DQ2D8BABDwD6GwEGgSRqILPb"


# --- device signature -> cloud session id ----------------------------------

def test_app_id_derived_from_captured_signature():
    """The id the machine honours is its signature read as a signed int32 BE."""
    sig = cb.device_signature_from_frame(COFFEE_FRAME)
    assert sig == COFFEE_SIGNATURE
    assert cb.app_id_from_signature(sig) == COFFEE_APP_ID


def test_app_id_matches_session_tail_encoding():
    """The derived id round-trips to the exact bytes used as the session tail."""
    app_id = cb.app_id_from_signature(COFFEE_SIGNATURE)
    assert cb._session_id_to_tail_bytes(app_id) == COFFEE_SIGNATURE
    # ayla_client normalises the id the same way before POSTing the session.
    assert ac.normalize_signed_app_id(app_id) == app_id
    assert ac.integration_app_id_to_bytes(app_id) == COFFEE_SIGNATURE


def test_app_id_from_high_bit_signature_is_negative_and_round_trips():
    """A signature with the high bit set becomes a negative (signed) app id."""
    sig = bytes.fromhex("c0ffee11")
    app_id = cb.app_id_from_signature(sig)
    assert app_id == -1056969199
    assert cb._session_id_to_tail_bytes(app_id) == sig


@pytest.mark.parametrize(
    "signature", [None, b"", b"\x00\x01\x02", b"\x00\x01\x02\x03\x04", bytes(4)]
)
def test_app_id_rejects_unusable_signature(signature):
    """Missing, malformed or all-zero signatures must fall back, never register 0."""
    assert cb.app_id_from_signature(signature) is None


def test_first_device_signature_scans_frames_and_skips_zeroes():
    zeroed = base64.b64encode(
        base64.b64decode(COFFEE_FRAME)[:-4] + bytes(4)
    ).decode()
    assert cb.first_device_signature([None, SOUL_FRAME, zeroed]) is None
    assert cb.first_device_signature([None, zeroed, COFFEE_FRAME]) == COFFEE_SIGNATURE


def test_two_machines_keep_distinct_session_ids():
    """Signatures are per machine, so two devices never share a session id."""
    other = cb.first_device_signature([ESPRESSO_FRAME])
    assert other == bytes.fromhex("00d32f8c")
    assert cb.app_id_from_signature(other) != COFFEE_APP_ID


def test_soul_frame_yields_no_signature():
    """A synthesized Soul frame carries none, so the Soul keeps the constant."""
    assert cb.first_device_signature([SOUL_FRAME]) is None


# --- frame dialect detection ------------------------------------------------

def test_coffee_frame_is_not_mistaken_for_a_soul_frame():
    """Recipe trailer 01 06: the length byte, not the trailer, decides the dialect."""
    d = cb.decode_command(COFFEE_FRAME)
    assert d["type"] == "beverage"
    assert d["style"] == "eletta"
    assert d["beverage_id"] == "0x02"
    assert d["beverage_name"] == "Coffee"
    assert d["crc_valid"] is True
    assert d["recipe"] == "01 00 6e 02 03 27 01 06"
    assert d["timestamp"] == 0x6A78AADA


def test_eletta_start_action_byte_is_named():
    """The app's start action on this dialect is 0x03, not 0x01."""
    assert cb.decode_command(COFFEE_FRAME)["action"] == 0x03
    assert cb.decode_command(COFFEE_FRAME)["action_name"] == "start"


def test_soul_frame_still_decodes_as_soul():
    d = cb.decode_command(SOUL_FRAME)
    assert d["style"] == "soul"
    assert d["recipe"] == "0f 00 fa 1b 01 06"
    assert d["params"] == "0f 00 fa 1b 01 06"


# --- learning gate ----------------------------------------------------------

def test_coffee_frame_is_learnable():
    """The regression of issue #15 bug 2: this frame used to be silently dropped."""
    assert cb.learnable_beverage_id(cb.decode_command(COFFEE_FRAME)) == 0x02


def test_trailered_frame_still_learnable():
    assert cb.learnable_beverage_id(cb.decode_command(ESPRESSO_FRAME)) == 0x01


def test_soul_shaped_frame_is_learnable_too():
    """The gate is dialect-agnostic: the model profile decides who learns."""
    assert cb.learnable_beverage_id(cb.decode_command(SOUL_FRAME)) == 0x10


def test_frame_matching_our_own_builder_is_not_learnable():
    """Our own best-effort fallback must never be learned as if the app sent it."""
    decoded = cb.decode_command(SOUL_FRAME)
    decoded["matches_integration"] = True
    assert cb.learnable_beverage_id(decoded) is None


def test_corrupted_frame_is_not_learnable():
    raw = bytearray(base64.b64decode(COFFEE_FRAME))
    raw[8] ^= 0xFF  # flip a recipe byte -> CRC no longer matches
    d = cb.decode_command(base64.b64encode(bytes(raw)).decode())
    assert d["crc_valid"] is False
    assert cb.learnable_beverage_id(d) is None


def test_unknown_beverage_id_is_not_learnable():
    raw = bytearray(base64.b64decode(COFFEE_FRAME))
    raw[4] = 0xFE  # beverage id this integration does not expose
    frame_len = raw[1] + 1
    raw[frame_len - 2 : frame_len] = cb.crc16_aug_ccitt(
        bytes(raw[0 : frame_len - 2])
    ).to_bytes(2, "big")
    d = cb.decode_command(base64.b64encode(bytes(raw)).decode())
    assert d["crc_valid"] is True
    assert cb.learnable_beverage_id(d) is None


@pytest.mark.parametrize(
    "value",
    [
        "DQeEDwIBVRJqF0Si",  # wake/power frame
        "not base64 !!!",
        "",
    ],
)
def test_non_beverage_values_are_not_learnable(value):
    assert cb.learnable_beverage_id(cb.decode_command(value)) is None
