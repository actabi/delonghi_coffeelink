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
import json
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
mon = _load("monitor", "monitor.py")
ac = _load("ayla_client", "ayla_client.py")


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


# --- standby command ---------------------------------------------------------

def test_build_standby_command_structure():
    # Frame validated LIVE on the reference Soul (machine powered off,
    # 2026-06-07): 0d 07 84 0f 01 01 00 41 <ts>.
    cmd = cb.build_standby_command(timestamp=0x6A258952)
    assert cmd.hex(" ") == "0d 07 84 0f 01 01 00 41 6a 25 89 52"


def test_build_standby_command_with_signature():
    # Eletta-style: the 4-byte device signature goes AFTER the timestamp,
    # so the CRC is unchanged.
    sig = bytes.fromhex("00d32f8c")
    cmd = cb.build_standby_command(timestamp=0x6A258952, signature=sig)
    assert cmd.hex(" ") == "0d 07 84 0f 01 01 00 41 6a 25 89 52 00 d3 2f 8c"
    assert cb.crc16_aug_ccitt(cmd[0:6]) == 0x0041


def test_decode_standby_is_power_but_not_learnable_as_wake():
    # A standby frame decodes as power family but the wake-learning guard
    # must never store it as the wake frame.
    d = cb.decode_command(base64.b64encode(cb.build_standby_command()).decode())
    assert d["type"] == "power"
    assert d["params"] == "01 01"
    assert d["crc_valid"] is True
    assert cb.is_wake_power_frame(d) is False


def test_standby_profile_values():
    soul = mp.SoulProfile()
    # Soul synthesizes without signature (validated live).
    out = soul.standby_value(None)
    d = cb.decode_command(out)
    assert d["type"] == "power" and d["params"] == "01 01" and d["crc_valid"] is True
    eletta = mp.ElettaProfile()
    # Eletta requires the learned device signature...
    assert eletta.standby_value(None) is None
    # ...and appends it after the timestamp when available.
    out = eletta.standby_value(bytes.fromhex("00d32f8c"))
    raw = base64.b64decode(out)
    assert raw[4:6].hex(" ") == "01 01"
    assert raw[-4:].hex(" ") == "00 d3 2f 8c"


# --- device_signature_from_frame ---------------------------------------------

def test_device_signature_from_learned_wake_frame():
    # Real app power-on capture (MrSpongy): 8-byte wake + ts + 00 d3 2f 8c sig.
    app_wake_hex = "0d 07 84 0f 02 01 55 12 6a 24 79 c0 00 d3 2f 8c"
    b64 = base64.b64encode(bytes.fromhex(app_wake_hex.replace(" ", ""))).decode()
    assert cb.device_signature_from_frame(b64) == bytes.fromhex("00d32f8c")


def test_device_signature_from_beverage_frame():
    # Eletta beverage frames carry the signature too (variable length: the
    # structural part is length_byte + 1). Synthetic frame built accordingly.
    structural = bytes.fromhex("0d1083f010031c0119010f00961b010a81")  # len 0x10 -> 17 bytes
    frame = structural + (0x6A2479C0).to_bytes(4, "big") + bytes.fromhex("00d32f8c")
    b64 = base64.b64encode(frame).decode()
    assert cb.device_signature_from_frame(b64) == bytes.fromhex("00d32f8c")


def test_device_signature_absent_or_junk():
    # 12-byte synthesized wake has no signature.
    b64 = base64.b64encode(cb.build_wake_command(timestamp=0x6A1744A2)).decode()
    assert cb.device_signature_from_frame(b64) is None
    assert cb.device_signature_from_frame(None) is None
    assert cb.device_signature_from_frame("not base64 !!!") is None
    assert cb.device_signature_from_frame("AA==") is None  # too short


# --- cloud session app_id helpers (DlghIoT convention) -----------------------

def test_normalize_signed_app_id():
    # 0xC0FFEE11 has the sign bit set -> negative int32 (matches the decimal
    # form the machine reports in its app_id property).
    assert ac.normalize_signed_app_id(0xC0FFEE11) == 0xC0FFEE11 - 0x100000000
    # Below the sign bit: unchanged.
    assert ac.normalize_signed_app_id(0x7FFFFFFF) == 0x7FFFFFFF
    # Idempotent on already-signed values.
    signed = ac.normalize_signed_app_id(0xC0FFEE11)
    assert ac.normalize_signed_app_id(signed) == signed


def test_integration_app_id_to_bytes():
    # The wire bytes must be the literal big-endian id, sign handled correctly.
    assert ac.integration_app_id_to_bytes(0xC0FFEE11).hex() == "c0ffee11"
    assert ac.integration_app_id_to_bytes(0x7FFFFFFF).hex() == "7fffffff"
    # Round-trip back to the unsigned form.
    raw = ac.integration_app_id_to_bytes(const.INTEGRATION_CLOUD_APP_ID)
    assert int.from_bytes(raw, "big", signed=True) & 0xFFFFFFFF == 0xC0FFEE11


def test_cloud_session_profile_gating():
    # Session is Eletta-only: the Soul (and the generic default) must never
    # register a cloud session.
    assert mp.SoulProfile().uses_cloud_session is False
    assert mp.ModelProfile().uses_cloud_session is False
    assert mp.ElettaProfile().uses_cloud_session is True


# --- monitor (d302_monitor_machine parsing) ----------------------------------

def _make_monitor_blob(status: int, progress: int = 0, accessory: int = 1) -> str:
    """Build a synthetic MonitorV2 EcamPacket: prefix, length, data, crc, ts."""
    contents = bytes([accessory, 0, 0, 0, 0, status, 0, progress]) + bytes(5)
    data = bytes([mon.MONITOR_REQUEST_ID, 0xF0]) + contents
    length = len(data) + 3  # data = raw[2 : length-1]
    head = bytes([0xD0, length]) + data
    crc = cb.crc16_aug_ccitt(head)
    raw = head + crc.to_bytes(2, "big") + (0x6A258952).to_bytes(4, "big")
    return base64.b64encode(raw).decode()


def test_parse_monitor_ready():
    out = mon.parse_monitor_b64(_make_monitor_blob(status=7, progress=3))
    # The synthetic blob carries a 13-byte contents block, so the ECAM
    # switches/alarms bitfields are parsed too (both zero here).
    assert out == {
        "status": 7,
        "status_name": "ready",
        "progress": 3,
        "action": 0,
        "accessory": 1,
        "switches": 0,
        "alarms": 0,
    }


def test_parse_monitor_standby_and_unknown_code():
    assert mon.parse_monitor_b64(_make_monitor_blob(status=0))["status_name"] == "standby"
    out = mon.parse_monitor_b64(_make_monitor_blob(status=99))
    assert out["status_name"] == "unknown" and out["status"] == 99


def test_parse_monitor_rejects_bad_input():
    # Never raises - returns {"error": ...} on anything malformed.
    assert "error" in mon.parse_monitor_b64("")
    assert "error" in mon.parse_monitor_b64("not base64 !!!")
    assert "error" in mon.parse_monitor_b64(base64.b64encode(b"\xd0\x02").decode())
    # Valid envelope but wrong request id (a command response, not MonitorV2).
    resp = bytes.fromhex("d00783f0010064d969e8c98e")
    assert "error" in mon.parse_monitor_b64(base64.b64encode(resp).decode())
    # Corrupted CRC.
    blob = base64.b64decode(_make_monitor_blob(status=7))
    bad = blob[:-5] + bytes([blob[-5] ^ 0xFF]) + blob[-4:]
    assert "error" in mon.parse_monitor_b64(base64.b64encode(bad).decode())


# --- is_wake_power_frame (wake-learning guard) ------------------------------

def test_is_wake_power_frame_accepts_real_wake():
    # Real captured app wake (params 02 01) must be learnable.
    d = cb.decode_command("DQeEDwIBVRJqIf9q")
    assert d["type"] == "power" and d["params"] == "02 01"
    assert cb.is_wake_power_frame(d) is True


def test_is_wake_power_frame_rejects_session_refresh():
    # The app also emits 84 0f frames with params 03 02 (session refresh,
    # Dieter's capture / issue #1). Learning one would overwrite the real
    # power-on frame - the guard must reject it.
    header = bytes.fromhex("0d07840f0302")
    crc = cb.crc16_aug_ccitt(header)
    frame = header + crc.to_bytes(2, "big") + (0x6A24A1BE).to_bytes(4, "big")
    d = cb.decode_command(base64.b64encode(frame).decode())
    assert d["type"] == "power" and d["params"] == "03 02"
    assert d["crc_valid"] is True
    assert cb.is_wake_power_frame(d) is False


def test_is_wake_power_frame_rejects_non_power_frames():
    # Beverage frames and undecodable input are never wake frames.
    bev = cb.decode_command("DQ2D8BABDwD6GwEGgSRqILPb")
    assert bev["type"] == "beverage"
    assert cb.is_wake_power_frame(bev) is False
    assert cb.is_wake_power_frame({"error": "junk"}) is False
    assert cb.is_wake_power_frame({}) is False


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
    assert mp.profile_for("DL-future-xyz", command_property="data_request").key == "soul-generic"
    assert mp.profile_for("DL-future-xyz", command_property="app_data_request").key == "eletta"


def test_an_unknown_soul_like_machine_gets_the_dialect_but_not_the_keepalive():
    """The command dialect generalises; the keepalive payload shape does not.

    A bare unix timestamp in the connected property is confirmed on
    ``DL-millcore`` and nowhere else - the Eletta family takes a different shape
    entirely - so writing it every 15 seconds to a machine nobody has tested is
    a guess repeated 2880 times a day. The rest of the Soul behaviour is safe to
    inherit and does inherit.
    """
    unknown = mp.profile_for("DL-future-xyz", command_property="data_request")
    known = mp.profile_for("DL-millcore")
    assert unknown.keeps_monitor_session is False
    assert known.keeps_monitor_session is True
    assert unknown.monitor_session_value() is not None, "only the flag differs"
    assert unknown.learns_from_app == known.learns_from_app
    assert unknown.command_property == known.command_property
    assert isinstance(unknown, type(known)), "still a Soul, just not a known one"


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


# --- user profile switch (0xa9 0xf0) ----------------------------------------
#
# The frame pair below is the official app switching the reference Soul to
# profile 1 and the machine acknowledging it two seconds later, as captured in
# tests/fixtures/soul_properties.json (data_request / data_response).

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "soul_properties.json"

PROFILE_REQUEST_CRCS = [(1, "d7 c0"), (2, "e7 a3"), (3, "f7 82"), (4, "87 65"), (5, "97 44")]
PROFILE_REPLY_CRCS = [(1, "3b 3c"), (2, "6e 6f"), (3, "5d 5e"), (4, "c4 c9"), (5, "f7 f8")]


@pytest.fixture(scope="module")
def soul_props() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_build_profile_command_reproduces_the_app_frame(soul_props: dict):
    """Profile 1 at the fixture's timestamp is the app's own eleven bytes.

    The fixture's ``data_request`` was written by the official app. A single
    byte of drift would hand the machine a frame it never sees from the app,
    and the sniffer would flag our own frame as not matching the integration.
    """
    raw = base64.b64decode(soul_props["data_request"]["value"])
    assert raw.hex(" ") == "0d 06 a9 f0 01 d7 c0 69 e8 c5 ee"
    assert cb.build_profile_command(1, timestamp=0x69E8C5EE) == raw
    assert cb.build_profile_encoded(1, timestamp=0x69E8C5EE) == "DQap8AHXwGnoxe4="


@pytest.mark.parametrize("profile, crc", PROFILE_REQUEST_CRCS)
def test_build_profile_command_crc_per_slot(profile: int, crc: str):
    """Length byte 0x06 and a CRC over the five header bytes, for every slot.

    0x06 is the frame size through the CRC (7 bytes) minus the start byte,
    the same rule that gives the 8-byte power frame its 0x07; a copy-paste of
    0x07 here would make the machine read the CRC one byte off.
    """
    cmd = cb.build_profile_command(profile, timestamp=0)
    assert len(cmd) == 11
    assert cmd[0] == const.CMD_PREFIX
    assert cmd[1] == 0x06
    assert cmd[2:4] == const.CMD_FAMILY_PROFILE
    assert cmd[4] == profile
    assert cmd[5:7].hex(" ") == crc
    assert cmd[7:11] == bytes(4)


def test_build_profile_command_signature_follows_timestamp_and_leaves_crc_alone():
    """Eletta-style signature goes after the timestamp; the CRC must not see it."""
    sig = bytes.fromhex("00d32f8c")
    plain = cb.build_profile_command(2, timestamp=0x6A258952)
    signed = cb.build_profile_command(2, timestamp=0x6A258952, signature=sig)
    assert signed == plain + sig
    assert signed.hex(" ") == "0d 06 a9 f0 02 e7 a3 6a 25 89 52 00 d3 2f 8c"
    assert cb.crc16_aug_ccitt(signed[0:5]) == int.from_bytes(signed[5:7], "big")
    # The keyword-order of the encoded shortcut mirrors build_standby_encoded.
    assert base64.b64decode(cb.build_profile_encoded(2, 0x6A258952, sig)) == signed


@pytest.mark.parametrize("bad", [0, 256, -1])
def test_build_profile_command_rejects_slots_that_do_not_fit_the_wire(bad: int):
    """0 and 256 never reach the machine: the wire field is one byte, 1-based."""
    with pytest.raises(ValueError):
        cb.build_profile_command(bad)


def test_build_profile_with_session_tail_mirrors_the_standby_helper():
    """The cloud-session tail is the app id as signed int32 BE, after the ts.

    ECAM machines ignore commands whose tail is not their own session id, so
    the profile frame must carry the same tail the standby frame does. The id
    may arrive as the decimal string the machine property holds; ``None``
    means no session and yields the bare frame.
    """
    app_id = -12345678
    tail = cb._session_id_to_tail_bytes(app_id)
    raw = base64.b64decode(cb.build_profile_with_session_tail_encoded(3, app_id, 0))
    assert raw[:7].hex(" ") == "0d 06 a9 f0 03 f7 82"
    assert raw[7:11] == bytes(4)
    assert raw[11:] == tail
    assert cb.build_profile_with_session_tail_encoded(3, str(app_id), 0) == base64.b64encode(
        raw
    ).decode("ascii")
    bare = base64.b64decode(cb.build_profile_with_session_tail_encoded(3, None, 0))
    assert bare == raw[:11]
    d = cb.decode_command(base64.b64encode(raw).decode("ascii"))
    assert d["type"] == "profile" and d["profile"] == 3 and d["crc_valid"] is True


def test_parse_profile_response_reads_the_fixture_reply(soul_props: dict):
    """The machine's acknowledgement: profile 1, status 0, two seconds later.

    The coordinator reads this every poll to learn the active profile, so the
    fields must come out exactly - and the Ayla trailing newline must not
    turn a valid reply into "unknown".
    """
    value = soul_props["data_response"]["value"]
    assert base64.b64decode(value).hex(" ") == "d0 07 a9 f0 01 00 3b 3c 69 e8 c5 f0"
    expected = {"profile": 1, "status": const.PROFILE_RESPONSE_OK, "timestamp": 0x69E8C5F0}
    assert cb.parse_profile_response(value) == expected
    assert cb.parse_profile_response(value + "\n") == expected
    assert cb.parse_profile_response(" " + value + " \n") == expected
    assert cb.parse_profile_response(base64.b64decode(value)) == expected


@pytest.mark.parametrize("profile, crc", PROFILE_REPLY_CRCS)
def test_parse_profile_response_crc_per_slot(profile: int, crc: str):
    """An accepted reply for every slot parses; without a timestamp it is None."""
    frame = bytes([0xD0, 0x07, 0xA9, 0xF0, profile, 0x00]) + bytes.fromhex(crc)
    assert cb.parse_profile_response(frame) == {
        "profile": profile,
        "status": 0,
        "timestamp": None,
    }


def test_parse_profile_response_refuses_everything_that_is_not_a_reply(soul_props: dict):
    """One flipped byte, the wrong direction, another family, or nothing at all.

    The response property is a shared channel: whatever the machine last
    answered sits there. Anything that is not a CRC-valid a9f0 reply must be
    ``None`` so the coordinator keeps the last known profile instead of
    adopting a byte from an unrelated frame.
    """
    reply = bytearray(base64.b64decode(soul_props["data_response"]["value"]))
    reply[5] ^= 0x01  # status 0 -> 1 without recomputing the CRC
    assert cb.parse_profile_response(bytes(reply)) is None
    # The app -> machine request wears the same family but the other prefix.
    assert cb.parse_profile_response(soul_props["data_request"]["value"]) is None
    # Another family on the d0 envelope (the monitor blob) is not a profile.
    assert cb.parse_profile_response(soul_props["d302_monitor"]["value"]) is None
    assert cb.parse_profile_response(soul_props["d001_rec_espresso"]["value"]) is None
    assert cb.parse_profile_response("") is None
    assert cb.parse_profile_response(None) is None
    assert cb.parse_profile_response("AA==") is None
    assert cb.parse_profile_response("not base64 !!!") is None
    assert cb.parse_profile_response(42) is None  # type: ignore[arg-type]
    # Right family and prefix, wrong length byte: not the reply shape.
    wrong_len = bytearray(base64.b64decode(soul_props["data_response"]["value"]))
    wrong_len[1] = 0x08
    assert cb.parse_profile_response(bytes(wrong_len)) is None


def test_parse_profile_response_reports_a_refusal_instead_of_hiding_it():
    """Status 1 (seen for a guest profile) is data, not garbage.

    The coordinator rolls its optimistic state back on a non-zero status; if
    the parser returned ``None`` here the refusal would look like silence and
    the select would keep showing a profile the machine never switched to.
    """
    header = bytes([0xD0, 0x07, 0xA9, 0xF0, 0x06, 0x01])
    frame = header + cb.crc16_aug_ccitt(header).to_bytes(2, "big")
    assert cb.parse_profile_response(frame) == {"profile": 6, "status": 1, "timestamp": None}
    d = cb.decode_command(base64.b64encode(frame).decode("ascii"))
    assert d["type"] == "machine_response"
    assert d["status"] == 1 and d["profile"] == 6 and d["crc_valid"] is True
    assert "status=1" in cb.summarize_decoded(d)


def test_decode_classifies_the_app_profile_frame(soul_props: dict):
    """The sniffer sees the app's frame as ``profile`` and matches it to ours.

    ``structural_b64`` is the frame through the CRC, so an app-written switch
    compares equal to the integration's own and gets matches_integration=True
    - the byte-parity proof the manual verification plan relies on.
    """
    value = soul_props["data_request"]["value"]
    raw = base64.b64decode(value)
    d = cb.decode_command(value)
    assert d["type"] == "profile"
    assert d["profile"] == 1
    assert d["params"] == "01"
    assert d["crc"] == "d7 c0"
    assert d["crc_valid"] is True
    assert d["timestamp"] == 0x69E8C5EE
    assert d["structural_b64"] == base64.b64encode(raw[:7]).decode("ascii")
    assert cb.builder_structural_b64(d) == d["structural_b64"]
    assert cb.learnable_beverage_id(d) is None
    assert cb.is_wake_power_frame(d) is False
    summary = cb.summarize_decoded(d)
    assert summary.startswith("profile id=1") and "crc_valid=True" in summary


def test_decode_profile_frame_flags_a_bad_crc():
    """A corrupted profile byte is reported, not trusted."""
    raw = bytearray(cb.build_profile_command(4, timestamp=0))
    raw[4] = 5
    d = cb.decode_command(base64.b64encode(bytes(raw)).decode("ascii"))
    assert d["type"] == "profile" and d["profile"] == 5
    assert d["crc_valid"] is False
    assert cb.builder_structural_b64(d) != d["structural_b64"]


def test_decode_keeps_the_profile_reply_a_machine_response(soul_props: dict):
    """The reply stays ``machine_response`` (the sniffer keys on that) and
    additionally carries the parsed profile, status, timestamp and CRC verdict."""
    d = cb.decode_command(soul_props["data_response"]["value"])
    assert d["type"] == "machine_response"
    assert d["profile"] == 1
    assert d["status"] == 0
    assert d["timestamp"] == 0x69E8C5F0
    assert d["crc"] == "3b 3c"
    assert d["crc_valid"] is True
    assert cb.builder_structural_b64(d) is None
    summary = cb.summarize_decoded(d)
    assert "profile id=1" in summary and "status=0" in summary


def test_decode_other_machine_responses_gain_no_profile_keys(soul_props: dict):
    """A monitor blob on the same channel is a machine_response and nothing more."""
    d = cb.decode_command(soul_props["d302_monitor"]["value"])
    assert d["type"] == "machine_response"
    assert "profile" not in d and "status" not in d and "crc_valid" not in d


def test_profile_value_per_model():
    """Soul synthesizes the frame; the Eletta needs the device signature first.

    Same contract as ``standby_value``: returning ``None`` without a signature
    lets the coordinator fall back explicitly and log it, instead of sending a
    frame the Eletta would most likely drop in silence.
    """
    soul = mp.SoulProfile()
    out = soul.profile_value(2, None, 0x69E8C5EE)
    raw = base64.b64decode(out)
    assert raw.hex(" ") == "0d 06 a9 f0 02 e7 a3 69 e8 c5 ee"
    assert cb.decode_command(out)["type"] == "profile"
    # The signature is ignored on the synthesized Soul path, as for standby.
    assert soul.profile_value(2, bytes.fromhex("00d32f8c"), 0x69E8C5EE) == out
    generic = mp.GenericSoulProfile()
    assert generic.profile_value(2, None, 0x69E8C5EE) == out

    eletta = mp.ElettaProfile()
    assert eletta.profile_value(2, None) is None
    out = eletta.profile_value(2, bytes.fromhex("00d32f8c"), 0x69E8C5EE)
    raw = base64.b64decode(out)
    assert raw[-4:].hex(" ") == "00 d3 2f 8c"
    assert raw[:11].hex(" ") == "0d 06 a9 f0 02 e7 a3 69 e8 c5 ee"
    d = cb.decode_command(out)
    assert d["type"] == "profile" and d["profile"] == 2 and d["crc_valid"] is True


def test_recipe_dump_never_renders_the_profile_channel(soul_props: dict):
    """``a9f0`` stays out of the diagnostic dump, request and reply alike.

    The dump is meant for public issue reports; the response channel carries
    live command traffic, not recipes, and it is excluded on purpose.
    """
    props = {
        "data_request": soul_props["data_request"],
        "data_response": soul_props["data_response"],
        "d001_rec_espresso": soul_props["d001_rec_espresso"],
    }
    lines = cb.recipe_dump_lines(props)
    assert len(lines) == 1
    assert lines[0].startswith("d001_rec_espresso [family b0f0] = ")
    assert not any("a9 f0" in line or "data_re" in line for line in lines)
    assert const.CMD_FAMILY_PROFILE not in const.DUMPABLE_BLOB_FAMILIES
