"""Header mapping, cost parsing and row identity for incoming feed rows.

A feed is mapped by column NAME, never by position, so a supplier that reorders its columns changes
nothing. A column renamed to a known alias maps as before. A required column that no header maps to
fails the run before a row is written (SchemaDriftError), because mapping it to "empty" would turn a
whole feed into unbranded rows without a single error.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

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


def resolve_headers(headers: set[str]) -> dict[str, str]:
    """canonical field -> the header that carries it. Raises SchemaDriftError on a missing or doubled one."""
    found: dict[str, list[str]] = {}
    for header in sorted(headers):
        key = header_key(header)
        for field, aliases in FIELD_ALIASES.items():
            if key in aliases:
                found.setdefault(field, []).append(header)
    missing = [f for f in REQUIRED if f not in found]
    if missing:
        raise SchemaDriftError(
            f"no column maps to {', '.join(missing)}; the feed has {', '.join(sorted(headers)) or 'no columns'}"
        )
    doubled = {f: h for f, h in found.items() if len(h) > 1}
    if doubled:
        raise SchemaDriftError(f"more than one column maps to the same field: {doubled}")
    return {field: hs[0] for field, hs in found.items()}


def unmapped_columns(headers: set[str], mapping: dict[str, str]) -> list[str]:
    return sorted(headers - set(mapping.values()))


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


def map_row(fields: dict[str, str], mapping: dict[str, str]) -> MappedRow:
    def get(field: str) -> str:
        header = mapping.get(field)
        return (fields.get(header) or "").strip() if header else ""

    part_number = get("part_number")
    if not part_number:
        raise RowRejected("blank_part_number")
    return MappedRow(
        part_number=part_number,
        brand=get("brand"),
        description=get("description"),
        gtin=get("gtin"),
        cost_cents=parse_cost_cents(get("cost")),
    )


def content_hash(row: MappedRow) -> str:
    text = json.dumps(
        [row.part_number, row.brand, row.description, row.gtin, row.cost_cents], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def idempotency_key(supplier_code: str, source_ref: str, position: int, content: str) -> str:
    text = f"{supplier_code}\x1f{source_ref}\x1f{position}\x1f{content}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
