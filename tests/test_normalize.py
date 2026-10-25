"""normalize_part_number: a table of rules, the cases that must NOT be merged, and properties."""
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from catalog.normalize import (
    BRAND_ALIASES,
    BRAND_PREFIXES,
    NORMALIZER_VERSION,
    canonical_brand,
    normalize_gtin,
    normalize_part_number,
)

# (raw as a supplier wrote it, canonical brand, canonical number, rules that fired)
RULE_TABLE = [
    ("mf-h4123a", "MARLOW", "H4123A", ("R2", "R3", "R4")),
    ("MF H4123A", "MARLOW", "H4123A", ("R3", "R4")),
    ("H4123A", "MARLOW", "H4123A", ()),
    ("KB 0440 011 187", "KESTREL", "0440011187", ("R3", "R4")),
    ("0440011187", "KESTREL", "0440011187", ()),  # a real leading zero is kept for this brand
    ("00716042", "TARN", "716042", ("R5",)),
    ("0000200022", "TARN", "200022", ("R5",)),
    ("TC-0000200022", "TARN", "200022", ("R3", "R4", "R5")),
    ("0123456", "TARN", "0123456", ()),  # 7 digits: not a fixed width export, kept
    ("00000012", "TARN", "0012", ("R5",)),  # never stripped below 4 digits
    ("DUN-MORE ZR4519", "DUNMORE", "ZR4519", ("R3", "R4")),
    ("zr 4519", "DUNMORE", "ZR4519", ("R2", "R3")),
    ("ac-1234", "", "AC1234", ("R2", "R3")),
    ("AB.12/34_56", "", "AB123456", ("R3",)),
    ("ÀBC-123", "", "ABC123", ("R1", "R3")),  # A grave
    ("１２３４５", "", "12345", ("R1",)),  # full width digits
    ("MF-12", "MARLOW", "MF12", ("R3",)),  # the prefix stays when fewer than 3 characters remain
    ("MF-ABC", "MARLOW", "MFABC", ("R3",)),  # ... or when what remains has no digit
    ("0440011187", "", "440011187", ("R5",)),  # unbranded: the zero rule applies, which is why no brand means review
]


@pytest.mark.parametrize("raw,brand,value,rules", RULE_TABLE)
def test_rule_table(raw, brand, value, rules):
    result = normalize_part_number(raw, brand)
    assert result.ok
    assert result.value == value
    assert set(result.rules) == set(rules)


@pytest.mark.parametrize(
    "raw,error",
    [
        ("", "too_short"),
        ("AB", "too_short"),
        ("--  --", "too_short"),
        ("X" * 33 + "1", "too_long"),
        ("АВС123", "non_ascii_letters"),  # Cyrillic A, V, S look-alikes: never mapped by guess
        ("ß-12", None),  # sharp s folds to SS, which is ASCII
    ],
)
def test_rejections(raw, error):
    result = normalize_part_number(raw)
    if error is None:
        assert result.ok
    else:
        assert not result.ok
        assert result.error == error
        assert result.value == ""


def test_numbers_that_differ_stay_different():
    assert normalize_part_number("AB1").value != normalize_part_number("AB11").value
    assert normalize_part_number("AB-12").value == normalize_part_number("AB12").value
    assert normalize_part_number("12345").value != normalize_part_number("12354").value
    # the letter O is not the digit zero: no guessing
    assert normalize_part_number("AB0C1").value != normalize_part_number("ABOC1").value
    # a zero that is part of the number survives when it is not a fixed width export
    assert normalize_part_number("0123").value == "0123"


def test_brand_names_and_aliases():
    for canon, names in BRAND_ALIASES.items():
        for name in (canon, *names, name_variant(canon)):
            assert canonical_brand(name) == canon
    assert canonical_brand("  ") == ""
    assert canonical_brand(None) == ""
    assert canonical_brand("Some New Brand!") == "SOMENEWBRAND"  # unknown brands keep their letters
    assert canonical_brand("Marlow") != canonical_brand("Marlowe")  # a near spelling is not an alias


def name_variant(canon: str) -> str:
    return canon.lower().capitalize() + " "


@pytest.mark.parametrize(
    "raw,expected,flag",
    [
        ("5012345678900", "05012345678900", ""),
        ("501234567890", "00501234567890", ""),  # a 12 digit UPC is the same code as the 13 digit form with a 0 in front
        ("0501234567890", "00501234567890", ""),
        ("5012345678901", "", "gtin_invalid"),  # wrong check digit
        ("12345", "", "gtin_invalid"),  # wrong length
        ("", "", ""),
        (None, "", ""),
        ("5012-3456-78900", "05012345678900", ""),
    ],
)
def test_gtin(raw, expected, flag):
    assert normalize_gtin(raw) == (expected, flag)


def test_version_is_exposed():
    assert isinstance(NORMALIZER_VERSION, int) and NORMALIZER_VERSION >= 1


# ---- properties -------------------------------------------------------------------------------

ALNUM = st.text(alphabet="ABCDEFGHJKLMNPQRSTUVWXYZ0123456789", min_size=3, max_size=20)
BRANDS = st.sampled_from(["", *BRAND_ALIASES])
SEPARATORS = st.sampled_from([" ", "-", ".", "/", "_", " - "])


@given(ALNUM, BRANDS)
@settings(max_examples=300, deadline=None)
def test_idempotent(raw, brand):
    first = normalize_part_number(raw, brand)
    assume(first.ok)
    assert normalize_part_number(first.value, brand).value == first.value


@given(ALNUM, BRANDS, st.data())
@settings(max_examples=300, deadline=None)
def test_separators_and_case_never_change_the_result(raw, brand, data):
    noisy = ""
    for ch in raw:
        noisy += ch.lower() if data.draw(st.booleans()) else ch
        if data.draw(st.booleans()):
            noisy += data.draw(SEPARATORS)
    assert normalize_part_number(noisy, brand).value == normalize_part_number(raw, brand).value


@given(ALNUM, st.sampled_from(sorted(BRAND_PREFIXES)), st.data())
@settings(max_examples=300, deadline=None)
def test_a_brand_prefix_never_changes_the_result(raw, brand, data):
    assume(any(c.isdigit() for c in raw))
    prefix = data.draw(st.sampled_from(BRAND_PREFIXES[brand]))
    sep = data.draw(SEPARATORS)
    assert normalize_part_number(prefix + sep + raw, brand).value == normalize_part_number(raw, brand).value


@given(st.text(max_size=40), BRANDS)
@settings(max_examples=300, deadline=None)
def test_output_is_only_ascii_letters_and_digits_or_a_rejection(raw, brand):
    result = normalize_part_number(raw, brand)
    if result.ok:
        assert result.value.isascii() and result.value.isalnum() and result.value == result.value.upper()
        assert 3 <= len(result.value) <= 32
    else:
        assert result.value == "" and result.error


@given(ALNUM, ALNUM)
@settings(max_examples=300, deadline=None)
def test_different_numbers_do_not_collide_when_no_rule_applies(a, b):
    # same brand, both without a prefix, a digit and not a fixed width export: equal output means equal input
    for x in (a, b):
        assume(any(c.isdigit() for c in x) and not x.isdigit())
    assert (normalize_part_number(a).value == normalize_part_number(b).value) == (a == b)
