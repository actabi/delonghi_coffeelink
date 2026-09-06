"""Read the machine's own beverage catalogue out of its Ayla datapoints.

The machine already tells us everything a beverage catalogue needs; the
integration just never read it. Every recipe-bearing datapoint is a
self-describing binary blob::

    d0 <len> <family 2B> [<profile 1B>] <bevid 1B> <TLV params...> <crc16>

``len`` is the frame size minus the start byte, and the CRC is the same CRC16
AUG-CCITT the command builder already uses, so a blob can be *proved* intact
before anything is derived from it. The reference PrimaDonna Soul (312
properties) publishes 194 such blobs in ten families; these six carry the
catalogue, and are the only ones this module reads:

===========  ====  ==============================================================
family       n     meaning
===========  ====  ==============================================================
``b0 f0``    28    factory descriptor - the capability schema (min/default/max)
``a6 f0``    140   per-profile instance - the current value of each parameter
``a8 f0``    5     a per-profile ordered short list of beverage ids
``a4 f0``    2     profile names (UTF-16BE, user-entered)
``aa f0``    2     custom-recipe slot names
``ba f0``    7     bean-system names
===========  ====  ==============================================================

The other four are the serial number (``a1 0f``), the machine settings including
the PIN (``95 0f``), the monitor blob (``75 0f``) and the command-response
channel (``a9 f0``). They are deliberately out of scope here, and out of the
diagnostic dump too - see ``command_builder.recipe_dump_lines``.

What the ``a8f0`` lists are NOT: the set of drinks the machine can make. On the
reference machine all ten lists (five profiles, two dumps taken days apart)
carry one identical set of 19 ids in four different orders, and between the two
dumps profiles 1, 4 and 5 changed order only - two adjacent transpositions each,
which is what a recency ordering looks like. Meanwhile three drinks that have
factory descriptors, per-profile recipes and lifetime counters (``0x19``
long_black, ``0x1a`` mug_to_go, ``0x1b`` brew_over_ice) appear in no list at
all. So membership is not evidence in either direction: it is an ordered short
list per profile, and a drink can be missing from every one of them and still be
brewable.

That caution is not theoretical. An earlier version of this parser read the ids
from ``payload[2:]``, assuming a header byte the frames do not have, and so ate
each list's first entry. It reported the bean-system drink as absent from every
list when the machine puts it first on all five, and the resulting "the contents
move between dumps" was an artefact of the off-by-one rather than a finding.

Two rules make the whole thing parseable without per-beverage special cases:

* **TLV**: tags ``0x01``, ``0x09`` and ``0x0f`` carry a 16-bit big-endian value,
  every other tag carries 8 bits. That single rule consumes all 140 ``a6f0``
  payloads with zero leftover bytes. In a ``b0f0`` descriptor the same tags
  carry a *triple* (min, default, max) instead of one value.
* **Parse by family, never by property name.** The Soul writes
  ``d039_1_rec_espresso`` and the Eletta ``d059_rec_1_espresso``, with the
  numbers shifted by 20 for the same drink; the blob header is identical on
  both.

Everything here is pure: no Home Assistant, no I/O, no network. Callers hand it
the property dict the coordinator already polls.

Deliberate refusals, because a beverage button that lies is worse than one that
is missing:

* A blob that fails any gate (prefix, declared length, CRC) is *unreadable*,
  never *absent* - it never removes a beverage from the catalogue.
* A TLV walk that leaves a trailing byte returns ``None`` rather than a partial
  parse, so a synthesized command can never be built from a half-understood
  payload.
* ``off_default`` is ``None``, not ``False``, when the factory descriptor is
  truncated or missing. Cross-profile disagreement is reported separately as
  ``profiles_differ`` - it is a weaker signal and must not be read as "the user
  changed this".
* ``in_priority_list`` is never an existence test, for either answer. The
  ``a8f0`` lists are a per-profile ordered *selection* of fixed size, not the
  set of drinks the machine can make - see below - so a drink can be missing
  from every one of them and still be brewed daily.
"""
from __future__ import annotations

import base64
import binascii
from collections.abc import Iterator
from typing import Any

from .command_builder import crc16_aug_ccitt
from .const import (
    BLOB_FAMILY_BEAN_NAMES,
    BLOB_FAMILY_CUSTOM_NAMES,
    BLOB_FAMILY_DESCRIPTOR,
    BLOB_FAMILY_PRIORITY,
    BLOB_FAMILY_PROFILE_NAMES,
    BLOB_FAMILY_PROFILE_RECIPE,
    CMD_RESPONSE_PREFIX,
)

# The family bytes live in const.py because the diagnostic dump in
# command_builder needs them too, and importing this module from there would
# close an import cycle.
BLOB_PREFIX = CMD_RESPONSE_PREFIX

FAMILY_DESCRIPTOR = BLOB_FAMILY_DESCRIPTOR
FAMILY_PROFILE_RECIPE = BLOB_FAMILY_PROFILE_RECIPE
FAMILY_PRIORITY = BLOB_FAMILY_PRIORITY
FAMILY_PROFILE_NAMES = BLOB_FAMILY_PROFILE_NAMES
FAMILY_CUSTOM_NAMES = BLOB_FAMILY_CUSTOM_NAMES
FAMILY_BEAN_NAMES = BLOB_FAMILY_BEAN_NAMES

FAMILY_LABELS = {
    FAMILY_DESCRIPTOR: "factory_descriptor",
    FAMILY_PROFILE_RECIPE: "profile_recipe",
    FAMILY_PRIORITY: "priority_list",
    FAMILY_PROFILE_NAMES: "profile_names",
    FAMILY_CUSTOM_NAMES: "custom_names",
    FAMILY_BEAN_NAMES: "bean_names",
}

#: TLV tags whose value is 16-bit big-endian; every other tag is 8-bit.
WIDE_TAGS = frozenset({0x01, 0x09, 0x0F})

#: Beverage-id ranges that are not one of the built-in drinks.
CUSTOM_SLOT_IDS = frozenset(range(0xE6, 0xEC))
BEAN_SYSTEM_IDS = frozenset(range(0xC8, 0xCE))

# Tag meanings pinned by value range against the reference machine. Only the
# five below are confirmed; the rest are carried through unnamed rather than
# guessed at.
TAG_COFFEE_ML = 0x01
TAG_MILK = 0x09
TAG_WATER_ML = 0x0F
TAG_TEMPERATURE = 0x1B
TAG_CUP_COUNT = 0x1E
TAG_INTENSITY = 0x02

#: Smallest payload each family can carry and still be indexable: the header
#: bytes that precede the parameters (bevid, or profile + bevid, or the two
#: slot-index bytes of a text blob).
_MIN_PAYLOAD = {
    FAMILY_DESCRIPTOR: 1,
    FAMILY_PROFILE_RECIPE: 2,
    FAMILY_PRIORITY: 1,  # <profile>, then ids; a one-entry list is 2 bytes
    FAMILY_PROFILE_NAMES: 2,
    FAMILY_CUSTOM_NAMES: 2,
    FAMILY_BEAN_NAMES: 2,
}

TAG_NAMES = {
    TAG_COFFEE_ML: "coffee_ml",
    TAG_MILK: "milk",
    TAG_WATER_ML: "water_ml",
    TAG_TEMPERATURE: "temperature",
    TAG_CUP_COUNT: "cup_count",
    TAG_INTENSITY: "intensity",
}


def _b64_bytes(value: Any) -> bytes | None:
    """Decode a datapoint value to bytes, tolerating a truncated dump.

    Ayla string datapoints arrive with stray whitespace, and a dump taken with a
    width limit can cut a base64 value mid-quantum - which is not valid base64 at
    all. Keeping the largest 4-character multiple recovers every complete byte
    and loses only the partial one, so a truncated blob is still classifiable
    instead of vanishing.
    """
    if not isinstance(value, str):
        return None
    clean = "".join(value.split())
    if not clean:
        return None
    try:
        return base64.b64decode(clean[: len(clean) // 4 * 4], validate=True)
    except (ValueError, binascii.Error):
        return None


def blob_family(value: Any) -> bytes | None:
    """The family bytes of a machine blob, read without decoding all of it.

    Only the first 8 base64 characters are decoded - 6 bytes, enough for the
    prefix and the family - so a caller can tell what a datapoint is at a
    fraction of the cost of parsing it. Returns ``None`` for anything that is
    not a ``0xd0`` blob.
    """
    if not isinstance(value, str):
        return None
    head = "".join(value.split())[:8]
    if len(head) < 8:
        return None
    try:
        raw = base64.b64decode(head, validate=True)
    except (ValueError, binascii.Error):
        return None
    return bytes(raw[2:4]) if raw[0] == BLOB_PREFIX else None


def catalog_fingerprint(props: dict) -> tuple:
    """A change-detection key over the catalogue datapoints, and only those.

    Cheap enough to run on every poll, and deliberately narrow. Two things go
    wrong if it is widened to "any value that looks like a blob":

    * ``d302_monitor`` and ``data_response`` wear the same envelope and carry a
      fresh timestamp on almost every poll, so the key would change constantly
      and the catalogue would be re-parsed for nothing;
    * whitespace. Ayla wraps string datapoints in it, so a key that compares raw
      values while the parser strips them can disagree about what changed - and
      a change-detector that cannot see a change is how a cache freezes without
      anyone noticing.
    """
    return tuple(
        (name, "".join(value.split()))
        for name in sorted(props)
        for prop in (props.get(name),)
        for value in (prop.get("value") if isinstance(prop, dict) else prop,)
        if blob_family(value) in FAMILY_LABELS
    )


def decode_blob(value: Any) -> dict | None:
    """Classify one datapoint value as a machine blob, or ``None`` if it is not.

    Returns a dict describing what could be established, never raising. Three
    gates, in order: the ``0xd0`` prefix, the declared length, and the CRC.

    The length gate is ``declared <= len(raw)``, not ``==``: the machine appends
    a 4-byte timestamp to some datapoints (``data_response``, ``d302_monitor``),
    and a strict equality would misgrade those live channels as truncated.
    """
    raw = _b64_bytes(value)
    if raw is None or len(raw) < 6 or raw[0] != BLOB_PREFIX:
        return None
    declared = raw[1] + 1
    family = bytes(raw[2:4])
    out: dict[str, Any] = {
        "raw": raw,
        "family": family,
        "family_label": FAMILY_LABELS.get(family, family.hex(" ")),
        "declared_length": declared,
        "length": len(raw),
    }
    if declared > len(raw):
        out.update(truncated=True, crc_valid=False, frame=None, payload=None)
        return out
    frame = raw[:declared]
    crc_valid = crc16_aug_ccitt(frame[:-2]) == int.from_bytes(frame[-2:], "big")
    out.update(
        truncated=False,
        crc_valid=crc_valid,
        frame=frame,
        trailer=raw[declared:],
        payload=frame[4:-2],
    )
    return out


def parse_tlv(payload: bytes) -> dict[int, int] | None:
    """Parse a TLV parameter payload, or ``None`` if it does not consume exactly.

    A trailing byte means the grammar does not fit this payload, and a partial
    parse is exactly the input that would build a wrong brew command, so the
    whole walk is refused rather than truncated.
    """
    out: dict[int, int] = {}
    i = 0
    end = len(payload)
    while i < end:
        tag = payload[i]
        width = 2 if tag in WIDE_TAGS else 1
        if i + 1 + width > end:
            return None
        out[tag] = int.from_bytes(payload[i + 1 : i + 1 + width], "big")
        i += 1 + width
    return out


def parse_triples(payload: bytes) -> dict[int, tuple[int, int, int]] | None:
    """Parse a factory descriptor payload into ``tag -> (min, default, max)``.

    Same tag widths as :func:`parse_tlv`, three values per tag instead of one.
    Returns ``None`` on any leftover byte, for the same reason.
    """
    out: dict[int, tuple[int, int, int]] = {}
    i = 0
    end = len(payload)
    while i < end:
        tag = payload[i]
        width = 2 if tag in WIDE_TAGS else 1
        if i + 1 + 3 * width > end:
            return None
        values = tuple(
            int.from_bytes(payload[i + 1 + n * width : i + 1 + (n + 1) * width], "big")
            for n in range(3)
        )
        out[tag] = values  # type: ignore[assignment]
        i += 1 + 3 * width
    return out


def decode_text(payload: bytes) -> str | None:
    """Decode the first UTF-16BE name in a text blob payload.

    Names live in NUL-padded fixed-width slots, but the slot width varies with
    the blob and a name shorter than the slot leaves the following slots
    misaligned - so only the first name is read. Returns ``None`` when the slot
    is empty or the bytes do not decode.
    """
    try:
        text = payload.decode("utf-16-be", errors="ignore")
    except (UnicodeDecodeError, ValueError):
        return None
    name = text.split("\x00", 1)[0].strip()
    return name or None


def iter_blobs(props: dict) -> Iterator[tuple[str, dict]]:
    """Yield ``(property_name, decoded_blob)`` for every machine blob in ``props``.

    Property names are carried through as labels only - the blob header, never
    the name, decides what a datapoint is.
    """
    for name in sorted(props):
        prop = props.get(name)
        value = prop.get("value") if isinstance(prop, dict) else prop
        blob = decode_blob(value)
        if blob is not None:
            yield name, blob


def _new_entry(bev_id: int) -> dict:
    if bev_id in CUSTOM_SLOT_IDS:
        kind = "custom"
    elif bev_id in BEAN_SYSTEM_IDS:
        kind = "bean_system"
    else:
        kind = "builtin"
    return {
        "id": bev_id,
        "kind": kind,
        "declared": False,
        "descriptor_truncated": False,
        "ranges": {},
        "profiles": {},
        "in_priority_list": None,
        "priority_position": {},
        "defined": None,
        "profiles_differ": False,
        "off_default": None,
        "source_properties": [],
    }


def _slot_is_defined(params: dict[int, int]) -> bool:
    """True when a custom slot has actually been programmed on the machine.

    A slot that dispenses nothing has not been programmed. That is the whole
    test, and it is deliberately the only one: an untouched slot on the
    reference machine also carries ``0x02 = 0xff`` where a programmed drink
    carries an intensity, but every such slot already reads zero for coffee,
    milk and water, so checking the marker as well decided nothing. Dropping it
    also drops a claim about what ``0xff`` means, which no machine has yet had
    to confirm - and were a slot ever to carry a real volume alongside it, "it
    pours something, so it is programmed" is the answer we would want anyway.
    """
    return any(params.get(tag) for tag in (TAG_COFFEE_ML, TAG_MILK, TAG_WATER_ML))


def build_catalog(props: dict) -> dict:
    """Build the machine's beverage catalogue from its polled properties.

    Pure function of ``props``: no I/O, no network, no new protocol traffic. The
    caller decides what to do with it; nothing here creates or removes entities.
    """
    beverages: dict[int, dict] = {}
    priority: dict[int, list[int]] = {}
    names: dict[str, dict[int, str]] = {"profiles": {}, "custom": {}, "beans": {}}
    stats = {
        "blobs": 0,
        "crc_ok": 0,
        "crc_failed": 0,
        "truncated": 0,
        "unparsed_payloads": 0,
        "short_payloads": 0,
        "families": {},
    }

    def entry(bev_id: int) -> dict:
        return beverages.setdefault(bev_id, _new_entry(bev_id))

    for name, blob in iter_blobs(props):
        stats["blobs"] += 1
        label = blob["family_label"]
        stats["families"][label] = stats["families"].get(label, 0) + 1
        if blob["truncated"]:
            stats["truncated"] += 1
            # A truncated descriptor still proves the drink exists; only its
            # ranges are unknown.
            if blob["family"] == FAMILY_DESCRIPTOR and blob["length"] >= 5:
                item = entry(blob["raw"][4])
                item["declared"] = True
                item["descriptor_truncated"] = True
                item["source_properties"].append(name)
            continue
        if not blob["crc_valid"]:
            stats["crc_failed"] += 1
            continue
        stats["crc_ok"] += 1
        family, payload = blob["family"], blob["payload"]
        # A frame can declare a length that leaves no header room at all. It has
        # already passed the CRC by then, so the shape has to be checked before
        # any byte is indexed - a short blob is one more thing that is
        # unreadable, never something that removes a beverage.
        if len(payload) < _MIN_PAYLOAD.get(family, 0):
            stats["short_payloads"] += 1
            continue

        if family == FAMILY_DESCRIPTOR:
            bev_id, body = payload[0], payload[1:]
            item = entry(bev_id)
            item["declared"] = True
            item["source_properties"].append(name)
            triples = parse_triples(body)
            if triples is None:
                stats["unparsed_payloads"] += 1
            else:
                item["ranges"] = triples
        elif family == FAMILY_PROFILE_RECIPE:
            profile, bev_id, body = payload[0], payload[1], payload[2:]
            item = entry(bev_id)
            item["source_properties"].append(name)
            params = parse_tlv(body)
            if params is None:
                stats["unparsed_payloads"] += 1
            else:
                item["profiles"][profile] = params
        elif family == FAMILY_PRIORITY:
            # payload = <profile> <bevid...>. There is NO header byte after the
            # profile: every remaining byte is a beverage id. Reading from [2:]
            # silently ate each list's first entry - see the module docstring.
            profile = payload[0]
            priority[profile] = list(payload[1:])
        elif family in (FAMILY_PROFILE_NAMES, FAMILY_CUSTOM_NAMES, FAMILY_BEAN_NAMES):
            bucket = {
                FAMILY_PROFILE_NAMES: "profiles",
                FAMILY_CUSTOM_NAMES: "custom",
                FAMILY_BEAN_NAMES: "beans",
            }[family]
            # payload = <first slot> <last slot> <UTF-16BE text...>; the text
            # starts at 2, and starting at 1 shifts every character by a byte.
            text = decode_text(payload[2:])
            if text:
                names[bucket][payload[0]] = text

    _apply_priority(beverages, priority)
    for item in beverages.values():
        _finalize(item)

    return {
        "source": "machine" if beverages else "empty",
        "beverages": beverages,
        "priority_lists": priority,
        "names": names,
        "stats": stats,
    }


def _apply_priority(beverages: dict[int, dict], priority: dict[int, list[int]]) -> None:
    """Record which drinks appear in each profile's ordered short list.

    ``in_priority_list`` says exactly that and nothing more. It is not an
    existence test in either direction: ``0x19``, ``0x1a`` and ``0x1b`` have
    factory descriptors, per-profile recipes and lifetime counters on the
    reference machine and appear in none of its five lists, so absence proves
    nothing about what the machine can brew.

    It stays ``None`` where no list was read at all, so "no opinion" and "not
    selected" never collapse into the same answer.
    """
    if not priority:
        return
    listed = {bev_id for ids in priority.values() for bev_id in ids}
    for profile, ids in priority.items():
        for position, bev_id in enumerate(ids):
            item = beverages.setdefault(bev_id, _new_entry(bev_id))
            item["priority_position"][profile] = position
    for item in beverages.values():
        item["in_priority_list"] = item["id"] in listed


def _finalize(item: dict) -> None:
    """Derive the customisation signals once all blobs have been folded in."""
    profiles = item["profiles"]
    if item["kind"] == "custom" and profiles:
        item["defined"] = any(_slot_is_defined(params) for params in profiles.values())

    if len(profiles) > 1:
        tags = {tag for params in profiles.values() for tag in params}
        item["profiles_differ"] = any(
            len({params.get(tag) for params in profiles.values()}) > 1 for tag in tags
        )

    # Only a readable factory descriptor can say whether a value was changed.
    # Without one the answer is unknown, and unknown must not read as "no".
    if item["ranges"] and profiles:
        off_default: dict[int, bool] = {}
        for tag, triple in item["ranges"].items():
            default = triple[1]
            values = [params[tag] for params in profiles.values() if tag in params]
            if values:
                off_default[tag] = any(value != default for value in values)
        item["off_default"] = off_default


def catalog_beverage_ids(catalog: dict | None) -> set[int]:
    """Every beverage id the machine declared, whatever its kind.

    Used to widen the learn gate: a frame the official app sent for a drink the
    machine declares is worth keeping even when the integration has no built-in
    entry for it.
    """
    if not catalog:
        return set()
    beverages = catalog.get("beverages", {})
    return {bev_id for bev_id, item in beverages.items() if item["declared"]}


def catalog_summary(catalog: dict | None) -> str:
    """One-line summary for the diagnostic log."""
    if not catalog or catalog.get("source") == "empty":
        return "no machine catalogue (no readable recipe blobs)"
    beverages = catalog["beverages"]
    stats = catalog["stats"]
    listed = sum(1 for item in beverages.values() if item["in_priority_list"] is True)
    unlisted = sum(1 for item in beverages.values() if item["in_priority_list"] is False)
    custom = sum(1 for item in beverages.values() if item["kind"] == "custom")
    defined = sum(1 for item in beverages.values() if item["defined"] is True)
    with_ranges = sum(1 for item in beverages.values() if item["ranges"])
    # "0 in a profile short list" and "no short list was published" are
    # different statements, and only one is true when no a8f0 blob was readable.
    priority_part = (
        f"{listed} in a profile short list, {unlisted} in none"
        if catalog["priority_lists"]
        else "no profile short list published"
    )
    return (
        f"{len(beverages)} beverage ids ({priority_part}, "
        f"{custom} custom slots of which {defined} programmed), {with_ranges} with "
        f"factory min/default/max ranges, {len(catalog['priority_lists'])} profile lists | blobs="
        f"{stats['blobs']} crc_ok={stats['crc_ok']} crc_failed={stats['crc_failed']} "
        f"truncated={stats['truncated']} unparsed={stats['unparsed_payloads']} "
        f"short={stats['short_payloads']}"
    )
