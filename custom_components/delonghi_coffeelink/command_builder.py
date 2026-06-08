"""Binary command builder for DeLonghi Coffee Link (Ayla transport)."""
from __future__ import annotations

import base64
import binascii
import time

from .const import (
    BEVERAGES,
    CMD_FAMILY_BREW,
    CMD_FAMILY_POWER,
    CMD_LENGTH,
    CMD_PREFIX,
    CMD_RESPONSE_PREFIX,
    CRC_INIT,
    CRC_POLY,
    DEFAULT_RECIPE_PARAMS,
    ELETTA_RECIPE_TRAILER,
    POWER_SESSION_REFRESH_PARAMS,
    POWER_STANDBY_PARAMS,
    POWER_WAKE_PARAMS,
)

_BEV_NAMES = {bev_id: display for bev_id, _key, display, _icon in BEVERAGES}
_ACTION_NAMES = {0x01: "start", 0x02: "stop"}


def _session_id_to_tail_bytes(app_id: int) -> bytes:
    """Encode cloud session app id as signed int32 BE (DlghIoT / ayla_client convention)."""
    signed = ((app_id & 0xFFFFFFFF) ^ 0x80000000) - 0x80000000
    return signed.to_bytes(4, "big", signed=True)


def crc16_aug_ccitt(data: bytes) -> int:
    """CRC16 AUG-CCITT: poly 0x1021, init 0x1D0F, BE, no reflection."""
    crc = CRC_INIT
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ CRC_POLY
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


def build_beverage_command(
    beverage_id: int,
    action: int,
    params: bytes = DEFAULT_RECIPE_PARAMS,
    timestamp: int | None = None,
) -> bytes:
    """
    Build a raw binary beverage start/stop command.

    beverage_id: 1 byte (see BEVERAGES in const.py)
    action: 0x01 (start) or 0x02 (stop)
    params: 6 bytes of recipe parameters
    timestamp: Unix seconds; None = now
    """
    if timestamp is None:
        timestamp = int(time.time())
    header = bytes(
        [CMD_PREFIX, CMD_LENGTH, CMD_FAMILY_BREW[0], CMD_FAMILY_BREW[1], beverage_id, action]
    ) + params
    if len(header) != 12:
        raise ValueError(f"Header must be 12 bytes, got {len(header)}")
    crc = crc16_aug_ccitt(header)
    return header + crc.to_bytes(2, "big") + timestamp.to_bytes(4, "big")


def build_eletta_beverage_command(
    beverage_id: int,
    action: int,
    recipe_block: bytes,
    timestamp: int | None = None,
) -> bytes:
    """Build a beverage command for Eletta Explore (oem_model=DL-striker-cb).

    Unlike the Soul frame (fixed 6-byte recipe, no trailer), the Eletta frame
    carries a *variable-length* recipe block - the exact bytes the official app
    sends for that beverage (captured live, quantity/intensity/milk included) -
    followed by the ``0x01 0x0a`` trailer, the CRC, then the timestamp::

        0d LEN 83 f0 <bev> <action> <recipe_block...> 01 0a <crc16> <ts>

    ``LEN`` is the total frame length (through the CRC) minus the start byte.
    Verified byte-for-byte against captured app frames (issue #1: Hot Water,
    Espresso, Cappuccino, Flat White on ECAM45x.xx.x).
    """
    if timestamp is None:
        timestamp = int(time.time())
    body = (
        bytes([CMD_FAMILY_BREW[0], CMD_FAMILY_BREW[1], beverage_id, action])
        + bytes(recipe_block)
        + ELETTA_RECIPE_TRAILER
    )
    frame = bytes([CMD_PREFIX, len(body) + 3]) + body
    frame += crc16_aug_ccitt(frame).to_bytes(2, "big")
    return frame + timestamp.to_bytes(4, "big")


def serialize_learned_frames(
    start: dict[int, str], stop: dict[int, str], wake: str | None = None
) -> dict:
    """Serialize the learned Eletta frames for persistence (HA Store / JSON).

    Beverage ids become hex strings (JSON keys must be strings); values are the
    captured base64 frames, replayed as-is later with a fresh timestamp. The
    optional power-on (``wake``) frame is a single captured frame.
    """
    data: dict = {
        "start": {f"0x{bev:02x}": frame for bev, frame in start.items()},
        "stop": {f"0x{bev:02x}": frame for bev, frame in stop.items()},
    }
    if wake:
        data["wake"] = wake
    return data


def deserialize_learned_frames(
    data: dict | None,
) -> tuple[dict[int, str], dict[int, str], str | None]:
    """Inverse of :func:`serialize_learned_frames`; tolerant of missing/odd data."""
    def _section(name: str) -> dict[int, str]:
        out: dict[int, str] = {}
        section = (data or {}).get(name) or {}
        if not isinstance(section, dict):
            return out
        for key, frame in section.items():
            if not isinstance(frame, str):
                continue
            try:
                out[int(key, 16)] = frame
            except (ValueError, TypeError):
                continue
        return out

    wake = (data or {}).get("wake")
    if not isinstance(wake, str):
        wake = None
    return _section("start"), _section("stop"), wake


def recipe_dump_lines(props: dict) -> list[str]:
    """Render the machine's stored recipe datapoints for a read-only diagnostic.

    Returns ``name = <hex>`` lines for every property whose name contains
    ``_rec_`` (the per-beverage recipe definitions the machine stores, e.g.
    ``d059_rec_1_espresso``) plus the active-profile indicator. Base64 blobs are
    decoded to hex; other values are shown as-is. Sends nothing to the machine -
    used to confirm whether a stored recipe maps to the beverage command's
    variable recipe block (the path to drop the "teach from the app" step).
    """
    lines: list[str] = []
    for name in sorted(props):
        if "_rec_" not in name and name != "d286_mach_sett_profile":
            continue
        prop = props.get(name)
        value = prop.get("value") if isinstance(prop, dict) else prop
        if isinstance(value, str) and value.strip():
            try:
                rendered = base64.b64decode("".join(value.split()), validate=True).hex(" ")
            except (ValueError, binascii.Error):
                rendered = value
        else:
            rendered = repr(value)
        lines.append(f"{name} = {rendered}")
    return lines


def replay_with_timestamp(value_b64: str, timestamp: int | None = None) -> str:
    """Re-emit a captured frame with a fresh timestamp and nothing else changed.

    The 4-byte Unix timestamp sits *after* the CRC (which only covers the bytes
    before it), so swapping it leaves the checksum valid. This is how an Eletta
    beverage frame captured from the official app is replayed: byte-for-byte
    identical - same action byte, same variable recipe block, and any trailing
    device signature the app appended - except the timestamp, which must change
    so the cloud/machine treats it as a new command rather than a duplicate.
    """
    raw = bytearray(base64.b64decode("".join(value_b64.split())))
    if timestamp is None:
        timestamp = int(time.time())
    if len(raw) >= 2:
        frame_len = raw[1] + 1
        if len(raw) >= frame_len + 4:
            raw[frame_len : frame_len + 4] = timestamp.to_bytes(4, "big")
    return base64.b64encode(bytes(raw)).decode("ascii")


def encode_command(command_bytes: bytes) -> str:
    """Base64-encode command for transmission via Ayla data_request property."""
    return base64.b64encode(command_bytes).decode("ascii")


def build_and_encode(beverage_id: int, action: int, params: bytes = DEFAULT_RECIPE_PARAMS) -> str:
    """Shortcut: build command + base64 encode for Ayla."""
    return encode_command(build_beverage_command(beverage_id, action, params))


def build_power_command(
    params: bytes, timestamp: int | None = None, signature: bytes | None = None
) -> bytes:
    """
    Build a power-family command (0x84 0x0f): wake, standby, ...

    Frame: 0d 07 84 0f <params 2B> <crc16> <timestamp> [<device signature 4B>]
    Length byte = 0x07, payload before CRC = 6 bytes. The optional 4-byte
    device signature goes AFTER the timestamp (so the CRC is unaffected);
    some models (Eletta Explore) ignore power commands without it.
    """
    if timestamp is None:
        timestamp = int(time.time())
    header = bytes([CMD_PREFIX, 0x07, CMD_FAMILY_POWER[0], CMD_FAMILY_POWER[1]]) + params
    if len(header) != 6:
        raise ValueError(f"Power header must be 6 bytes, got {len(header)}")
    crc = crc16_aug_ccitt(header)
    frame = header + crc.to_bytes(2, "big") + timestamp.to_bytes(4, "big")
    if signature:
        frame += signature
    return frame


def build_wake_command(timestamp: int | None = None) -> bytes:
    """Build the WAKE / power-on command (captured: 0d 07 84 0f 02 01 55 12 <ts>)."""
    return build_power_command(POWER_WAKE_PARAMS, timestamp)


def build_wake_encoded() -> str:
    """Shortcut: build wake command + base64 encode."""
    return encode_command(build_wake_command())


def build_wake_with_session_tail(app_id: int, timestamp: int | None = None) -> bytes:
    """Wake/power-on with the cloud session id in the 4-byte tail (DlghIoT resume)."""
    return build_power_command(
        POWER_WAKE_PARAMS, timestamp, _session_id_to_tail_bytes(app_id)
    )


def build_wake_with_session_tail_encoded(app_id: int) -> str:
    return encode_command(build_wake_with_session_tail(app_id))


def build_standby_with_session_tail(app_id: int, timestamp: int | None = None) -> bytes:
    """Standby with the cloud session id in the 4-byte tail (DlghIoT standby)."""
    return build_power_command(
        POWER_STANDBY_PARAMS, timestamp, _session_id_to_tail_bytes(app_id)
    )


def build_standby_with_session_tail_encoded(app_id: int) -> str:
    return encode_command(build_standby_with_session_tail(app_id))


def build_session_refresh_command(app_id: int, timestamp: int | None = None) -> bytes:
    """Deep-standby session nudge (DlghIoT refresh(), params 03 02)."""
    return build_power_command(
        POWER_SESSION_REFRESH_PARAMS, timestamp, _session_id_to_tail_bytes(app_id)
    )


def build_session_refresh_encoded(app_id: int) -> str:
    return encode_command(build_session_refresh_command(app_id))


def validate_power_frame_b64(value_b64: str, expected_params: bytes) -> bool:
    """True if a base64 frame is a valid power command with the expected params."""
    decoded = decode_command(value_b64)
    return (
        decoded.get("type") == "power"
        and decoded.get("params") == expected_params.hex(" ")
        and decoded.get("crc_valid") is True
    )


def validate_replayed_wake_frame(value_b64: str) -> bool:
    """Validate a learned/replayed wake frame before send (params 02 01, CRC ok)."""
    return validate_power_frame_b64(value_b64, POWER_WAKE_PARAMS)


def build_standby_command(
    timestamp: int | None = None, signature: bytes | None = None
) -> bytes:
    """Build the STANDBY / power-off command (0d 07 84 0f 01 01 00 41 <ts>).

    Reported on Eletta Explore (issue #1) and validated live on the reference
    PrimaDonna Soul: the machine powers off as if the physical button was
    pressed. The official app exposes no power-off control, so this frame
    cannot be learned - it is always synthesized.
    """
    return build_power_command(POWER_STANDBY_PARAMS, timestamp, signature)


def build_standby_encoded(signature: bytes | None = None) -> str:
    """Shortcut: build standby command + base64 encode."""
    return encode_command(build_standby_command(signature=signature))


def device_signature_from_frame(frame_b64: str | None) -> bytes | None:
    """Extract the 4-byte device signature from a learned app frame.

    App frames carry a per-machine constant after the timestamp (e.g.
    ``00 d3 2f 8c``). Frame layout: <structural = length_byte + 1> <ts 4B>
    [<signature 4B>]. Returns ``None`` if the frame has no signature.
    """
    if not frame_b64:
        return None
    try:
        raw = base64.b64decode(frame_b64.strip(), validate=True)
    except (binascii.Error, ValueError, AttributeError):
        return None
    if len(raw) < 2:
        return None
    structural_len = raw[1] + 1
    sig = raw[structural_len + 4 : structural_len + 8]
    return sig if len(sig) == 4 else None


def is_wake_power_frame(decoded: dict) -> bool:
    """True if a decoded frame is a real wake/power-on command (params 02 01).

    The app also emits 0x84 0x0f frames that are NOT a power-on (e.g.
    session-refresh packets with params 03 02, seen in issue #1 captures);
    those must never be learned as the wake frame or they would overwrite
    the learned power-on frame.
    """
    return (
        decoded.get("type") == "power"
        and decoded.get("params") == POWER_WAKE_PARAMS.hex(" ")
    )


# ---------------------------------------------------------------------------
# Decoding / inspection (used by the diagnostic command sniffer)
#
# These functions never raise on bad input - they return a dict describing what
# could be parsed, so a value captured live from the cloud (possibly written by
# the official Coffee Link app, possibly malformed) can always be logged.
# ---------------------------------------------------------------------------


def decode_command(value_b64: str) -> dict:
    """Decode a base64 command/response payload into a human-readable dict.

    Recognises the two app->machine frame families this integration emits
    (brew ``0x83 0xf0`` and power/wake ``0x84 0x0f``) and machine->app
    responses (prefix ``0xd0``). Unknown shapes still get a hex dump.
    """
    if not isinstance(value_b64, str) or not value_b64.strip():
        return {"raw_b64": value_b64, "error": "value is not a non-empty string"}
    # Ayla returns string datapoints with surrounding whitespace (commonly a
    # trailing newline); normalise it so the frame decodes and round-trips.
    value_b64 = "".join(value_b64.split())
    out: dict = {"raw_b64": value_b64}
    try:
        raw = base64.b64decode(value_b64, validate=True)
    except (ValueError, binascii.Error):
        out["error"] = "not valid base64"
        return out

    out["hex"] = raw.hex(" ")
    out["length"] = len(raw)
    if len(raw) >= 4:
        out["prefix"] = f"0x{raw[0]:02x}"
        out["length_byte"] = f"0x{raw[1]:02x}"
        out["family"] = raw[2:4].hex(" ")
    family = bytes(raw[2:4]) if len(raw) >= 4 else b""

    if family == CMD_FAMILY_BREW and len(raw) >= 8:
        out["type"] = "beverage"
        out["beverage_id"] = f"0x{raw[4]:02x}"
        out["beverage_name"] = _BEV_NAMES.get(raw[4], "unknown")
        out["action"] = raw[5]
        out["action_name"] = _ACTION_NAMES.get(raw[5], "?")
        # The frame is self-describing: the length byte (raw[1]) gives the total
        # frame size (through the CRC) minus the start byte, so the same decoder
        # handles the fixed Soul frame and the variable-length Eletta frame.
        frame_len = raw[1] + 1
        if frame_len < 8 or frame_len > len(raw):
            out["error"] = "length byte inconsistent with payload"
            return out
        crc_bytes = raw[frame_len - 2 : frame_len]
        out["crc"] = crc_bytes.hex(" ")
        out["crc_valid"] = (
            crc16_aug_ccitt(raw[0 : frame_len - 2]) == int.from_bytes(crc_bytes, "big")
        )
        # Eletta (DL-striker-cb) terminates the recipe block with 0x01 0x0a before
        # the CRC; the Soul (DL-millcore) frame has no trailer (6 fixed bytes).
        eletta = raw[frame_len - 4 : frame_len - 2] == ELETTA_RECIPE_TRAILER
        out["style"] = "eletta" if eletta else "soul"
        recipe = raw[6 : (frame_len - 4 if eletta else frame_len - 2)]
        out["recipe"] = recipe.hex(" ")
        # Back-compat: the historical "params" key keeps the first 6 recipe bytes.
        out["params"] = recipe[:6].hex(" ")
        if len(raw) >= frame_len + 4:
            out["timestamp"] = int.from_bytes(raw[frame_len : frame_len + 4], "big")
        # The whole frame minus the 4 trailing timestamp bytes (which change every
        # second) - this is the part to compare between app and integration.
        out["structural_b64"] = base64.b64encode(raw[0:frame_len]).decode("ascii")
    elif family == CMD_FAMILY_POWER and len(raw) >= 12:
        out["type"] = "power"
        out["params"] = raw[4:6].hex(" ")
        out["crc"] = raw[6:8].hex(" ")
        out["crc_valid"] = crc16_aug_ccitt(raw[0:6]) == int.from_bytes(raw[6:8], "big")
        out["timestamp"] = int.from_bytes(raw[8:12], "big")
        out["structural_b64"] = base64.b64encode(raw[0:8]).decode("ascii")
    elif len(raw) >= 1 and raw[0] == CMD_RESPONSE_PREFIX:
        out["type"] = "machine_response"
    else:
        out["type"] = "unknown"
    return out


def builder_structural_b64(decoded: dict) -> str | None:
    """Return the non-timestamp prefix THIS integration would emit for the same
    command, so a captured frame can be compared structurally (payload + CRC)
    while ignoring the per-second timestamp. ``None`` if not comparable.
    """
    kind = decoded.get("type")
    if kind == "beverage":
        if decoded.get("style") == "eletta":
            # Eletta commands are replayed verbatim from the captured app recipe
            # bytes, so a structural comparison against a synthesized frame would
            # be trivially true (or meaningless) - not informative.
            return None
        try:
            bev_id = int(decoded["beverage_id"], 16)
        except (KeyError, ValueError):
            return None
        cmd = build_beverage_command(bev_id, decoded.get("action", 0x01))
        return base64.b64encode(cmd[0:14]).decode("ascii")
    if kind == "power":
        return base64.b64encode(build_wake_command()[0:8]).decode("ascii")
    return None


def summarize_decoded(decoded: dict) -> str:
    """One-line human summary for logs."""
    if "error" in decoded:
        return f"undecodable ({decoded['error']}): {decoded.get('raw_b64')}"
    kind = decoded.get("type")
    match = decoded.get("matches_integration")
    match_str = "" if match is None else f" matches_integration={match}"
    if kind == "beverage":
        return (
            f"beverage {decoded.get('beverage_name')} id={decoded.get('beverage_id')} "
            f"action={decoded.get('action_name')} style={decoded.get('style')} "
            f"recipe=[{decoded.get('recipe')}] "
            f"crc_valid={decoded.get('crc_valid')}{match_str}"
        )
    if kind == "power":
        return (
            f"power/wake params=[{decoded.get('params')}] "
            f"crc_valid={decoded.get('crc_valid')}{match_str}"
        )
    return f"{kind} hex=[{decoded.get('hex')}]"
