"""Header mapping and cost parsing: what maps, what is refused, and that nothing is guessed."""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from catalog.ingest.mapping import (
    RowRejected,
    SchemaDriftError,
    content_hash,
    idempotency_key,
    map_row,
    parse_cost_cents,
    resolve_headers,
    unmapped_columns,
)


def test_column_order_never_matters():
    a = resolve_headers({"item_number", "brand", "desc", "unit_cost", "ean"})
    b = resolve_headers({"ean", "unit_cost", "desc", "brand", "item_number"})
    assert a == b == {
        "part_number": "item_number", "brand": "brand", "description": "desc", "cost": "unit_cost", "gtin": "ean",
    }


@pytest.mark.parametrize(
    "headers,field,expected",
    [
        ({"PartNo", "Mfr", "Cost"}, "brand", "Mfr"),
        ({"sku", "manufacturer", "price"}, "cost", "price"),
        ({"Item Number", "Brand", "Cost USD"}, "cost", "Cost USD"),  # case, spaces and punctuation are ignored
        ({"item_number", "manufacturer", "cost_usd"}, "brand", "manufacturer"),
    ],
)
def test_known_aliases_map(headers, field, expected):
    assert resolve_headers(headers)[field] == expected


@pytest.mark.parametrize(
    "headers,missing",
    [
        ({"item_number", "producer", "unit_cost"}, "brand"),  # a rename nobody told us about
        ({"item_number", "brand"}, "cost"),
        ({"brand", "cost"}, "part_number"),
        (set(), "part_number"),
    ],
)
def test_a_required_column_with_no_mapping_is_drift(headers, missing):
    with pytest.raises(SchemaDriftError) as err:
        resolve_headers(headers)
    assert missing in str(err.value)


def test_two_columns_for_one_field_is_refused():
    with pytest.raises(SchemaDriftError, match="more than one column"):
        resolve_headers({"sku", "brand", "cost", "price"})


def test_unmapped_columns_are_reported_not_dropped_silently():
    headers = {"sku", "brand", "cost", "warehouse", "bin"}
    assert unmapped_columns(headers, resolve_headers(headers)) == ["bin", "warehouse"]


@pytest.mark.parametrize(
    "text,cents",
    [
        ("12", 1200), ("12.5", 1250), ("12.50", 1250), ("$12.50", 1250), (" 12.50 ", 1250),
        ("1,234.50", 123450), ("12.5000", 1250), ("0.99", 99), ("0", 0), ("", None), ("   ", None),
        ("10000000", 1_000_000_000),
    ],
)
def test_cost_accepted(text, cents):
    assert parse_cost_cents(text) == cents


@pytest.mark.parametrize(
    "text",
    ["12,50", "N/A", "-3", "12.505", "1,23.45", "abc", "12.5.0", "1e3", "10000000.01", "12 EUR", "1,234,5"],
)
def test_cost_refused_never_guessed(text):
    with pytest.raises(RowRejected) as err:
        parse_cost_cents(text)
    assert err.value.reason == "bad_cost"


def test_cost_is_an_integer_not_a_float():
    assert parse_cost_cents("19.99") == 1999  # float("19.99") * 100 is 1998.9999999999998
    assert isinstance(parse_cost_cents("19.99"), int)


def test_map_row_requires_a_part_number_and_strips_whitespace():
    mapping = resolve_headers({"sku", "brand", "cost"})
    row = map_row({"sku": "  AB-123 ", "brand": " Marlow ", "cost": "5"}, mapping)
    assert (row.part_number, row.brand, row.cost_cents) == ("AB-123", "Marlow", 500)
    with pytest.raises(RowRejected) as err:
        map_row({"sku": "  ", "brand": "Marlow", "cost": "5"}, mapping)
    assert err.value.reason == "blank_part_number"


def test_keys_depend_on_what_they_should():
    row = map_row({"sku": "AB123", "brand": "Marlow", "cost": "5"}, resolve_headers({"sku", "brand", "cost"}))
    same = map_row({"sku": "AB123", "brand": "Marlow", "cost": "5.00"}, resolve_headers({"sku", "brand", "cost"}))
    other = map_row({"sku": "AB123", "brand": "Marlow", "cost": "6"}, resolve_headers({"sku", "brand", "cost"}))
    assert content_hash(row) == content_hash(same)  # 5 and 5.00 are the same row
    assert content_hash(row) != content_hash(other)
    h = content_hash(row)
    key = idempotency_key("s1", "a.csv", 1, h)
    assert key == idempotency_key("s1", "a.csv", 1, h)
    assert key != idempotency_key("s2", "a.csv", 1, h)  # another supplier
    assert key != idempotency_key("s1", "b.csv", 1, h)  # another source
    assert key != idempotency_key("s1", "a.csv", 2, h)  # another row


def test_a_doubled_optional_field_is_kept_in_header_order_and_a_doubled_required_one_is_refused():
    mapping = resolve_headers(["PartNo", "Brand", "Cost", "UPC", "EAN", "Title", "Description"])
    assert mapping["gtin"] == ("UPC", "EAN") and mapping["description"] == ("Title", "Description")
    assert resolve_headers(["EAN", "PartNo", "Brand", "Cost", "UPC"])["gtin"] == ("EAN", "UPC")  # left to right, not alphabetical
    assert unmapped_columns(["PartNo", "Brand", "Cost", "UPC", "EAN", "Bin"], resolve_headers(["PartNo", "Brand", "Cost", "UPC", "EAN"])) == ["Bin"]
    with pytest.raises(SchemaDriftError, match="more than one column"):
        resolve_headers(["SKU", "MPN", "Brand", "Cost"])


def test_the_first_valid_gtin_wins_and_the_first_filled_description_wins():
    mapping = resolve_headers(["PartNo", "Brand", "Cost", "UPC", "EAN", "Title", "Description"])
    got = map_row({"PartNo": "AB123", "Brand": "Marlow", "Cost": "1", "UPC": "123", "EAN": "5012345678900", "Title": "", "Description": "Oil"}, mapping)
    assert (got.gtin, got.description, got.picked) == ("5012345678900", "Oil", {"gtin": "EAN", "description": "Description"})
    junk = map_row({"PartNo": "AB123", "Brand": "Marlow", "Cost": "1", "UPC": "123", "EAN": "999"}, mapping)
    assert junk.gtin == "123"  # nothing valid: the first value is kept so it is flagged invalid, not silently dropped


# ---- second output round: a cell holding two amounts is rejected, never glued into one -----------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "$20 $15", "$20$15", "20 $15", "$20 15", "20 15", "$20 $15.50", "$20.50 $15", "$ 20 $ 15", "20  15", "12 50", "$1,200 $999",
        "$20.00 $15.00", "$1 234 $999",
    ],
)
def test_a_cell_with_a_struck_price_and_a_new_price_is_rejected_not_glued_into_one_amount(text):
    """`$20` then `$15` used to lose the space and become 2015 dollars, stored as 201500 cents."""
    with pytest.raises(RowRejected) as err:
        parse_cost_cents(text)
    assert err.value.reason == "bad_cost"


@pytest.mark.parametrize(
    "text, cents",
    [
        ("$20", 2000), ("20", 2000), ("20.00", 2000), ("$ 20", 2000), ("$20.00", 2000), (" $ 20.00 ", 2000), ("$1,234", 123400), ("$1,234.56", 123456),
        ("1 234.50", 123450), ("12 345", 1234500), ("$1 234 567.89", 123456789), ("12 .50", 1250), ("$0.05", 5), ("1,000,000", 100_000_000),
    ],
)
def test_every_ordinary_price_shape_still_parses_as_it_did(text, cents):
    assert parse_cost_cents(text) == cents


@given(st.integers(0, 9_999_999_99), st.integers(0, 9_999_999_99), st.sampled_from(["$", ""]), st.sampled_from(["$", ""]), st.sampled_from([" ", "  ", " $ "]))
@settings(max_examples=300, deadline=None)
def test_two_amounts_in_any_shape_are_never_one_price_and_one_amount_in_any_shape_is_its_price(a, b, sign_a, sign_b, glue):
    def shown(cents, sign, thousands):
        whole, frac = divmod(cents, 100)
        body = f"{whole:,}" if thousands else str(whole)
        return f"{sign}{body}.{frac:02d}"  # a whole amount with no cents and no sign is the one case that is not told apart, see docs/feeds.md

    for thousands in (False, True):
        assert parse_cost_cents(shown(a, sign_a, thousands)) == a
        with pytest.raises(RowRejected):
            parse_cost_cents(f"{shown(a, sign_a, thousands)}{glue}{shown(b, sign_b, thousands)}")
