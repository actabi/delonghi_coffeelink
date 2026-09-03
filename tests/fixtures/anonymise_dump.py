"""Rebuild ``soul_properties.json`` from a raw machine property dump.

Kept next to the fixture so the anonymisation is reproducible and auditable
rather than a one-off nobody can check. The dump is from a real machine, so
before it can live in a public repository every identifier has to go:

* ``d270_serialnumber`` - the ASCII serial is overwritten in place;
* every blob carrying user-entered UTF-16BE text (families ``a4f0`` profile
  names, ``aaf0`` custom-slot names, ``baf0`` bean names) gets a placeholder;
* the Wi-Fi MAC address, which is three plain **integer** datapoints
  (``d521``/``d522``/``d523``) rather than a blob - a scan that only looks at
  the base64 strings walks straight past it.

Selection is by blob **family**, not by property name - the same principle the
parser uses, and the reason this catches ``d036_recipe_custom_name_1_3``, which
a name-based rule missed. Each rewritten blob has its CRC recomputed, so the
fixture still exercises the checksum path exactly as the machine's own bytes
would; a blob the dump truncated is re-emitted at its original length so the
truncation cases survive too.

``test_catalog.py::test_the_fixture_carries_no_identifiers`` enforces the result.

Usage: ``python anonymise_dump.py <raw-dump.txt> [out.json]``.
"""
import base64
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "raw_dump.txt")
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "soul_properties.json")

CRC_POLY, CRC_INIT = 0x1021, 0x1D0F
def crc16(data):
    crc = CRC_INIT
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ CRC_POLY) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc

LINE = re.compile(r"^\s*\[(\w+)\s+RO=(\w+)\]\s+(\S+)\s+=\s(.*?)\s+\((string|integer)\)\s*$")
props = {}
for line in open(SRC, encoding="utf-8", errors="replace"):
    m = LINE.match(line.rstrip("\n"))
    if not m:
        continue
    _dir, _ro, name, value, kind = m.groups()
    props[name] = {"value": int(value) if kind == "integer" else value}
# the two trailing channel props are printed without a (string) suffix on data_request
for line in open(SRC, encoding="utf-8", errors="replace"):
    m = re.match(r"^\s*\[(\w+)\s+RO=(\w+)\]\s+(\S+)\s+=\s(\S+)\s*$", line.rstrip("\n"))
    if m and m.group(3) not in props:
        props[m.group(3)] = {"value": m.group(4)}

# Anonymise by blob FAMILY, not by property name - the same principle the parser uses.
TEXT_FAMILIES = {b"\xa4\xf0": "Profile", b"\xba\xf0": "Bean", b"\xaa\xf0": "Slot"}

def _decode_lenient(value):
    """Decode a possibly truncated base64 value: keep the largest 4-char multiple."""
    clean = "".join(value.split())
    return bytearray(base64.b64decode(clean[: len(clean) // 4 * 4]))


def _reencode(raw, original):
    """Re-emit, preserving the original truncation (same base64 char count)."""
    out = base64.b64encode(bytes(raw)).decode()
    clean = "".join(original.split())
    return out if len(clean) % 4 == 0 else out[: len(clean)]


def anonymise_text(name, value):
    raw = _decode_lenient(value)
    if len(raw) < 8 or raw[0] != 0xD0:
        return value
    fam = bytes(raw[2:4])
    label = TEXT_FAMILIES.get(fam)
    if label is None:
        return value
    complete = raw[1] + 1 <= len(raw)
    end = (raw[1] + 1 - 2) if complete else len(raw)
    idx = raw[4]
    filler = f"{label} {idx + 1}".encode("utf-16-be")
    region = bytearray(end - 6)
    region[: len(filler)] = filler[: len(region)]
    raw[6:end] = region
    if complete:
        raw[raw[1] - 1 : raw[1] + 1] = crc16(bytes(raw[: raw[1] - 1])).to_bytes(2, "big")
    return _reencode(raw, value)

def anonymise_serial(value):
    raw = _decode_lenient(value)
    fake = b"000000XX00000000000"
    i = raw.find(b"217065")
    if i >= 0:
        raw[i : i + len(fake)] = fake
        if raw[1] + 1 <= len(raw):
            raw[raw[1] - 1 : raw[1] + 1] = crc16(bytes(raw[: raw[1] - 1])).to_bytes(2, "big")
    return _reencode(raw, value)

# Integer identifiers: the radio MAC is three 16-bit words. Nothing in
# catalog.py reads them, so they can be blanked outright.
MAC_PROPERTIES = ("d521_radio_mac_addr0", "d522_radio_mac_addr1", "d523_radio_mac_addr2")
for name in MAC_PROPERTIES:
    if name in props:
        props[name]["value"] = 0

for name, prop in props.items():
    v = prop["value"]
    if not isinstance(v, str):
        continue
    if name == "d270_serialnumber":
        prop["value"] = anonymise_serial(v)
    else:
        try:
            prop["value"] = anonymise_text(name, v)
        except Exception as exc:  # noqa: BLE001
            print("skip", name, exc)

# scrub any residual identifying ASCII / UTF-16 text
leaks = []
for name, prop in props.items():
    v = prop["value"]
    if isinstance(v, str) and len(v) > 8:
        try:
            raw = bytes(_decode_lenient(v))
        except Exception:
            continue
        txt = raw.decode("utf-16-be", "ignore") + raw.decode("latin-1", "ignore")
        for needle in ("Veronica", "Guill", "217065", "AC000W", "Kimbo", "eden", "Neo Bio", "Perso"):
            if needle.lower() in txt.lower():
                leaks.append((name, needle))
for name in MAC_PROPERTIES:
    assert props.get(name, {}).get("value") == 0, name
print("properties:", len(props), "| residual leaks:", leaks)
os.makedirs(os.path.dirname(OUT), exist_ok=True)
json.dump(props, open(OUT, "w", encoding="utf-8"), indent=1, sort_keys=True)
print("written", OUT)
