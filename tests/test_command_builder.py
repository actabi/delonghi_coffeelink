"""Unit tests for the pure command builder / decoder logic.

These tests load only the dependency-free modules (`const`, `command_builder`)
directly, without importing the package `__init__` (which pulls in Home
Assistant). That keeps them runnable with just `pytest` installed.

Payloads below are REAL frames captured from the GitHub issue threads (logged as
"Sending ... value=" by the integration itself), so they are known-good and let
us assert the decoder against ground truth.
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
    spec = importlib.util.spec_from_file_location(full, PKG_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


# Stub the parent package so the modules' relative imports resolve, WITHOUT
# executing the real __init__.py (which imports homeassistant/voluptuous).
if "delonghi_coffeelink" not in sys.modules:
    _pkg = types.ModuleType("delonghi_coffeelink")
    _pkg.__path__ = [str(PKG_DIR)]
    sys.modules["delonghi_coffeelink"] = _pkg

const = _load("const", "const.py")
cb = _load("command_builder", "command_builder.py")
mp = _load("model_profiles", "model_profiles.py")


# --- CRC -------------------------------------------------------------------

def test_crc16_aug_ccitt_known_vector():
    # Hot Water header (12 bytes) -> CRC 0x8124 (from captured frame).
    header = bytes.fromhex("0d0d83f010010f00fa1b0106")
    assert cb.crc16_aug_ccitt(header) == 0x8124


# --- build_beverage_command -----------------------------------------------

def test_build_beverage_command_structure():
    cmd = cb.build_beverage_command(0x10, const.ACTION_START, timestamp=0x6a20b3db)
    assert cmd.hex(" ") == "0d 0d 83 f0 10 01 0f 00 fa 1b 01 06 81 24 6a 20 b3 db"


def test_build_beverage_command_rejects_bad_param_length():
    with pytest.raises(ValueError):
        cb.build_beverage_command(0x01, 0x01, params=b"\x00")


def test_build_wake_command_structure():
    cmd = cb.build_wake_command(timestamp=0x6a1744a2)
    assert cmd.hex(" ") == "0d 07 84 0f 02 01 55 12 6a 17 44 a2"


# --- decode_command: beverage ---------------------------------------------

@pytest.mark.parametrize(
    "b64, bev_id, bev_name, params",
    [
        ("DQ2D8BABDwD6GwEGgSRqILPb", "0x10", "Hot Water", "0f 00 fa 1b 01 06"),
        ("DQ2D8AEBDwD6GwEG+0NqILPw", "0x01", "Espresso", "0f 00 fa 1b 01 06"),
        ("DQ2D8BYBDwD6GwEGAe9qIcfY", "0x16", "Tea", "0f 00 fa 1b 01 06"),
    ],
)
def test_decode_beverage_real_frames(b64, bev_id, bev_name, params):
    d = cb.decode_command(b64)
    assert d["type"] == "beverage"
    assert d["beverage_id"] == bev_id
    assert d["beverage_name"] == bev_name
    assert d["action"] == 1
    assert d["action_name"] == "start"
    assert d["params"] == params
    assert d["crc_valid"] is True
    assert "timestamp" in d


def test_decode_power_real_frame():
    d = cb.decode_command("DQeEDwIBVRJqF0Si")
    assert d["type"] == "power"
    assert d["family"] == "84 0f"
    assert d["params"] == "02 01"
    assert d["crc_valid"] is True
    assert d["timestamp"] == 0x6a1744a2


def test_decode_tolerates_ayla_trailing_newline():
    # Ayla returns datapoint values wrapped in whitespace (a real captured app
    # wake came back as 'DQeEDwIBVRJqIf9q\n'); the decoder must normalise it.
    d = cb.decode_command("DQeEDwIBVRJqIf9q\n")
    assert d["type"] == "power"
    assert d["crc_valid"] is True
    assert d["raw_b64"] == "DQeEDwIBVRJqIf9q"  # cleaned, no newline
    # ...and still compares equal to the integration's own wake.
    assert cb.builder_structural_b64(d) == d["structural_b64"]


# --- decode_command: robustness -------------------------------------------

def test_decode_rejects_non_base64():
    d = cb.decode_command("not base64 !!!")
    assert "error" in d and d.get("type") is None


def test_decode_rejects_empty_and_non_string():
    assert "error" in cb.decode_command("")
    assert "error" in cb.decode_command(None)  # type: ignore[arg-type]


def test_decode_unknown_frame_still_hex_dumps():
    d = cb.decode_command(base64.b64encode(b"\x01\x02\x03\x04\x05").decode())
    assert d["type"] == "unknown"
    assert d["hex"] == "01 02 03 04 05"


def test_decode_machine_response_prefix():
    # Response frames start with 0xd0 (machine -> app).
    d = cb.decode_command(base64.b64encode(bytes([0xd0, 0x0d, 0x83, 0xf0, 0x00])).decode())
    assert d["type"] == "machine_response"


# --- structural comparison (the key diagnostic) ----------------------------

def test_builder_structural_matches_for_integration_frame():
    """A frame the integration itself produced must compare equal structurally."""
    d = cb.decode_command("DQ2D8BABDwD6GwEGgSRqILPb")  # hot water, integration-built
    assert cb.builder_structural_b64(d) == d["structural_b64"]


def test_builder_structural_detects_param_difference():
    """If the recipe params differ, the structural prefix must differ - this is
    exactly how an Eletta app capture with different bytes would be flagged."""
    altered = cb.build_beverage_command(0x10, 0x01, params=bytes([0x0f, 0x00, 0xff, 0x1b, 0x01, 0x06]))
    d = cb.decode_command(base64.b64encode(altered).decode())
    assert d["type"] == "beverage"
    assert cb.builder_structural_b64(d) != d["structural_b64"]


def test_builder_structural_none_for_unknown():
    d = cb.decode_command(base64.b64encode(b"\x01\x02\x03\x04").decode())
    assert cb.builder_structural_b64(d) is None


# --- summary string --------------------------------------------------------

def test_summarize_beverage_includes_match_flag():
    d = cb.decode_command("DQ2D8AEBDwD6GwEG+0NqILPw")  # espresso
    d["matches_integration"] = True
    s = cb.summarize_decoded(d)
    assert "Espresso" in s and "matches_integration=True" in s


# --- Eletta Explore (DL-striker-cb) variable-length frames ------------------
#
# Real frames captured from the official Coffee Link app via the v0.3.3
# diagnostic sniffer (issue #1, MrSpongy ECAM45x). Each is the full app payload
# (header + variable recipe block + 01 0a trailer + CRC + timestamp + 4-byte
# device signature). The integration emits everything EXCEPT the device
# signature, so build_eletta_beverage_command must reproduce frame[:-4].

# (name, bev_id, action, recipe_hex, full_app_frame_hex, timestamp)
_ELETTA_FRAMES = [
    (
        "Hot Water", 0x10, 0x03, "0f 00 96 1b 01 1c 01 27",
        "0d 11 83 f0 10 03 0f 00 96 1b 01 1c 01 27 01 0a 9a 26 6a 24 39 14 00 d3 2f 8c",
        0x6a243914,
    ),
    (
        "Espresso", 0x01, 0x02, "01 00 28 02 04 08 00 1b",
        "0d 11 83 f0 01 02 01 00 28 02 04 08 00 1b 01 0a 7e 68 6a 24 68 ef 00 d3 2f 8c",
        0x6a2468ef,
    ),
    (
        "Cappuccino", 0x07, 0x03, "01 00 41 02 03 09 00 d3 0b 02 1b 01 1c 02 27",
        "0d 18 83 f0 07 03 01 00 41 02 03 09 00 d3 0b 02 1b 01 1c 02 27 01 0a d3 c7 "
        "6a 24 68 50 00 d3 2f 8c",
        0x6a246850,
    ),
    (
        "Flat White", 0x0a, 0x03,
        "01 00 5a 02 03 09 01 90 0b 01 0c 01 1b 03 1c 02 27",
        "0d 1a 83 f0 0a 03 01 00 5a 02 03 09 01 90 0b 01 0c 01 1b 03 1c 02 27 01 0a "
        "ed 36 6a 24 67 ce 00 d3 2f 8c",
        0x6a2467ce,
    ),
]


@pytest.mark.parametrize(
    "name, bev_id, action, recipe_hex, frame_hex, ts", _ELETTA_FRAMES
)
def test_eletta_build_reproduces_app_frame(name, bev_id, action, recipe_hex, frame_hex, ts):
    """The Eletta builder must reproduce the app's frame byte-for-byte (minus the
    4-byte device signature the app appends and the integration does not)."""
    recipe = bytes.fromhex(recipe_hex.replace(" ", ""))
    built = cb.build_eletta_beverage_command(bev_id, action, recipe, timestamp=ts)
    app_frame = bytes.fromhex(frame_hex.replace(" ", ""))
    assert built == app_frame[:-4]  # drop the device signature


@pytest.mark.parametrize(
    "name, bev_id, action, recipe_hex, frame_hex, ts", _ELETTA_FRAMES
)
def test_eletta_decode_variable_length(name, bev_id, action, recipe_hex, frame_hex, ts):
    """Decoding a captured Eletta frame yields style=eletta, a valid CRC (proving
    the existing CRC algorithm already covers Eletta), and the full recipe block."""
    b64 = base64.b64encode(bytes.fromhex(frame_hex.replace(" ", ""))).decode()
    d = cb.decode_command(b64)
    assert d["type"] == "beverage"
    assert d["style"] == "eletta"
    assert d["beverage_id"] == f"0x{bev_id:02x}"
    assert d["action"] == action
    assert d["recipe"] == recipe_hex
    assert d["crc_valid"] is True
    assert d["timestamp"] == ts


def test_eletta_roundtrip_decode_of_built_frame():
    """build -> decode round-trip preserves the recipe block."""
    recipe = bytes.fromhex("01 00 28 02 04 08 00 1b".replace(" ", ""))
    built = cb.build_eletta_beverage_command(0x01, 0x02, recipe, timestamp=0x6a2468ef)
    d = cb.decode_command(base64.b64encode(built).decode())
    assert d["style"] == "eletta"
    assert bytes.fromhex(d["recipe"].replace(" ", "")) == recipe
    assert d["crc_valid"] is True


def test_eletta_structural_is_not_compared():
    """Eletta frames are replayed from captured bytes, so builder_structural_b64
    returns None (a synthesized comparison would be meaningless)."""
    frame_hex = _ELETTA_FRAMES[0][4]
    d = cb.decode_command(base64.b64encode(bytes.fromhex(frame_hex.replace(" ", ""))).decode())
    assert d["style"] == "eletta"
    assert cb.builder_structural_b64(d) is None


def test_soul_frame_still_decodes_as_soul():
    """No regression: the fixed Soul frame keeps style=soul and its 6-byte recipe."""
    d = cb.decode_command("DQ2D8BABDwD6GwEGgSRqILPb")  # Soul hot water
    assert d["style"] == "soul"
    assert d["recipe"] == "0f 00 fa 1b 01 06"
    assert d["crc_valid"] is True


# --- replay_with_timestamp (Eletta verbatim frame replay) ------------------

def test_replay_swaps_only_timestamp_and_keeps_crc_valid():
    """Replaying a captured Eletta frame changes only the 4 timestamp bytes; the
    action, recipe block, CRC and trailing device signature are all preserved,
    and the CRC stays valid (the timestamp is outside the checksummed region)."""
    app_frame_hex = (
        "0d 18 83 f0 07 03 01 00 41 02 03 09 00 d3 0b 02 1b 01 1c 02 27 01 0a d3 c7 "
        "6a 24 68 50 00 d3 2f 8c"  # Cappuccino, with original ts + device signature
    )
    original = base64.b64encode(bytes.fromhex(app_frame_hex.replace(" ", ""))).decode()
    replayed = cb.replay_with_timestamp(original, timestamp=0x11223344)
    orig_raw = base64.b64decode(original)
    new_raw = base64.b64decode(replayed)
    # frame_len = length byte + 1; timestamp lives at [frame_len : frame_len+4].
    frame_len = orig_raw[1] + 1
    assert new_raw[:frame_len] == orig_raw[:frame_len]          # frame + CRC intact
    assert new_raw[frame_len : frame_len + 4] == bytes.fromhex("11223344")
    assert new_raw[frame_len + 4 :] == orig_raw[frame_len + 4 :]  # device signature kept
    # And it still decodes as a valid Eletta frame.
    d = cb.decode_command(replayed)
    assert d["style"] == "eletta" and d["crc_valid"] is True
    assert d["timestamp"] == 0x11223344


def test_replay_wake_preserves_device_signature():
    """The app's power-on frame carries a 4-byte device signature after the
    timestamp that a synthesized wake lacks (the reason a built wake is ignored).
    Replaying must keep that signature and only swap the timestamp."""
    # Real app power-on capture (MrSpongy): 8-byte wake + ts + 00 d3 2f 8c sig.
    app_wake_hex = "0d 07 84 0f 02 01 55 12 6a 24 79 c0 00 d3 2f 8c"
    original = base64.b64encode(bytes.fromhex(app_wake_hex.replace(" ", ""))).decode()
    replayed = base64.b64decode(cb.replay_with_timestamp(original, timestamp=0x11223344))
    assert replayed.hex(" ") == "0d 07 84 0f 02 01 55 12 11 22 33 44 00 d3 2f 8c"
    d = cb.decode_command(original)
    assert d["type"] == "power" and d["crc_valid"] is True


def test_replay_tolerates_garbage():
    """Never raises on odd input (diagnostic/runtime safety)."""
    assert isinstance(cb.replay_with_timestamp("AAEC", timestamp=1), str)  # too short


# --- recipe datapoint dump (zero-touch diagnostic) -------------------------

def test_recipe_dump_lines_selects_and_decodes():
    """Only recipe datapoints (+ active profile) are dumped; base64 blobs decode
    to hex, non-recipe properties are ignored."""
    esp_b64 = base64.b64encode(bytes.fromhex("01 00 28 02 04 08 00 1b".replace(" ", ""))).decode()
    props = {
        "d059_rec_1_espresso": {"value": esp_b64},
        "d286_mach_sett_profile": {"value": 1},
        "software_version": {"value": "1.2.3"},   # not a recipe -> skipped
        "d704_tot_bev_espressi": {"value": "x"},   # counter, not _rec_ -> skipped
    }
    lines = cb.recipe_dump_lines(props)
    assert lines == [
        "d059_rec_1_espresso = 01 00 28 02 04 08 00 1b",
        "d286_mach_sett_profile = 1",
    ]


def test_recipe_dump_lines_handles_non_base64_and_empty():
    """Non-base64 strings are shown as-is; missing/None values never raise."""
    props = {
        "d060_rec_1_regular": {"value": "not base64 !!"},
        "d061_rec_1_long_coffee": {"value": None},
        "d062_rec_1_2x_espresso": "raw-string-not-dict",
    }
    lines = cb.recipe_dump_lines(props)
    assert "d060_rec_1_regular = not base64 !!" in lines
    assert any(line.startswith("d061_rec_1_long_coffee = ") for line in lines)
    assert "d062_rec_1_2x_espresso = raw-string-not-dict" in lines


# --- model profiles (per-oem behaviour, extensible) ------------------------

def test_profile_detection_by_oem_model():
    """Known oem_model families resolve to their profile."""
    assert mp.profile_for("DL-millcore").key == "soul"
    assert mp.profile_for("DL-striker-cb").key == "eletta"
    # Prefix match, not exact.
    assert mp.profile_for("DL-millcore-x").key == "soul"


def test_profile_unknown_model_defaults_sensibly():
    """Unknown model: replay (eletta-style) works on any machine, so it's the
    default - unless the plain data_request channel says it's Soul-like."""
    assert mp.profile_for(None).key == "eletta"
    assert mp.profile_for("DL-future-xyz").key == "eletta"
    assert mp.profile_for("DL-future-xyz", command_property="data_request").key == "soul"
    assert mp.profile_for("DL-future-xyz", command_property="app_data_request").key == "eletta"


def test_soul_profile_synthesizes_commands():
    """Soul does not learn; it always returns a synthesized command value."""
    soul = mp.profile_for("DL-millcore")
    assert soul.learns_from_app is False
    # Returns a real value regardless of learned frames (synthesized).
    val = soul.beverage_value(0x10, const.ACTION_START, learned_frame=None)
    assert isinstance(val, str) and val
    assert isinstance(soul.wake_value(None), str)


def test_eletta_profile_requires_learned_frame():
    """Eletta learns; without a learned frame it signals None (needs teaching),
    with one it replays it (timestamp refreshed)."""
    eletta = mp.profile_for("DL-striker-cb")
    assert eletta.learns_from_app is True
    assert eletta.beverage_value(0x01, const.ACTION_START, learned_frame=None) is None
    assert eletta.wake_value(None) is None
    # With a learned frame -> replays it as a valid frame.
    learned = base64.b64encode(
        bytes.fromhex("0d 07 84 0f 02 01 55 12 6a 24 79 c0 00 d3 2f 8c".replace(" ", ""))
    ).decode()
    out = eletta.wake_value(learned)
    assert isinstance(out, str)
    d = cb.decode_command(out)
    assert d["type"] == "power" and d["crc_valid"] is True


# --- learned-frame persistence (serialize/deserialize) ---------------------

def test_learned_frames_roundtrip():
    """Serialize -> deserialize must preserve per-beverage frames and the wake
    frame, with int beverage ids restored from their hex string keys."""
    start = {0x01: "ESPRESSO_B64", 0x10: "HOTWATER_B64"}
    stop = {0x01: "ESPRESSO_STOP_B64"}
    wake = "WAKE_B64"
    data = cb.serialize_learned_frames(start, stop, wake)
    # JSON-safe: keys are strings.
    assert data == {
        "start": {"0x01": "ESPRESSO_B64", "0x10": "HOTWATER_B64"},
        "stop": {"0x01": "ESPRESSO_STOP_B64"},
        "wake": "WAKE_B64",
    }
    back_start, back_stop, back_wake = cb.deserialize_learned_frames(data)
    assert back_start == start
    assert back_stop == stop
    assert back_wake == wake


def test_serialize_omits_absent_wake():
    """No wake learned yet -> no 'wake' key (and round-trips to None)."""
    data = cb.serialize_learned_frames({0x01: "E"}, {})
    assert "wake" not in data
    assert cb.deserialize_learned_frames(data) == ({0x01: "E"}, {}, None)


def test_deserialize_tolerates_missing_and_bad_data():
    """A missing file (None), partial sections, or junk entries never raise."""
    assert cb.deserialize_learned_frames(None) == ({}, {}, None)
    assert cb.deserialize_learned_frames({}) == ({}, {}, None)
    # Bad key / non-string value are skipped, good ones kept; bad wake -> None.
    start, stop, wake = cb.deserialize_learned_frames(
        {"start": {"0x07": "ok", "zz": "bad-key", "0x09": 123}, "stop": None, "wake": 9}
    )
    assert start == {0x07: "ok"}
    assert stop == {}
    assert wake is None


def test_summarize_handles_error():
    assert "undecodable" in cb.summarize_decoded(cb.decode_command(""))
