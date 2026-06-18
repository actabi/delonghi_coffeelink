"""Unit tests for the pure MonitorV2 parsing (monitor.py), focused on the
``switches``/``alarms`` bitfields added for ECAM maintenance binary sensors.

Loads only the dependency-free modules (no Home Assistant import), matching the
approach in test_command_builder.py: a stub parent package is registered so the
relative imports in monitor.py (`command_builder`, `const`) resolve.

The parser must:
  - keep returning the legacy fields (status/progress/action/accessory) unchanged,
  - add switches (16-bit) + alarms (32-bit) ONLY when the contents block is long
    enough (>= 13 bytes), so short Soul payloads are unaffected,
  - never raise on malformed input (returns {"error": ...}).
"""
from __future__ import annotations

import base64
import importlib.util
import sys
import types
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "delonghi_coffeelink"


def _load(modname: str, filename: str):
    full = f"delonghi_coffeelink.{modname}"
    spec = importlib.util.spec_from_file_location(full, PKG_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


# Stub the parent package so relative imports resolve without running __init__.py.
if "delonghi_coffeelink" not in sys.modules:
    _pkg = types.ModuleType("delonghi_coffeelink")
    _pkg.__path__ = [str(PKG_DIR)]
    sys.modules["delonghi_coffeelink"] = _pkg

_load("const", "const.py")
cb = _load("command_builder", "command_builder.py")
monitor = _load("monitor", "monitor.py")

parse_monitor_b64 = monitor.parse_monitor_b64
crc16_aug_ccitt = cb.crc16_aug_ccitt
MONITOR_REQUEST_ID = monitor.MONITOR_REQUEST_ID


def _build_monitor_blob(contents: bytes) -> str:
    """Build a valid MonitorV2 EcamPacket base64 around a given contents block.

    Envelope (machine->app): <prefix=0x0d> <length> <data ...> <crc16 2B> <ts 4B>
    where data = [MONITOR_REQUEST_ID, <subid>, *contents] and the CRC covers
    raw[0 : length-1]. This mirrors how the real machine frames are decoded in
    ``monitor._parse_ecam_packet`` so the test exercises the real path.
    """
    data = bytes([MONITOR_REQUEST_ID, 0x00]) + contents
    # length byte = index of the CRC high byte = len(prefix+length+data+? ) ...
    # _parse_ecam_packet: data = raw[2:length-1]; crc at raw[length-1:length+1];
    # so length-1 == 2 + len(data)  ->  length = len(data) + 3.
    length = len(data) + 3
    body = bytes([0x0D, length]) + data
    crc = crc16_aug_ccitt(body)
    raw = body + crc.to_bytes(2, "big") + b"\x00\x00\x00\x00"  # + 4-byte timestamp
    return base64.b64encode(raw).decode()


def test_short_contents_has_no_switches_or_alarms():
    """A minimal 8-byte contents block (Soul-style) parses status but no bitfields."""
    contents = bytes(
        [
            0x01,  # 0 accessory
            0x00,  # 1
            0x00,  # 2
            0x00,  # 3
            0x00,  # 4
            0x05,  # 5 status
            0x00,  # 6 action
            0x00,  # 7 progress
        ]
    )
    result = parse_monitor_b64(_build_monitor_blob(contents))
    assert "error" not in result, result
    assert result["status"] == 0x05
    assert result["accessory"] == 0x01
    assert "switches" not in result
    assert "alarms" not in result


def test_long_contents_parses_switches_and_alarms():
    """A >=13-byte contents block (ECAM-style) yields switches + alarms bitfields."""
    contents = bytes(
        [
            0x02,  # 0 accessory
            0x18,  # 1 switches low  (bits 3,4 set -> 0x18)
            0x00,  # 2 switches high
            0x0F,  # 3 alarms byte0  (bits 0..3 set)
            0x00,  # 4 alarms byte1
            0x05,  # 5 status
            0x01,  # 6 action
            0x42,  # 7 progress
            0x00,  # 8 alarms byte2
            0x00,  # 9 alarms byte3
            0x00,  # 10
            0x00,  # 11
            0x00,  # 12
        ]
    )
    result = parse_monitor_b64(_build_monitor_blob(contents))
    assert "error" not in result, result
    assert result["status"] == 0x05
    assert result["progress"] == 0x42
    # switches = byte1 | byte2<<8
    assert result["switches"] == 0x0018
    # alarms = byte3 | byte4<<8 | byte8<<16 | byte9<<24
    assert result["alarms"] == 0x0000000F
    # Bit decoding used by the binary sensors:
    assert (result["alarms"] >> 0) & 1  # water empty
    assert (result["alarms"] >> 1) & 1  # waste full
    assert (result["alarms"] >> 2) & 1  # descale
    assert (result["alarms"] >> 3) & 1  # filter
    assert (result["switches"] >> 3) & 1  # waste container removed bit
    assert (result["switches"] >> 4) & 1  # water tank removed bit


def test_alarms_high_bytes_are_placed_correctly():
    """byte8/byte9 land in alarm bits 16-31 (e.g. low-water warning bit 16)."""
    contents = bytearray(13)
    contents[5] = 0x05  # status
    contents[8] = 0x01  # alarms byte2 -> bit 16
    contents[9] = 0x80  # alarms byte3 -> bit 31
    result = parse_monitor_b64(_build_monitor_blob(bytes(contents)))
    assert "error" not in result, result
    assert (result["alarms"] >> 16) & 1
    assert (result["alarms"] >> 31) & 1
    assert result["alarms"] == 0x80010000


def test_malformed_blob_returns_error_not_raise():
    """Garbage input never raises - it surfaces as an error dict."""
    assert "error" in parse_monitor_b64("not base64!!!")
    assert "error" in parse_monitor_b64("")
    # Valid base64 but not a MonitorV2 packet.
    assert "error" in parse_monitor_b64(base64.b64encode(b"\x0d\x05\x99\x00").decode())
