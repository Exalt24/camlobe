"""Header mapping, cost parsing and row identity for incoming feed rows.

A feed is mapped by column NAME, never by position, so a supplier that reorders its columns changes
nothing. A column renamed to a known alias maps as before. A required column that no header maps to
fails the run before a row is written (SchemaDriftError), because mapping it to "empty" would turn a
whole feed into unbranded rows without a single error.

Two columns for one REQUIRED field (SKU and MPN both read as the part number) are still refused: which one is
the supplier's number is a decision, not a default. Two columns for an OPTIONAL field are ordinary (UPC and EAN
side by side): a row takes the first valid GTIN in header order, and the first non empty description, and the run
notes say which columns were used and how often.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Iterable

from ..normalize import normalize_gtin

REQUIRED = ("part_number", "brand", "cost")
OPTIONAL = ("description", "gtin")

FIELD_ALIASES: dict[str, frozenset[str]] = {
    "part_number": frozenset({"partno", "partnumber", "part", "sku", "itemnumber", "itemno", "mpn", "mfrpartno"}),
    "brand": frozenset({"brand", "mfr", "manufacturer", "mfg", "make", "linecode"}),
    "description": frozenset({"description", "desc", "title", "itemdescription", "partdescription"}),
    "cost": frozenset({"cost", "unitcost", "costusd", "dealerprice", "wholesale", "price", "netcost"}),
    "gtin": frozenset({"upc", "ean", "gtin", "barcode"}),
}


class SchemaDriftError(Exception):
    """The feed's columns no longer map to what the pipeline needs."""


def header_key(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.lower())


def resolve_headers(headers: Iterable[str]) -> dict[str, str | tuple[str, ...]]:
    """canonical field -> the header that carries it (a tuple, in header order, for a doubled optional field).

    Raises SchemaDriftError on a missing required field or a doubled required one. A set is read in sorted
    order; any other iterable keeps its order, which is what "first valid GTIN" means.
    """
    ordered = sorted(headers) if isinstance(headers, (set, frozenset)) else list(dict.fromkeys(headers))
    found: dict[str, list[str]] = {}
    for header in ordered:
        key = header_key(header)
        for field, aliases in FIELD_ALIASES.items():
            if key in aliases:
                found.setdefault(field, []).append(header)
    missing = [f for f in REQUIRED if f not in found]
    if missing:
        raise SchemaDriftError(
            f"no column maps to {', '.join(missing)}; the feed has {', '.join(sorted(headers)) or 'no columns'}"
        )
    doubled = {f: h for f, h in found.items() if len(h) > 1 and f in REQUIRED}
    if doubled:
        raise SchemaDriftError(f"more than one column maps to the same field: {doubled}")
    return {f: (hs[0] if len(hs) == 1 else tuple(hs)) for f, hs in found.items()}


def mapped_headers(mapping: dict[str, str | tuple[str, ...]]) -> set[str]:
    return {h for value in mapping.values() for h in ((value,) if isinstance(value, str) else value)}


def unmapped_columns(headers: Iterable[str], mapping: dict[str, str | tuple[str, ...]]) -> list[str]:
    return sorted(set(headers) - mapped_headers(mapping))


def doubled_columns(mapping: dict[str, str | tuple[str, ...]]) -> dict[str, list[str]]:
    return {f: list(v) for f, v in mapping.items() if not isinstance(v, str)}


class RowRejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def parse_cost_cents(text: str) -> int | None:
    """Dollars as text to integer cents. Blank is None. Anything ambiguous is rejected, never guessed.

    Accepted: 12, 12.5, 12.50, $12.50, 1,234.50, 12.5000. Rejected: 12,50 (a decimal comma looks like a
    thousands separator), 12.505 (half a cent), -3, N/A, and a cost above ten million dollars.
    """
    value = (text or "").strip().replace("$", "").replace(" ", "")
    if value == "":
        return None
    if "," in value:
        if not re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", value):
            raise RowRejected("bad_cost")
        value = value.replace(",", "")
    if not re.fullmatch(r"\d+(\.\d+)?", value):
        raise RowRejected("bad_cost")
    try:
        amount = Decimal(value)
    except InvalidOperation:  # pragma: no cover - the pattern above already guarantees a number
        raise RowRejected("bad_cost")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise RowRejected("bad_cost")
    if cents > 10_000_000 * 100:
        raise RowRejected("bad_cost")
    return int(cents)


@dataclass(frozen=True)
class MappedRow:
    part_number: str
    brand: str
    description: str
    gtin: str
    cost_cents: int | None
    picked: dict = field(default_factory=dict, compare=False)  # doubled optional field -> the header this row used


def _value(fields: dict[str, str], header: str) -> str:
    return (fields.get(header) or "").strip()


def _pick(fields: dict[str, str], headers: tuple[str, ...], field_name: str) -> tuple[str, str]:
    """(value, header) from a doubled optional field: the first valid GTIN, or the first non empty description."""
    values = [(h, _value(fields, h)) for h in headers]
    if field_name == "gtin":
        for header, value in values:
            if normalize_gtin(value)[0]:
                return value, header
    for header, value in values:
        if value:
            return value, header
    return "", ""


def map_row(fields: dict[str, str], mapping: dict[str, str | tuple[str, ...]]) -> MappedRow:
    picked: dict[str, str] = {}

    def get(field_name: str) -> str:
        header = mapping.get(field_name)
        if not header:
            return ""
        if isinstance(header, str):
            return _value(fields, header)
        value, used = _pick(fields, header, field_name)
        if used:
            picked[field_name] = used
        return value

    part_number = get("part_number")
    if not part_number:
        raise RowRejected("blank_part_number")
    return MappedRow(
        part_number=part_number,
        brand=get("brand"),
        description=get("description"),
        gtin=get("gtin"),
        cost_cents=parse_cost_cents(get("cost")),
        picked=picked,
    )


def row_identity(fields: dict[str, str], mapping: dict[str, str | tuple[str, ...]]) -> tuple[str, str] | None:
    """(part number, brand) as the feed wrote them, or None when the row names no part. Cost is not looked at."""
    number, brand = mapping.get("part_number"), mapping.get("brand")
    part_number = _value(fields, number) if isinstance(number, str) else ""
    if not part_number:
        return None
    return part_number, (_value(fields, brand) if isinstance(brand, str) else "")


def content_hash(row: MappedRow) -> str:
    text = json.dumps(
        [row.part_number, row.brand, row.description, row.gtin, row.cost_cents], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def idempotency_key(supplier_code: str, source_ref: str, position: int, content: str) -> str:
    text = f"{supplier_code}\x1f{source_ref}\x1f{position}\x1f{content}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
