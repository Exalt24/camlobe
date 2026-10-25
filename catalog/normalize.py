"""Part number, brand and GTIN normalisation.

`normalize_part_number` is a pure function. Every rule has an id, and the ids that fired are
stored with the result, so a surprising canonical number can be explained from the row.

Rules, in the order they run (the same table is in docs/normalization.md):

  R1 FOLD      Unicode NFKC, then accents dropped (full width digits, ligatures, a dash that is not ASCII).
  R2 CASE      Upper case.
  R3 SEPARATORS  Every character that is not A-Z or 0-9 is removed: spaces, dashes, dots, slashes, underscores.
  R4 PREFIX    A brand or supplier prefix is removed when the brand is known and at least 3 characters
               including a digit remain. The prefixes come from BRAND_PREFIXES plus the brand's own name.
  R5 ZEROPAD   Leading zeros are removed from an all digit number of 8 or more digits (a fixed width export),
               keeping at least 4 digits, except for brands whose catalog numbers really start with 0.
  R6 NONASCII  A letter outside A-Z after R1 (a Cyrillic look-alike, for one) rejects the number. No guessing.
  R7 LENGTH    Fewer than 3 or more than 32 characters rejects the number.

R4 and R5 repeat until nothing changes, so normalising a canonical number returns it unchanged.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

NORMALIZER_VERSION = 1

MIN_LEN = 3
MAX_LEN = 32
ZEROPAD_MIN_DIGITS = 8
ZEROPAD_KEEP_DIGITS = 4

# Canonical brand -> every spelling a feed has used for it. Matching is on the letters and digits only.
BRAND_ALIASES: dict[str, tuple[str, ...]] = {
    "KESTREL": ("Kestrel", "Kestrel Brakes", "KESTREL BRAKE CO", "Kestrel Brake Company"),
    "MARLOW": ("Marlow", "Marlow Filters", "MARLOW FILTRATION", "Marlow Filter Co"),
    "DUNMORE": ("Dunmore", "Dunmore Ignition", "DUNMORE IGN", "Dun-More"),
    "TARN": ("Tarn", "Tarn Cooling", "TARN THERMAL", "Tarn Radiator"),
    "HALDEN": ("Halden", "Halden Suspension", "HALDEN SUSP", "Halden Chassis"),
    "ORSETT": ("Orsett", "Orsett Electrical", "ORSETT ELEC", "Orsett Auto Electric"),
}

# Prefixes a supplier glues on the front of a number. Real catalogs do this ("MF-", "KB ").
BRAND_PREFIXES: dict[str, tuple[str, ...]] = {
    "KESTREL": ("KB", "KST"),
    "MARLOW": ("MF", "MRL"),
    "DUNMORE": ("DU", "DNM"),
    "TARN": ("TC", "TRN"),
    "HALDEN": ("HS", "HLD"),
    "ORSETT": ("OE", "ORS"),
}

# Brands whose catalog numbers start with a zero that is part of the number (a 10 digit family code).
ZERO_SIGNIFICANT = frozenset({"KESTREL"})


def _alnum(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", text)


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


_ALIAS_LOOKUP: dict[str, str] = {}
for _canon, _names in BRAND_ALIASES.items():
    for _name in (_canon, *_names):
        _ALIAS_LOOKUP[_alnum(_fold(_name)).upper()] = _canon


def canonical_brand(raw: str | None) -> str:
    """Canonical brand for a feed's spelling. An unknown brand keeps its own letters and digits, upper case."""
    if not raw:
        return ""
    key = _alnum(_fold(raw)).upper()
    if not key:
        return ""
    return _ALIAS_LOOKUP.get(key, key)[:80]


@dataclass(frozen=True)
class NormalizedNumber:
    value: str
    rules: tuple[str, ...]
    error: str = ""  # empty when the number is usable

    @property
    def ok(self) -> bool:
        return not self.error


def _prefixes_for(brand: str) -> list[str]:
    names = {brand, *BRAND_PREFIXES.get(brand, ())}
    return sorted((n for n in names if n), key=len, reverse=True)


def _strip_prefix(value: str, brand: str) -> str | None:
    for prefix in _prefixes_for(brand):
        if value.startswith(prefix):
            rest = value[len(prefix):]
            if len(rest) >= MIN_LEN and any(c.isdigit() for c in rest):
                return rest
    return None


def _strip_zeros(value: str, brand: str) -> str | None:
    if brand in ZERO_SIGNIFICANT or not value.isdigit() or len(value) < ZEROPAD_MIN_DIGITS:
        return None
    stripped = value.lstrip("0")
    if stripped == value:
        return None
    if len(stripped) < ZEROPAD_KEEP_DIGITS:
        stripped = value[-ZEROPAD_KEEP_DIGITS:]
    return stripped if stripped != value else None


def normalize_part_number(raw: str | None, brand: str | None = None) -> NormalizedNumber:
    """Canonical form of a supplier's part number. `brand` is the canonical brand, or empty."""
    brand = brand or ""
    rules: list[str] = []
    text = _fold(raw or "")
    if text != (raw or ""):
        rules.append("R1")
    if text != text.upper():
        rules.append("R2")
    text = text.upper()
    # R6, before the separators go: a non ASCII letter is not something to guess a mapping for.
    if any(c.isalpha() and not ("A" <= c <= "Z") for c in text):
        return NormalizedNumber("", tuple(rules + ["R6"]), "non_ascii_letters")
    stripped = re.sub(r"[^A-Z0-9]", "", text)
    if stripped != text:
        rules.append("R3")
    value = stripped
    while True:  # every pass removes characters, so it ends; a canonical number is a fixed point
        changed = False
        rest = _strip_prefix(value, brand)
        if rest is not None:
            value, changed = rest, True
            rules.append("R4")
        rest = _strip_zeros(value, brand)
        if rest is not None:
            value, changed = rest, True
            rules.append("R5")
        if not changed:
            break
    if len(value) < MIN_LEN:
        return NormalizedNumber("", tuple(rules + ["R7"]), "too_short")
    if len(value) > MAX_LEN:
        return NormalizedNumber("", tuple(rules + ["R7"]), "too_long")
    return NormalizedNumber(value, tuple(dict.fromkeys(rules)))


def normalize_gtin(raw: str | None) -> tuple[str, str]:
    """(gtin, flag). A GTIN is 8, 12, 13 or 14 digits with a valid check digit; anything else becomes "".

    The flag is "" for no GTIN or a good one, "gtin_invalid" for a value that was there and was dropped.
    """
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return "", ""
    if len(digits) not in (8, 12, 13, 14) or not _gtin_check_ok(digits):
        return "", "gtin_invalid"
    return digits.zfill(14), ""


def _gtin_check_ok(digits: str) -> bool:
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check
