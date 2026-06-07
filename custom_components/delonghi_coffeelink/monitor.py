"""Parse d302_monitor_machine packets (MonitorV2) from DeLonghi machines."""
from __future__ import annotations

import base64
import binascii
import logging
from typing import Any

from .const import MACHINE_STATUS

_LOGGER = logging.getLogger(__name__)

MONITOR_REQUEST_ID = 117


def _crc16_aug_ccitt(data: bytes) -> bytes:
    crc = 0x1D0F
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return (crc & 0xFFFF).to_bytes(2, byteorder="big")


def _parse_ecam_packet(raw: bytes) -> tuple[bytes, bytes, bytes]:
    """Return (data, timestamp, extra) from an EcamPacket blob."""
    if len(raw) < 4:
        raise ValueError("packet too short")
    length = raw[1]
    if length < 4:
        raise ValueError("invalid length byte")
    data = raw[2 : length - 1]
    expected_crc = raw[length - 1 : length + 1]
    if _crc16_aug_ccitt(raw[0 : length - 1]) != expected_crc:
        raise ValueError("CRC mismatch")
    timestamp = raw[length + 1 : length + 5] if len(raw) >= length + 5 else b""
    extra = raw[length + 5 :] if len(raw) > length + 5 else b""
    return data, timestamp, extra


def _parse_monitor_contents(contents: bytes) -> dict[str, int]:
    """Extract monitor fields from request contents (13 bytes)."""
    if len(contents) < 8:
        raise ValueError("monitor contents too short")
    return {
        "accessory": contents[0],
        "status": contents[5],
        "action": contents[6],
        "progress": contents[7],
    }


def parse_monitor_b64(value_b64: str) -> dict[str, Any]:
    """Decode a monitor property value into status fields.

    Never raises: returns ``{"error": ...}`` on failure.
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
        return {
            "status": status,
            "status_name": MACHINE_STATUS.get(status, "unknown"),
            "progress": fields["progress"],
            "action": fields["action"],
            "accessory": fields["accessory"],
        }
    except (ValueError, binascii.Error) as err:
        _LOGGER.debug("Failed to parse monitor: %s", err)
        return {"error": str(err)}
