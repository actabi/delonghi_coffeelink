"""Parse ``d302_monitor_machine`` packets (MonitorV2) into machine status.

The machine continuously publishes a monitor blob on ``d302_monitor_machine``
describing its operational state (standby, ready, rinsing, ...). Decoding it
gives a proper "Machine Status" sensor instead of inferring state from
side effects.

Packet layout (machine->app EcamPacket, same envelope as command responses):

    <prefix> <length> <data ...> <crc16 2B> <timestamp 4B> [<extra ...>]

where ``data = raw[2 : length-1]`` and the CRC (crc16 AUG-CCITT, the one
already used for commands) covers ``raw[0 : length-1]``. For a MonitorV2
packet ``data[0] == 0x75`` (117) and ``data[2:]`` carries the monitor
contents; the interesting fields are at fixed offsets in those contents.

Contributed by @TischenkoArseny (PR #5), derived from the DlghIoT client by
Matthieu Guerquin-Kern (https://framagit.org/mattgk/dlghiot).

All helpers are pure and never raise - a malformed blob returns
``{"error": ...}`` so the poll loop is never at risk.
"""
from __future__ import annotations

import base64
import binascii
import logging
from typing import Any

from .command_builder import crc16_aug_ccitt
from .const import MACHINE_STATUS

_LOGGER = logging.getLogger(__name__)

# data[0] identifying a MonitorV2 packet.
MONITOR_REQUEST_ID = 0x75


def _parse_ecam_packet(raw: bytes) -> tuple[bytes, bytes, bytes]:
    """Split an EcamPacket blob into (data, timestamp, extra). Raises ValueError."""
    if len(raw) < 4:
        raise ValueError("packet too short")
    length = raw[1]
    if length < 4 or len(raw) < length + 1:
        raise ValueError("invalid length byte")
    data = raw[2 : length - 1]
    expected_crc = int.from_bytes(raw[length - 1 : length + 1], "big")
    if crc16_aug_ccitt(raw[0 : length - 1]) != expected_crc:
        raise ValueError("CRC mismatch")
    timestamp = raw[length + 1 : length + 5] if len(raw) >= length + 5 else b""
    extra = raw[length + 5 :] if len(raw) > length + 5 else b""
    return data, timestamp, extra


def _parse_monitor_contents(contents: bytes) -> dict[str, int]:
    """Extract the monitor fields from the MonitorV2 contents block."""
    if len(contents) < 8:
        raise ValueError("monitor contents too short")
    fields: dict[str, int] = {
        "accessory": contents[0],
        "status": contents[5],
        "action": contents[6],
        "progress": contents[7],
    }
    if len(contents) >= 13:
        fields["switches"] = contents[1] | (contents[2] << 8)
        fields["alarms"] = (
            contents[3]
            | (contents[4] << 8)
            | (contents[8] << 16)
            | (contents[9] << 24)
        )
    return fields


def parse_monitor_b64(value_b64: str) -> dict[str, Any]:
    """Decode a ``d302_monitor_machine`` value into status fields.

    Never raises: returns ``{"error": ...}`` on any failure so the caller can
    surface the problem as a sensor attribute without breaking the update.
    """
    if not isinstance(value_b64, str) or not value_b64.strip():
        return {"error": "empty value"}
    try:
        raw = base64.b64decode("".join(value_b64.split()), validate=True)
        data, _timestamp, _extra = _parse_ecam_packet(raw)
        if len(data) < 2 or data[0] != MONITOR_REQUEST_ID:
            return {"error": f"not MonitorV2 (id={data[0] if data else '?'})"}
        fields = _parse_monitor_contents(data[2:])
        status = fields["status"]
        result: dict[str, Any] = {
            "status": status,
            "status_name": MACHINE_STATUS.get(status, "unknown"),
            "progress": fields["progress"],
            "action": fields["action"],
            "accessory": fields["accessory"],
        }
        if "switches" in fields:
            result["switches"] = fields["switches"]
        if "alarms" in fields:
            result["alarms"] = fields["alarms"]
        return result
    except (ValueError, binascii.Error) as err:
        _LOGGER.debug("Failed to parse monitor blob: %s", err)
        return {"error": str(err)}
