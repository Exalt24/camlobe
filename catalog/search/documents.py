"""The search document for a part, and how a search string is read.

A string with a digit in it is read as a part number and normalised the way a feed's numbers are, so
"h-4123 a" finds H4123A. A string without a digit is a text search over brand and description.
Part number matching is exact or by prefix and never fuzzy: a number one character off is a different part.
"""
from __future__ import annotations

import re

from django.contrib.postgres.aggregates import ArrayAgg
from django.db.models import Min, Q

from ..models import Part
from ..normalize import BRAND_ALIASES, canonical_brand, normalize_gtin, normalize_part_number


def interpret_query(q: str) -> dict:
    """{"kind": "number", "value": "H4123A"} or {"kind": "text", "value": "oil filter"}."""
    q = (q or "").strip()
    gtin, _ = normalize_gtin(q) if re.fullmatch(r"[\d\- ]{8,17}", q) else ("", "")
    if gtin:
        return {"kind": "gtin", "value": gtin, "brand": ""}
    if re.search(r"\d", q):
        # a leading brand word is dropped when it is a known brand ("marlow h4123a")
        words = q.split()
        brand = ""
        if len(words) > 1 and canonical_brand(words[0]) in _known_brands():
            brand = canonical_brand(words[0])
            q = " ".join(words[1:])
        result = normalize_part_number(q, brand)
        if result.ok:
            return {"kind": "number", "value": result.value, "brand": brand}
        digits_and_letters = re.sub(r"[^A-Za-z0-9]", "", q).upper()
        return {"kind": "number", "value": digits_and_letters, "brand": brand}
    return {"kind": "text", "value": q, "brand": ""}


def _known_brands() -> set[str]:
    return set(BRAND_ALIASES)


def documents_for(parts_qs) -> list[dict]:
    """Search documents for the parts in a queryset, with their supplier codes and lowest cost."""
    rows = (
        parts_qs.annotate(
            supplier_codes=ArrayAgg("sources__supplier__code", distinct=True, filter=Q(sources__isnull=False)),
            lowest_cost=Min("sources__cost_cents"),
        )
        .order_by("id")
        .values("id", "brand", "number_norm", "description", "gtin", "supplier_codes", "lowest_cost", "search_version")
    )
    return [
        {
            "id": r["id"],
            "brand": r["brand"],
            "number": r["number_norm"],
            "description": r["description"],
            "gtin": r["gtin"],
            "suppliers": sorted(r["supplier_codes"] or []),
            "supplier_count": len(r["supplier_codes"] or []),
            "lowest_cost_cents": r["lowest_cost"],
            "version": r["search_version"],
        }
        for r in rows
    ]
