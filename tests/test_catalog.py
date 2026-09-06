"""Unit tests for the machine-derived beverage catalogue (catalog.py).

Anchored on ``fixtures/soul_properties.json``: a real 312-property dump from a
reference PrimaDonna Soul (DL-millcore), anonymised - the serial and every
user-entered name (profiles, custom recipes, beans) were overwritten with
placeholders and their CRCs recomputed, so the file carries no device or
household identifiers. Every count below was measured against that dump; they
are pinned deliberately, so a future parser edit that quietly changes what the
integration believes the machine can make fails here instead of in someone's
kitchen.

Loads only the dependency-free modules, like test_counters.py, so the suite runs
with plain pytest and no Home Assistant installed.
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
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "soul_properties.json"

if "delonghi_coffeelink" not in sys.modules:
    _pkg = types.ModuleType("delonghi_coffeelink")
    _pkg.__path__ = [str(PKG_DIR)]
    sys.modules["delonghi_coffeelink"] = _pkg


def _load(modname: str, filename: str):
    full = f"delonghi_coffeelink.{modname}"
    spec = importlib.util.spec_from_file_location(full, PKG_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


_load("const", "const.py")
cb = _load("command_builder", "command_builder.py")
catalog = _load("catalog", "catalog.py")


@pytest.fixture(scope="module")
def props() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cat(props: dict) -> dict:
    return catalog.build_catalog(props)


def _blob(family: bytes, payload: bytes, *, trailer: bytes = b"") -> str:
    """Build a well-formed base64 blob, CRC included, for synthetic cases."""
    frame = bytes([catalog.BLOB_PREFIX, 0]) + family + payload
    frame = bytes([catalog.BLOB_PREFIX, len(frame) + 1]) + family + payload
    frame += cb.crc16_aug_ccitt(frame).to_bytes(2, "big")
    return base64.b64encode(frame + trailer).decode("ascii")


# --- the dump, as the machine actually published it ------------------------ #


def test_family_census_matches_the_reference_machine(cat: dict):
    """Every recipe-bearing family, counted. These numbers are the machine's."""
    families = cat["stats"]["families"]
    assert families["factory_descriptor"] == 28
    assert families["profile_recipe"] == 140  # 28 ids x 5 profiles
    assert families["priority_list"] == 5  # one ordered menu per profile
    assert families["profile_names"] == 2
    assert families["custom_names"] == 2
    assert families["bean_names"] == 7


def test_every_readable_blob_passes_crc(cat: dict):
    """163 complete blobs, 163 valid checksums - the data is machine-authored."""
    stats = cat["stats"]
    assert stats["blobs"] == 194
    assert stats["crc_ok"] == 163
    assert stats["crc_failed"] == 0
    assert stats["truncated"] == 31


def test_every_payload_parses_with_no_leftover_bytes(cat: dict):
    """One TLV rule consumes all 140 recipe payloads and all readable descriptors.

    A leftover byte would mean the grammar does not fit, which is exactly the
    input that could build a wrong brew command - so it is counted, and must be
    zero.
    """
    assert cat["stats"]["unparsed_payloads"] == 0


def test_length_gate_accepts_a_trailing_timestamp(props: dict):
    """``declared <= len(raw)``, never ``==``.

    The machine appends a 4-byte timestamp to its live channels. A strict
    equality would misgrade ``data_response`` - a completed, CRC-valid
    request/response round trip - as truncated garbage.
    """
    blob = catalog.decode_blob(props["data_response"]["value"])
    assert blob is not None
    assert blob["truncated"] is False
    assert blob["crc_valid"] is True
    assert len(blob["trailer"]) == 4


# --- refusing to guess ----------------------------------------------------- #


def test_a_truncated_descriptor_is_unreadable_never_absent(cat: dict):
    """A cut blob still proves the drink exists; only its ranges are unknown.

    20 of the 28 factory descriptors are truncated in this dump (a dump-tool
    width limit, not a machine limitation). Espresso is one of them: it must
    stay declared, with no invented range.
    """
    truncated = [i for i, item in cat["beverages"].items() if item["descriptor_truncated"]]
    assert len(truncated) == 20
    espresso = cat["beverages"][0x01]
    assert espresso["declared"] is True
    assert espresso["ranges"] == {}


def test_off_default_is_none_when_the_factory_default_is_unknown(cat: dict):
    """Unknown must never read as "the user changed nothing".

    Espresso reads 48 ml on profiles 1/4/5 and 40 ml on 2/3, but its factory
    descriptor is truncated, so whether 48 is a customisation cannot be known.
    That is reported as ``profiles_differ``, a weaker and different signal, and
    ``off_default`` stays ``None``.
    """
    espresso = cat["beverages"][0x01]
    assert espresso["off_default"] is None
    assert espresso["profiles_differ"] is True
    volumes = {params[catalog.TAG_COFFEE_ML] for params in espresso["profiles"].values()}
    assert volumes == {40, 48}
    unknown = [i for i, item in cat["beverages"].items() if item["off_default"] is None]
    assert len(unknown) == 20


def test_factory_ranges_are_read_not_guessed(cat: dict):
    """The machine publishes the editable ranges a CMS would have to supply."""
    assert cat["beverages"][0x10]["ranges"][catalog.TAG_WATER_ML] == (20, 250, 420)
    assert cat["beverages"][0x0C]["ranges"][catalog.TAG_MILK] == (50, 360, 1080)
    assert cat["beverages"][0x17]["ranges"][catalog.TAG_CUP_COUNT] == (0, 0, 5)
    with_ranges = [i for i, item in cat["beverages"].items() if item["ranges"]]
    assert len(with_ranges) == 8  # the 8 descriptors this dump did not truncate


def test_a_corrupted_blob_is_dropped_not_parsed():
    """One flipped byte fails the CRC, and the blob is refused whole."""
    payload = bytes([0x10, catalog.TAG_WATER_ML, 0, 20, 0, 250, 1, 164])
    raw = bytearray(base64.b64decode(_blob(catalog.FAMILY_DESCRIPTOR, payload)))
    raw[5] ^= 0xFF
    corrupted = base64.b64encode(bytes(raw)).decode()
    assert catalog.decode_blob(corrupted)["crc_valid"] is False
    cat = catalog.build_catalog({"d001_rec_espresso": {"value": corrupted}})
    assert cat["stats"]["crc_failed"] == 1
    assert cat["beverages"] == {}


def test_tlv_refuses_a_partial_walk():
    """A trailing byte returns None, never a half-parsed parameter set."""
    assert catalog.parse_tlv(bytes([0x1B, 0x02])) == {0x1B: 2}
    assert catalog.parse_tlv(bytes([0x01, 0x00, 0x30])) == {0x01: 0x30}
    assert catalog.parse_tlv(bytes([0x1B, 0x02, 0x01])) is None  # wide tag, one byte short
    assert catalog.parse_tlv(bytes([0x1B])) is None


def test_triples_refuse_a_partial_walk():
    """Same refusal for the min/default/max form."""
    assert catalog.parse_triples(bytes([0x1B, 0, 1, 4])) == {0x1B: (0, 1, 4)}
    wide = bytes([0x0F, 0, 20, 0, 250, 1, 164])
    assert catalog.parse_triples(wide) == {0x0F: (20, 250, 420)}
    assert catalog.parse_triples(bytes([0x0F, 0, 20, 0, 250, 1])) is None


# --- what the machine says it can make ------------------------------------- #


def test_every_byte_after_the_profile_is_a_beverage_id(cat: dict, full_cat: dict):
    """The off-by-one this file now exists to prevent.

    The payload is `<profile> <bevid...>` with NO header byte, so reading the
    ids from `payload[2:]` eats each list's first entry. That shipped once: it
    reported the bean-system drink as absent from every list when the machine
    puts it first on all five, and it manufactured a "the contents drift between
    dumps" finding out of nothing.

    Two independent guards, because a length assertion alone would pass a parser
    that dropped the first id and gained a phantom at the end.
    """
    for catalogue in (cat, full_cat):
        priority = catalogue["priority_lists"]
        assert sorted(priority) == [1, 2, 3, 4, 5]
        assert {len(ids) for ids in priority.values()} == {19}
        # Every entry is an id the machine also declares elsewhere. A stray
        # header byte would not satisfy this.
        declared = catalog.catalog_beverage_ids(catalogue)
        for ids in priority.values():
            assert set(ids) <= declared, "a list entry that no descriptor declares"

    # And the one that pins the boundary directly: the bean system leads every
    # list on the reference machine, which is exactly the byte an off-by-one eats.
    assert cat["beverages"][0xC8]["priority_position"] == {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}


def test_being_in_no_short_list_does_not_mean_the_drink_is_unavailable(cat: dict):
    """The correction that cost the most to learn, pinned so it cannot come back.

    long_black, mug_to_go and brew_over_ice sit in none of the five lists, and it
    would be easy to read that as "this machine cannot make them" and hide their
    buttons. They are the counter-example to that reading: all three carry a
    factory descriptor, a recipe on every profile and a lifetime counter, so the
    machine plainly knows them. Membership is an ordering, not a capability.
    """
    unlisted = {i for i, item in cat["beverages"].items() if item["in_priority_list"] is False}
    assert {0x19, 0x1A, 0x1B} <= unlisted
    for bev_id in (0x19, 0x1A, 0x1B):
        item = cat["beverages"][bev_id]
        assert item["declared"] is True, "the machine carries a factory descriptor for it"
        assert item["profiles"], "and a recipe on every profile"
    listed = [i for i, item in cat["beverages"].items() if item["in_priority_list"] is True]
    assert len(listed) == 19


def test_in_priority_list_is_unknown_only_when_no_list_was_read():
    """"Not selected" and "no opinion" must stay distinguishable.

    Every drink gets a definite True/False once any list is read, including the
    custom slots and the bean system that no list enumerates - the flag reports
    membership, and membership of an unread list is what `None` is for.
    """
    descriptor = _blob(catalog.FAMILY_DESCRIPTOR, bytes([0x10, catalog.TAG_WATER_ML, 0, 20, 0, 250, 1, 164]))
    without = catalog.build_catalog({"d015_rec_hot_water": {"value": descriptor}})
    assert without["beverages"][0x10]["in_priority_list"] is None

    with_list = catalog.build_catalog(
        {
            "d015_rec_hot_water": {"value": descriptor},
            "d261_1_rec_priority": {"value": _blob(catalog.FAMILY_PRIORITY, bytes([1, 0xC8, 0x07]))},
        }
    )
    assert with_list["beverages"][0x10]["in_priority_list"] is False
    assert with_list["beverages"][0x07]["in_priority_list"] is True


def test_custom_slots_are_found_and_reported_as_unprogrammed(cat: dict):
    """Six saved-recipe slots exist; none is programmed on this machine.

    The machine's own "not defined yet" state (no quantity, 0xff intensity) is
    detectable, which is what lets a UI hide an empty slot instead of offering a
    button that brews nothing.
    """
    custom = {i: item for i, item in cat["beverages"].items() if item["kind"] == "custom"}
    assert set(custom) == set(catalog.CUSTOM_SLOT_IDS)
    assert all(item["defined"] is False for item in custom.values())
    assert all(len(item["profiles"]) == 5 for item in custom.values())


def test_a_programmed_custom_slot_is_reported_as_defined():
    """The same detection, the other way round."""
    payload = bytes([1, 0xE6, catalog.TAG_COFFEE_ML, 0x00, 0x50, catalog.TAG_INTENSITY, 3])
    blob = _blob(catalog.FAMILY_PROFILE_RECIPE, payload)
    cat = catalog.build_catalog({"d200_1_cstm_recipe_01": {"value": blob}})
    assert cat["beverages"][0xE6]["defined"] is True


def test_the_bean_system_drink_is_discovered(cat: dict):
    """0xc8 is a real brewable entry the hardcoded beverage list has never had."""
    bean = cat["beverages"][0xC8]
    assert bean["kind"] == "bean_system"
    assert bean["declared"] is True
    assert bean["ranges"], "its factory descriptor is readable in this dump"


def test_builtin_ids_match_the_shipped_beverage_list(cat: dict):
    """21 built-in ids - the 2x_espresso trap, counted the hard way.

    A regex like ``_rec_[a-z]`` silently drops ``2x_espresso`` because the drink
    key starts with a digit, giving 20. Parsing by blob family cannot make that
    mistake: the id is a byte.
    """
    builtin = {i for i, item in cat["beverages"].items() if item["kind"] == "builtin"}
    assert len(builtin) == 21
    assert 0x04 in builtin


# --- parsing by family, not by name ---------------------------------------- #


@pytest.mark.parametrize(
    "name",
    [
        "d022_beansystem_1",  # the bean-system descriptor
        "d160_1_bs_recipe_01",  # a per-profile bean-system recipe
        "d200_1_cstm_recipe_01",  # a saved-recipe slot
        "d036_recipe_custom_name_1_3",  # the slot names
    ],
)
def test_the_substring_filter_misses_what_family_parsing_finds(props: dict, name: str):
    """These four shapes are exactly what the old ``"_rec_" in name`` filter lost.

    Every one of them is a custom or bean-system recipe - the entries a
    catalogue exists to discover.
    """
    assert "_rec_" not in name, "the legacy filter would skip it"
    assert name in dict(catalog.iter_blobs(props)), "the family switch finds it"


def test_the_legacy_name_filter_undercounts_the_recipes(props: dict):
    """137 by name, 194 blobs by family - measured on the reference machine."""
    by_name = [n for n in props if "_rec_" in n]
    assert len(by_name) == 137
    assert len(dict(catalog.iter_blobs(props))) == 194


def test_the_dump_diagnostic_now_selects_by_family(props: dict):
    """``recipe_dump_lines`` must surface the custom and bean-system recipes."""
    rendered = "\n".join(cb.recipe_dump_lines(props))
    assert "d022_beansystem_1 [family b0f0]" in rendered
    assert "d200_1_cstm_recipe_01 [family a6f0]" in rendered
    assert "d261_1_rec_priority [family a8f0]" in rendered
    assert len(cb.recipe_dump_lines(props)) == 184


def test_the_dump_never_publishes_an_identifier(props: dict):
    """This log is the block the README tells users to paste into an issue.

    The machine wraps its serial number, its settings PIN, the monitor blob and
    the response channel in the same ``0xd0`` envelope as its recipes, so
    selecting on the prefix alone would put a serial in every bug report. Only
    the six catalogue families are rendered.
    """
    rendered = "\n".join(cb.recipe_dump_lines(props))
    for excluded in (
        "d270_serialnumber",  # family a10f - the machine's serial, in ASCII
        "d280_mchn_sett_pin",  # family 950f
        "d302_monitor",  # family 750f
        "data_response",  # family a9f0
    ):
        assert excluded not in rendered, f"{excluded} must never reach a public log"


def test_the_dump_redacts_names_the_household_typed_in(props: dict):
    """Profile names are first names. Their structure is useful; their text is not."""
    rendered = "\n".join(cb.recipe_dump_lines(props))
    assert "d034_profiles_1_3 [family a4f0]" in rendered, "the blob is still dumped"
    assert "bytes of name redacted>" in rendered
    # The fixture's placeholders stand in for real names - none may be rendered.
    for placeholder in ("Profile 2", "Slot 2", "Bean 1"):
        encoded = placeholder.encode("utf-16-be").hex(" ")
        assert encoded not in rendered


def test_property_names_are_labels_only(props: dict):
    """The blob header decides what a datapoint is - the name never does.

    Renaming every property Eletta-style (the same drinks are ``d059_rec_1_*``
    there, numbers shifted by 20) must change nothing about what is discovered.
    """
    renamed = {f"zz{i:03d}_whatever": prop for i, prop in enumerate(props.values())}

    def _without_labels(cat: dict) -> dict:
        # source_properties is the one field that is *meant* to carry the names.
        return {
            bev_id: {k: v for k, v in item.items() if k != "source_properties"}
            for bev_id, item in cat["beverages"].items()
        }

    assert _without_labels(catalog.build_catalog(renamed)) == _without_labels(
        catalog.build_catalog(props)
    )


# --- degrading honestly ---------------------------------------------------- #


def test_no_properties_yields_an_empty_source_not_an_empty_catalogue():
    cat = catalog.build_catalog({})
    assert cat["source"] == "empty"
    assert cat["beverages"] == {}
    assert "no machine catalogue" in catalog.catalog_summary(cat)
    assert "no machine catalogue" in catalog.catalog_summary(None)


@pytest.mark.parametrize("value", [None, "", "   ", 42, "not base64 !!", "QUJD", {"a": 1}])
def test_non_blob_values_are_ignored_without_raising(value):
    assert catalog.decode_blob(value) is None


def test_decode_text_reads_the_first_slot_only():
    """Names are UTF-16BE in NUL-padded slots; only slot 1 aligns reliably."""
    payload = "Perso 1".encode("utf-16-be") + b"\x00\x00" + "Perso 2".encode("utf-16-be")
    assert catalog.decode_text(payload) == "Perso 1"
    assert catalog.decode_text(b"\x00\x00\x00\x00") is None
    assert catalog.decode_text(b"") is None


def test_summary_reports_what_was_unreadable(cat: dict):
    """The diagnostic line must show the truncation, not hide it."""
    summary = catalog.catalog_summary(cat)
    assert "28 beverage ids" in summary
    assert "in a profile short list" in summary
    assert "truncated=31" in summary


# --- the learn gate -------------------------------------------------------- #


def test_catalog_beverage_ids_lists_every_declared_id(cat: dict):
    ids = catalog.catalog_beverage_ids(cat)
    assert 0xE6 in ids, "a saved-recipe slot"
    assert 0xC8 in ids, "the bean-system drink"
    assert 0x01 in ids
    assert catalog.catalog_beverage_ids(None) == set()
    assert catalog.catalog_beverage_ids({}) == set()


def test_a_saved_recipe_brewed_from_the_app_is_now_learnable(cat: dict):
    """The bug this fixes: "Perso 1" pressed in the official app was discarded.

    ``0xe6`` is not in the hardcoded beverage list, so the captured frame - the
    only way to ever reproduce that drink - was dropped in silence.
    """
    frame = cb.encode_command(cb.build_beverage_command(0xE6, 0x01))
    decoded = cb.decode_command(frame)
    assert decoded["crc_valid"] is True
    assert cb.learnable_beverage_id(decoded) is None, "the old gate dropped it"
    assert cb.learnable_beverage_id(decoded, catalog.catalog_beverage_ids(cat)) == 0xE6


def test_widening_the_gate_still_refuses_an_id_no_one_declares(cat: dict):
    frame = cb.encode_command(cb.build_beverage_command(0x7F, 0x01))
    decoded = cb.decode_command(frame)
    assert cb.learnable_beverage_id(decoded, catalog.catalog_beverage_ids(cat)) is None


def test_widening_the_gate_does_not_weaken_the_checksum_rule(cat: dict):
    """A corrupt frame stays unlearnable however many ids the machine declares."""
    raw = bytearray(base64.b64decode(cb.encode_command(cb.build_beverage_command(0xE6, 0x01))))
    raw[4] = 0xE7  # id changed after the CRC was computed
    decoded = cb.decode_command(base64.b64encode(bytes(raw)).decode())
    assert decoded["crc_valid"] is False
    assert cb.learnable_beverage_id(decoded, catalog.catalog_beverage_ids(cat)) is None


def test_a_blob_too_short_for_its_own_header_is_refused():
    """A frame can pass the CRC and still be too short to index.

    ``d0 05 b0 f0 <crc>`` is a well-formed, checksum-valid descriptor with no
    beverage id in it. Reading ``payload[0]`` there would raise and take the
    whole catalogue down with it, so the shape is checked before any indexing.
    """
    for family in (
        catalog.FAMILY_DESCRIPTOR,
        catalog.FAMILY_PROFILE_RECIPE,
        catalog.FAMILY_PRIORITY,
        catalog.FAMILY_BEAN_NAMES,
    ):
        blob = _blob(family, b"")
        assert catalog.decode_blob(blob)["crc_valid"] is True
        cat = catalog.build_catalog({"d001_rec_espresso": {"value": blob}})
        assert cat["stats"]["short_payloads"] == 1
        assert cat["beverages"] == {}


def test_a_listed_entry_without_a_recipe_is_listed_but_not_declared():
    """In a short list, yet no descriptor: worth showing, not worth learning."""
    blob = _blob(catalog.FAMILY_PRIORITY, bytes([1, 0xC8, 0x07]))
    cat = catalog.build_catalog({"d261_1_rec_priority": {"value": blob}})
    entry = cat["beverages"][0x07]
    assert entry["in_priority_list"] is True
    assert entry["declared"] is False
    assert catalog.catalog_beverage_ids(cat) == set()


def test_the_fixture_carries_no_identifiers(props: dict):
    """The fixture ships in a public repo - it must hold nothing personal.

    Every blob is decoded and searched, both as UTF-16BE (how the machine stores
    names) and as Latin-1 (how it stores the serial). Only the placeholders
    written by ``fixtures/anonymise_dump.py`` may survive.
    """
    import re

    allowed = re.compile(r"^(Profile|Slot|Bean) \d+$|^X*0*$")
    found: list[tuple[str, str]] = []
    for name in props:
        raw = catalog._b64_bytes(props[name].get("value"))
        if not raw:
            continue
        for encoding in ("utf-16-be", "latin-1"):
            for match in re.finditer(
                r"[A-Za-z][A-Za-z0-9 ._/()-]{3,}", raw.decode(encoding, "ignore")
            ):
                if not allowed.match(match.group().strip()):
                    found.append((name, match.group()))
    assert not found, f"identifying text survived anonymisation: {found}"

    # Not every identifier is text. The Wi-Fi MAC is three plain integers, and a
    # scan that only decodes the base64 strings walks straight past it.
    for mac_part in ("d521_radio_mac_addr0", "d522_radio_mac_addr1", "d523_radio_mac_addr2"):
        assert props[mac_part]["value"] == 0, f"{mac_part} still holds the real MAC"


def test_the_fixture_is_the_real_thing(props: dict):
    """Anonymising must not have turned the dump into synthetic data.

    The rewritten name blobs get fresh CRCs; everything the parser reasons about
    - recipes, descriptors, menus - is the machine's own untouched bytes.
    """
    assert len(props) == 312
    assert props["software_version"]["value"].startswith("Millcore_demo")
    assert catalog.build_catalog(props)["stats"]["crc_failed"] == 0


# --- the derived signals, pinned to real values ---------------------------- #


def test_off_default_reports_what_the_user_actually_changed(cat: dict):
    """Tea is customised, Coffee is not - and the parser can tell, exactly.

    Both have a readable factory descriptor, so the comparison is available for
    both. Tea sits off its factory default on water volume (0x0f), temperature
    (0x1b) and 0x0d; Coffee sits on the default for every parameter it has.
    Asserting only that the dict exists let both branches rot.
    """
    tea = cat["beverages"][0x16]["off_default"]
    assert tea == {0x19: False, catalog.TAG_WATER_ML: True, catalog.TAG_TEMPERATURE: True, 0x0D: True}
    coffee = cat["beverages"][0x02]["off_default"]
    assert coffee == {
        0x19: False,
        catalog.TAG_TEMPERATURE: False,
        catalog.TAG_COFFEE_ML: False,
        catalog.TAG_INTENSITY: False,
        0x04: False,
    }
    assert set(coffee.values()) == {False}, "untouched means every tag False, not an empty dict"


def test_profiles_differ_is_asserted_both_ways(cat: dict):
    """Espresso differs across profiles; Coffee does not."""
    assert cat["beverages"][0x01]["profiles_differ"] is True
    assert cat["beverages"][0x02]["profiles_differ"] is False

    same = {
        f"d0{39 + i}_{p}_rec_espresso": {
            "value": _blob(
                catalog.FAMILY_PROFILE_RECIPE, bytes([p, 0x01, catalog.TAG_COFFEE_ML, 0x00, 0x28])
            )
        }
        for i, p in enumerate((1, 2, 3))
    }
    assert catalog.build_catalog(same)["beverages"][0x01]["profiles_differ"] is False


def test_the_summary_says_unpublished_rather_than_zero():
    """No a8f0 blob means no opinion, and the summary must not imply zero."""
    payload = bytes([0x10, catalog.TAG_WATER_ML, 0, 20, 0, 250, 1, 164])
    cat = catalog.build_catalog({"d015_rec_hot_water": {"value": _blob(catalog.FAMILY_DESCRIPTOR, payload)}})
    assert cat["beverages"][0x10]["in_priority_list"] is None
    assert cat["priority_lists"] == {}
    assert "no profile short list published" in catalog.catalog_summary(cat)


def test_an_unparsable_payload_is_counted_and_never_half_parsed():
    """A leftover byte must leave the values absent, with the drink still declared."""
    cat = catalog.build_catalog(
        {
            # a wide tag with only one of its two value bytes
            "d040_1_rec_regular": {
                "value": _blob(
                    catalog.FAMILY_PROFILE_RECIPE, bytes([1, 0x02, catalog.TAG_COFFEE_ML, 0x00])
                )
            },
            # a triple missing its last value
            "d015_rec_hot_water": {
                "value": _blob(catalog.FAMILY_DESCRIPTOR, bytes([0x10, catalog.TAG_WATER_ML, 0, 20, 0]))
            },
        }
    )
    assert cat["stats"]["unparsed_payloads"] == 2
    assert cat["beverages"][0x02]["profiles"] == {}
    assert cat["beverages"][0x10]["declared"] is True
    assert cat["beverages"][0x10]["ranges"] == {}


def test_name_blobs_are_filed_by_family_and_slot():
    """The three text families each land in their own bucket, keyed by slot.

    The reference dump truncates every name blob, so this is the only coverage
    the ``names`` bucket has - without it the whole feature could be deleted and
    the suite would stay green.
    """
    cat = catalog.build_catalog(
        {
            "d034_profiles_1_3": {
                "value": _blob(
                    catalog.FAMILY_PROFILE_NAMES, bytes([1, 3]) + "Alice".encode("utf-16-be")
                )
            },
            "d036_recipe_custom_name_1_3": {
                "value": _blob(
                    catalog.FAMILY_CUSTOM_NAMES, bytes([2, 3]) + "Perso 1".encode("utf-16-be")
                )
            },
            "d250_beansystem_0": {
                "value": _blob(
                    catalog.FAMILY_BEAN_NAMES, bytes([0, 0]) + "Kimbo".encode("utf-16-be")
                )
            },
        }
    )
    assert cat["names"] == {
        "profiles": {1: "Alice"},
        "custom": {2: "Perso 1"},
        "beans": {0: "Kimbo"},
    }


def test_summary_reports_every_counter(cat: dict):
    """Spot-checking three substrings let the other five counters mutate freely."""
    assert catalog.catalog_summary(cat) == (
        "28 beverage ids (19 in a profile short list, 9 in none, "
        "6 custom slots of which 0 programmed), 8 with factory min/default/max "
        "ranges, 5 profile lists | blobs=194 crc_ok=163 crc_failed=0 "
        "truncated=31 unparsed=0 short=0"
    )


# --- change detection ------------------------------------------------------ #


def test_the_fingerprint_ignores_the_live_channels(props: dict):
    """The monitor and response datapoints wear the same envelope as a recipe.

    They also carry a fresh timestamp on nearly every poll. Including them would
    change the key constantly and re-parse the catalogue for nothing.
    """
    key = catalog.catalog_fingerprint(props)
    names = [name for name, _ in key]
    assert len(key) == 184, "the six catalogue families, nothing else"
    for live in ("d302_monitor", "data_response", "d270_serialnumber", "d280_mchn_sett_pin"):
        assert live not in names

    moved = dict(props)
    moved["d302_monitor"] = {"value": _blob(b"\x75\x0f", bytes([1, 2, 3]))}
    assert catalog.catalog_fingerprint(moved) == key


def test_the_fingerprint_sees_a_recipe_change(props: dict):
    """The direction that must never fail: an edited recipe changes the key."""
    key = catalog.catalog_fingerprint(props)
    edited = dict(props)
    edited["d039_1_rec_espresso"] = {
        "value": _blob(
            catalog.FAMILY_PROFILE_RECIPE, bytes([1, 0x01, catalog.TAG_COFFEE_ML, 0x00, 0x3C])
        )
    }
    assert catalog.catalog_fingerprint(edited) != key

    # ...and so does a recipe disappearing entirely.
    removed = {k: v for k, v in props.items() if k != "d039_1_rec_espresso"}
    assert catalog.catalog_fingerprint(removed) != key


def test_the_fingerprint_normalises_whitespace_like_the_parser(props: dict):
    """Ayla wraps string datapoints in whitespace; the parser strips it.

    A key that compared raw values would report a change the parser cannot see,
    or miss one it can - a change-detector out of step with the thing it guards
    is how a cache freezes without anyone noticing.
    """
    key = catalog.catalog_fingerprint(props)
    padded = {
        name: {"value": f"\n {prop['value']} \n" if isinstance(prop["value"], str) else prop["value"]}
        for name, prop in props.items()
    }
    assert catalog.catalog_fingerprint(padded) == key
    assert catalog.build_catalog(padded)["beverages"] == catalog.build_catalog(props)["beverages"]


def test_blob_family_reads_the_family_without_a_full_decode(props: dict):
    assert catalog.blob_family(props["d039_1_rec_espresso"]["value"]) == catalog.FAMILY_PROFILE_RECIPE
    assert catalog.blob_family(props["d261_1_rec_priority"]["value"]) == catalog.FAMILY_PRIORITY
    assert catalog.blob_family(props["d270_serialnumber"]["value"]) == b"\xa1\x0f"
    for not_a_blob in (None, "", "AA==", "not base64 !!", 42, "QUJDRA=="):
        assert catalog.blob_family(not_a_blob) is None


# --- the same machine, read whole ------------------------------------------- #
#
# fixtures/soul_properties.json is a full 312-property dump whose values the
# dump tool truncated at 50 base64 characters, which is a real shape the parser
# has to survive - 31 of its blobs are cut, including 20 of the 28 factory
# descriptors. This second fixture is the same machine's catalogue read again
# with that cut removed, straight off the running integration: fewer properties,
# but every one of them whole, and it reaches the saved-recipe and bean-system
# blobs the old `_rec_` name filter never selected.
#
# It needs no anonymisation and that is not an oversight. The dump it came from
# renders recipe families only - no serial, no settings PIN, no monitor blob -
# and redacts the user-entered text of the three name families, which is why
# those 11 blobs are absent here rather than anonymised. The test below enforces
# the result rather than trusting it.

FULL_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "soul_recipes_full.json"


@pytest.fixture(scope="module")
def full_props() -> dict:
    return json.loads(FULL_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def full_cat(full_props: dict) -> dict:
    return catalog.build_catalog(full_props)


def test_the_full_dump_parses_with_nothing_left_over(full_cat: dict):
    """173 blobs, 173 checksums, nothing cut and nothing the grammar could not eat.

    This is the parser's first run against bytes it was not written from: the
    truncated dump is what it was developed on, this one arrived afterwards.
    """
    stats = full_cat["stats"]
    assert stats["blobs"] == 173
    assert stats["crc_ok"] == 173
    assert stats["crc_failed"] == 0
    assert stats["truncated"] == 0
    assert stats["unparsed_payloads"] == 0
    assert stats["short_payloads"] == 0


def test_removing_the_cut_makes_every_schema_readable(cat: dict, full_cat: dict):
    """The truncation was hiding the machine's own parameter ranges.

    Same machine, same descriptors: 20 of the 28 were unreadable purely because
    the dump tool stopped at 50 base64 characters.

    Every id the truncated dump could read is still readable here, and the set
    is now complete: 28 of 28, including the bean-system descriptor `0xc8` that
    the old `_rec_` name filter never even selected.
    """
    truncated_ranges = {i for i, item in cat["beverages"].items() if item["ranges"]}
    full_ranges = {i for i, item in full_cat["beverages"].items() if item["ranges"]}
    assert len(truncated_ranges) == 8
    assert full_ranges == set(full_cat["beverages"]), "every declared id has its schema"
    assert len(full_ranges) == 28
    assert truncated_ranges < full_ranges, "nothing readable before became unreadable"


def test_espresso_ranges_are_now_readable(full_cat: dict):
    """Espresso is the drink whose descriptor the old dump cut in half."""
    espresso = full_cat["beverages"][0x01]
    assert espresso["descriptor_truncated"] is False
    assert espresso["ranges"][catalog.TAG_COFFEE_ML] == (20, 40, 180)
    assert espresso["ranges"][catalog.TAG_TEMPERATURE] == (0, 1, 4)


def test_customisation_becomes_knowable_instead_of_unknown(cat: dict, full_cat: dict):
    """`off_default` was None for espresso only because the default was missing.

    With the factory descriptor readable, the same per-profile values resolve to
    a definite answer: 48 ml against a factory 40 is a customisation, and the
    parser now says so rather than shrugging.
    """
    assert cat["beverages"][0x01]["off_default"] is None
    espresso = full_cat["beverages"][0x01]
    assert espresso["off_default"][catalog.TAG_COFFEE_ML] is True
    volumes = {p[catalog.TAG_COFFEE_ML] for p in espresso["profiles"].values()}
    assert volumes == {40, 48}

    # And the mirror case, so this does not just assert that everything is
    # customised: doppio sits exactly on its factory default on every profile.
    doppio = full_cat["beverages"][0x05]
    assert doppio["ranges"][catalog.TAG_COFFEE_ML][1] == 120
    assert doppio["off_default"][catalog.TAG_COFFEE_ML] is False


def test_the_recipe_fixture_needs_no_anonymisation(full_props: dict):
    """Recipe blobs only - no serial, no MAC, no name the household typed in.

    Pinned rather than assumed, because the day someone refreshes this file from
    a wider dump is the day it starts carrying identifiers.
    """
    import re

    for name, prop in full_props.items():
        raw = catalog._b64_bytes(prop["value"])
        assert raw is not None, name
        for encoding in ("utf-16-be", "latin-1"):
            text = raw.decode(encoding, "ignore")
            assert not re.search(r"[A-Za-z][A-Za-z0-9 ._/()-]{3,}", text), f"{name}: {text!r}"
    families = {catalog.blob_family(p["value"]) for p in full_props.values()}
    assert families <= {
        catalog.FAMILY_DESCRIPTOR,
        catalog.FAMILY_PROFILE_RECIPE,
        catalog.FAMILY_PRIORITY,
    }, "a text-bearing family would need the anonymiser"


def test_nothing_about_this_machine_is_unknown_any_more(full_cat: dict):
    """The whole point of reading the machine whole: no shrugging left.

    Every declared drink has its factory schema, so `off_default` resolves to a
    real answer for all of them. On the truncated dump 20 of the 28 had to say
    `None`.
    """
    assert all(item["off_default"] is not None for item in full_cat["beverages"].values())
    assert all(item["descriptor_truncated"] is False for item in full_cat["beverages"].values())


def test_the_saved_recipe_slots_are_read_and_reported_empty(full_cat: dict):
    """Six slots, five profiles each, none programmed on the reference machine.

    These blobs are the ones the old `_rec_` name filter never selected, so they
    could not be tested against real bytes until the dump learned to select by
    family. `defined` is False here, not None: the machine's own unprogrammed
    sentinel was read, not guessed.
    """
    custom = {i: item for i, item in full_cat["beverages"].items() if item["kind"] == "custom"}
    assert set(custom) == set(catalog.CUSTOM_SLOT_IDS)
    assert all(item["defined"] is False for item in custom.values())
    assert all(len(item["profiles"]) == 5 for item in custom.values())


def test_the_bean_system_drink_carries_a_real_schema(full_cat: dict):
    """`0xc8` is brewable, has ranges, and is in no hardcoded beverage list."""
    bean = full_cat["beverages"][0xC8]
    assert bean["kind"] == "bean_system"
    assert bean["ranges"][catalog.TAG_COFFEE_ML] == (30, 40, 60)
    assert bean["ranges"][catalog.TAG_INTENSITY] == (1, 4, 5)
    assert len(bean["profiles"]) == 5, "one per-profile recipe per profile"
    assert bean["in_priority_list"] is True, "it is in three of the five lists"


def test_the_lists_reorder_between_dumps_but_never_change_membership(cat: dict, full_cat: dict):
    """What actually moves between two dumps of one machine: the order, only.

    This test previously asserted the opposite - that membership drifted - and
    it passed, because the parser was eating each list's first entry and
    manufacturing the difference. All ten lists carry the same 19 ids; profiles
    1, 4 and 5 differ between the dumps by two adjacent transpositions each,
    which is what a most-recently-used ordering looks like.
    """
    sets = [frozenset(ids) for ids in cat["priority_lists"].values()]
    sets += [frozenset(ids) for ids in full_cat["priority_lists"].values()]
    assert len(set(sets)) == 1, "one membership set across ten lists"
    assert len(sets[0]) == 19

    def order(catalogue: dict, profile: int) -> list:
        return catalogue["priority_lists"][profile]

    for profile in (2, 3):
        assert order(cat, profile) == order(full_cat, profile), "these two never moved"
    for profile in (1, 4, 5):
        before, after = order(cat, profile), order(full_cat, profile)
        assert before != after, "these reordered"
        assert sorted(before) == sorted(after), "without gaining or losing a drink"
        moved = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        assert moved == [0, 1, 4, 5], "two adjacent swaps, nothing else"


def test_a_single_entry_list_is_read_not_discarded():
    """The smallest possible list: one profile byte, one beverage id.

    `_MIN_PAYLOAD[FAMILY_PRIORITY]` has to be 1, not 2. At 2 this frame clears
    the guard and then parses to an empty list, which reports a confident "not
    selected" for the only drink the machine selected - the exact shape of lie
    this module exists to avoid.
    """
    blob = _blob(catalog.FAMILY_PRIORITY, bytes([1, 0x07]))
    cat = catalog.build_catalog({"d261_1_rec_priority": {"value": blob}})
    assert cat["priority_lists"] == {1: [0x07]}
    assert cat["beverages"][0x07]["in_priority_list"] is True
    assert cat["stats"]["short_payloads"] == 0
    assert "1 in a profile short list" in catalog.catalog_summary(cat)


def test_a_priority_blob_with_no_ids_at_all_is_refused():
    """A profile byte and nothing else says nothing, so it must not say "none"."""
    blob = _blob(catalog.FAMILY_PRIORITY, bytes([1]))
    cat = catalog.build_catalog({"d261_1_rec_priority": {"value": blob}})
    assert cat["priority_lists"] == {1: []}
    assert cat["stats"]["short_payloads"] == 0
