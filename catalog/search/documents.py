"""The search document for a part, and how a search string is read.

One reading of a query serves both backends (`search_legs`), so PostgreSQL and Meilisearch cannot drift apart:

  - nothing but punctuation or space: no legs, an empty answer;
  - a valid GTIN (8, 12, 13 or 14 digits with a check digit): a GTIN leg, plus the number legs below;
  - a string with a digit: a part number. It is tried in every form a supplier might have printed it: as typed
    (letters and digits only), normalised with no brand, and normalised with each known brand's own rules
    (that brand's prefix removed, its zero rule applied). Each form is matched exactly or by prefix against the
    canonical numbers. A form that only one brand's rules produce matches that brand's parts only, so two
    brands that both fit a number both answer and the code never picks one;
  - otherwise text: every word must start a word of the brand or the description.

A number matches by prefix and never fuzzily: a number one character off is a different part.
"""
from __future__ import annotations

import re

from django.contrib.postgres.aggregates import ArrayAgg
from django.db.models import Min, Q

from ..models import Part
from ..normalize import BRAND_ALIASES, canonical_brand, normalize_gtin, normalize_part_number

_WORD = re.compile(r"[^\W_]+")


def _known_brands() -> list[str]:
    return list(BRAND_ALIASES)


def _typed(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", text).upper()


def interpret_query(q: str) -> dict:
    """The main reading of a query: kind number, gtin, text or empty, its value, the brand word it started with.

    `forms` lists every (brand, number) pair a number query is matched with; brand "" means any brand.
    """
    q = (q or "").strip()
    if not _WORD.search(q):
        return {"kind": "empty", "value": "", "brand": "", "forms": []}
    gtin, _ = normalize_gtin(q) if re.fullmatch(r"[\d\- ]{8,17}", q) else ("", "")
    if re.search(r"\d", q):
        words = q.split()
        brand = ""
        rest = q
        if len(words) > 1 and canonical_brand(words[0]) in _known_brands():
            brand = canonical_brand(words[0])
            rest = " ".join(words[1:])
        forms = number_forms(rest, brand)
        primary = normalize_part_number(rest, brand)
        value = primary.value if primary.ok else _typed(rest)
        if gtin:
            return {"kind": "gtin", "value": gtin, "brand": "", "forms": forms}
        return {"kind": "number", "value": value, "brand": brand, "forms": forms}
    return {"kind": "text", "value": q, "brand": "", "forms": []}


def number_forms(text: str, brand: str = "") -> list[tuple[str, str]]:
    """Every (brand, number) a number query is matched with, most literal first. Brand "" matches any brand."""
    typed = _typed(text)
    if not typed:
        return []
    forms: list[tuple[str, str]] = []

    def add(form_brand: str, value: str) -> None:
        if value and (form_brand, value) not in forms and ("", value) not in forms:
            forms.append((form_brand, value))

    if brand:
        for value in (normalize_part_number(text, brand).value, typed):
            add(brand, value)
        return forms
    add("", typed)
    add("", normalize_part_number(text, "").value)
    for known in _known_brands():
        add(known, normalize_part_number(text, known).value)
    return forms


def query_words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text)]


def search_legs(q: str, brand: str = "") -> list[dict]:
    """The legs of a search, ORed together: {"kind": "number"|"gtin"|"text", "value": str, "brand": str}.

    `brand` is the API's brand parameter. A leg that names another brand than the parameter cannot match and is
    dropped, so asking for two different brands at once gives no leg at all.
    """
    query = interpret_query(q)
    wanted = canonical_brand(brand)
    legs: list[dict] = []

    def add(kind: str, value: str, leg_brand: str) -> None:
        if wanted and leg_brand and leg_brand != wanted:
            return
        legs.append({"kind": kind, "value": value, "brand": leg_brand or wanted})

    if query["kind"] == "empty":
        return []
    if query["kind"] == "gtin":
        add("gtin", query["value"], "")
    if query["kind"] in ("number", "gtin"):
        for form_brand, value in query["forms"]:
            add("number", value, form_brand)
        return legs
    words = query_words(query["value"])
    if not words:
        return []
    add("text", " ".join(words), "")
    return legs


def leg_filter(leg: dict) -> Q:
    """The same leg as a database filter."""
    q = Q(brand=leg["brand"]) if leg["brand"] else Q()
    if leg["kind"] == "number":
        return q & Q(number_norm__startswith=leg["value"])
    if leg["kind"] == "gtin":
        return q & Q(gtin=leg["value"])
    for word in leg["value"].split():
        # a word of the query must start a word of the brand or the description, as Meilisearch's prefix search does
        pattern = rf"(^|[^[:alnum:]]){re.escape(word)}"
        q &= Q(description__iregex=pattern) | Q(brand__iregex=pattern)
    return q


def documents_for(parts_qs) -> list[dict]:
    """Search documents for the parts in a queryset, with their supplier codes and lowest cost.

    Only listings that are not delisted count: a supplier that dropped a part from its full feed no longer
    offers it, so it must not show as a supplier or set the lowest cost.
    """
    live = Q(sources__isnull=False, sources__delisted_at__isnull=True)
    rows = (
        parts_qs.annotate(
            supplier_codes=ArrayAgg("sources__supplier__code", distinct=True, filter=live),
            lowest_cost=Min("sources__cost_cents", filter=Q(sources__delisted_at__isnull=True)),
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
