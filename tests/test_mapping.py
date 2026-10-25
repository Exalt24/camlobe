"""Header mapping and cost parsing: what maps, what is refused, and that nothing is guessed."""
import pytest

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
