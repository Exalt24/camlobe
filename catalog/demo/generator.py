"""Invented supplier feeds with the mess real ones have. Every name, number and price is made up.

One universe of canonical parts is shown to three invented suppliers, each writing the same part
differently (spacing, dashes, brand prefixes, zero padding, brand spelling) and each with its own
faults. A truth file records which canonical part every written row stands for, so a test can check
the pipeline's grouping against it instead of against itself.

  redline  CSV, windows-1252 with a few UTF-8 lines and a few undecodable bytes, exact duplicate rows,
           a repeated row with a new price, blank part numbers, "N/A" costs.
  harbor   XML, brand missing on some items, zero padded numbers, two items that reuse another part's
           GTIN (a renumbered part) and one that carries a GTIN that disagrees with the first.
  delta    three CSV files with a BOM: the base file, then the same columns in another order with some
           new costs, then a rename of two columns that is still a known alias. A fourth file renames a
           column to something unknown and must be refused.
"""
from __future__ import annotations

import csv
import io
import json
import random
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from ..normalize import BRAND_ALIASES, BRAND_PREFIXES

BRANDS = list(BRAND_ALIASES)
WORDS = {
    "KESTREL": ["Brake pad set", "Brake rotor", "Brake caliper", "Brake hose", "Drum shoe"],
    "MARLOW": ["Oil filter", "Air filter", "Cabin filter", "Fuel filter", "Transmission filter"],
    "DUNMORE": ["Spark plug", "Ignition coil", "Coil boot", "Plug wire set", "Distributor cap"],
    "TARN": ["Radiator", "Water pump", "Thermostat", "Radiator hose", "Fan clutch"],
    "HALDEN": ["Strut assembly", "Control arm", "Ball joint", "Sway bar link", "Tie rod end"],
    "ORSETT": ["Alternator", "Starter motor", "Battery cable", "Ignition switch", "Wiper motor"],
}
ACCENTED = {"Brake caliper": "Étrier de frein", "Water pump": "Pompe à eau"}
LETTERS = "ACEFGJKLMNPQRSTUVWXYZ"  # none of the letters a brand prefix starts with in a way that clashes


@dataclass
class Universe:
    parts: list[dict]


def _check_digit(body: str) -> str:
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return str((10 - total % 10) % 10)


def _canonical_number(brand: str, rng: random.Random, serial: int) -> str:
    if brand == "KESTREL":  # a 10 digit family code that starts with a real zero
        return f"0{rng.randint(100, 999)}{rng.randint(100, 999)}{serial % 1000:03d}"
    if brand == "MARLOW":
        return f"{rng.choice('HNPRSW')}{serial % 10000:04d}{rng.choice('ABC')}"
    if brand == "DUNMORE":
        return f"ZR{serial % 10000:04d}{rng.choice(['', 'X', 'P'])}"
    if brand == "TARN":
        return f"{rng.randint(1, 9)}{serial % 100000:05d}"
    if brand == "HALDEN":
        return f"{rng.choice('KJ')}{rng.randint(10, 99)}{serial % 1000:03d}"
    return f"{rng.randint(10, 99)}{rng.choice(LETTERS)}{serial % 10000:04d}"


def build_universe(size: int, seed: int) -> Universe:
    rng = random.Random(seed)
    parts: list[dict] = []
    seen: set[tuple[str, str]] = set()
    serial = 0
    while len(parts) < size:
        serial += 1
        brand = BRANDS[serial % len(BRANDS)]
        number = _canonical_number(brand, rng, serial * 7 + rng.randint(0, 6))
        if (brand, number) in seen:
            continue
        seen.add((brand, number))
        gtin = ""
        if rng.random() < 0.7:
            body = f"{rng.randint(10**11, 10**12 - 1)}"
            gtin = body + _check_digit(body)
        title = rng.choice(WORDS[brand])
        parts.append(
            {
                "brand": brand,
                "number": number,
                "title": title,
                "gtin": gtin,
                "cost_cents": rng.randint(350, 48000),
            }
        )
    return Universe(parts)


def _spaced(number: str, rng: random.Random) -> str:
    if len(number) <= 4:
        return number
    cut = rng.randint(2, len(number) - 2)
    sep = rng.choice([" ", "-", ".", "-"])
    return number[:cut] + sep + number[cut:]


def _brand_name(brand: str, rng: random.Random) -> str:
    name = rng.choice((brand.title(), *BRAND_ALIASES[brand]))
    return rng.choice([name, name.upper(), name.lower()]) if rng.random() < 0.3 else name


def _redline_number(brand: str, number: str, rng: random.Random) -> str:
    form = rng.random()
    if form < 0.35:
        return _spaced(number, rng)
    if form < 0.5:
        return number.lower()
    if form < 0.65:
        return f"{rng.choice(BRAND_PREFIXES[brand])}-{_spaced(number, rng)}"
    return number


def _harbor_sku(brand: str, number: str, rng: random.Random) -> str:
    if number.isdigit() and len(number) < 8 and rng.random() < 0.5:
        return number.zfill(rng.choice([8, 10, 12]))
    if rng.random() < 0.4:
        return f"{rng.choice(BRAND_PREFIXES[brand])}{rng.choice(['', '-', ' '])}{number}"
    return number


def _cost(cents: int, rng: random.Random, style: str) -> str:
    dollars = f"{cents // 100}.{cents % 100:02d}"
    if style == "dollar" and rng.random() < 0.4:
        return "$" + dollars
    if style == "thousands" and cents >= 100000:
        return f"{cents // 100:,}.{cents % 100:02d}"
    if rng.random() < 0.15:
        return dollars.rstrip("0").rstrip(".") if "." in dollars else dollars
    return dollars


def write_feeds(out: Path, size: int = 20000, seed: int = 7) -> dict:
    """Write every feed and the truth file into `out`. Returns the row counts per file."""
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    uni = build_universe(size, seed)
    truth: list[dict] = []
    counts: dict[str, int] = {}

    # ---- redline: CSV in windows-1252 ----------------------------------------------------------
    header = ["PartNo", "Mfr", "Description", "Cost", "UPC"]
    lines: list[bytes] = [",".join(header).encode("cp1252") + b"\r\n"]
    stored = 0
    repeats: list[list[str]] = []
    for part in uni.parts:
        if rng.random() > 0.7:
            continue
        pn = _redline_number(part["brand"], part["number"], rng)
        brand = _brand_name(part["brand"], rng)
        desc = ACCENTED.get(part["title"], part["title"]) if rng.random() < 0.5 else part["title"]
        row = [pn, brand, desc, _cost(part["cost_cents"], rng, "dollar"), part["gtin"]]
        truth.append({"supplier": "redline", "part_number": pn, "brand": brand, "canon_brand": part["brand"], "canon_number": part["number"]})
        r = rng.random()
        if r < 0.005:
            row[0] = ""
        elif r < 0.01:
            row[3] = "N/A"
        buf = io.StringIO()
        csv.writer(buf, lineterminator="").writerow(row)
        text = buf.getvalue()
        if 0.01 <= r < 0.0125:
            encoded = text.encode("cp1252", errors="replace") + b"\x81"  # a byte windows-1252 does not define
        elif rng.random() < 0.05:
            encoded = text.encode("utf-8")  # a line pasted from a UTF-8 tool
        else:
            encoded = text.encode("cp1252", errors="replace")
        lines.append(encoded + b"\r\n")
        stored += 1
        if rng.random() < 0.03:
            lines.append(encoded + b"\r\n")  # exact duplicate row
            stored += 1
        elif rng.random() < 0.01:
            new_cost = _cost(max(100, int(part["cost_cents"] * 1.04)), rng, "dollar")
            changed = list(row)
            changed[3] = new_cost
            buf = io.StringIO()
            csv.writer(buf, lineterminator="").writerow(changed)
            repeats.append(buf.getvalue())
    for text in repeats:  # the same part again near the end of the file, with a new cost
        lines.append(text.encode("cp1252", errors="replace") + b"\r\n")
        stored += 1
    (out / "redline_catalog.csv").write_bytes(b"".join(lines))
    counts["redline_catalog.csv"] = stored

    # ---- harbor: XML ---------------------------------------------------------------------------
    items: list[str] = []
    harbor_parts = [p for p in uni.parts if rng.random() < 0.6]
    for part in harbor_parts:
        sku = _harbor_sku(part["brand"], part["number"], rng)
        brand = _brand_name(part["brand"], rng) if rng.random() > 0.04 else None
        truth.append(
            {"supplier": "harbor", "part_number": sku, "brand": brand or "", "canon_brand": part["brand"], "canon_number": part["number"]}
        )
        items.append(_harbor_item(sku, brand, part["title"], _cost(part["cost_cents"], rng, "thousands"), part["gtin"]))
    # a part that was renumbered: a different number, the same GTIN as an existing part
    with_gtin = [p for p in harbor_parts if p["gtin"]]
    for part in rng.sample(with_gtin, k=min(len(with_gtin), max(2, size // 2000))):
        sku = part["number"] + "R"
        items.append(_harbor_item(sku, part["brand"].title(), part["title"] + " (revised)", "19.99", part["gtin"]))
        truth.append({"supplier": "harbor", "part_number": sku, "brand": part["brand"].title(), "canon_brand": part["brand"], "canon_number": sku, "expect": "gtin_cross"})
    xml ='<?xml version="1.0" encoding="UTF-8"?>\n<catalog supplier="harbor">\n' + "\n".join(items) + "\n</catalog>\n"
    (out / "harbor_catalog.xml").write_text(xml, encoding="utf-8")
    counts["harbor_catalog.xml"] = len(items)

    # ---- delta: three good files and one that renames a column to something unknown ------------
    delta_parts = [p for p in uni.parts if rng.random() < 0.65]
    delta_keys = {(p["brand"], p["number"]) for p in delta_parts}
    # parts harbor lists with a GTIN, written here with a different valid GTIN: the two suppliers disagree
    disagree = [p for p in with_gtin if (p["brand"], p["number"]) not in delta_keys]
    for part in rng.sample(disagree, k=min(len(disagree), max(1, size // 4000))):
        body = f"{rng.randint(10**11, 10**12 - 1)}"
        delta_parts.append({**part, "gtin": body + _check_digit(body), "expect": "gtin_conflict"})
    base_rows = []
    for part in delta_parts:
        pn = part["number"] if rng.random() < 0.7 else _spaced(part["number"], rng)
        brand = _brand_name(part["brand"], rng)
        base_rows.append([pn, brand, part["title"], _cost(part["cost_cents"], rng, "plain"), part["gtin"], part["cost_cents"]])
        truth.append(
            {"supplier": "delta", "part_number": pn, "brand": brand, "canon_brand": part["brand"],
             "canon_number": part["number"], "expect": part.get("expect", "")}
        )

    def write_delta(name: str, headers: list[str], order: list[int], rows: list[list]) -> None:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(headers)
        for r in rows:
            w.writerow([r[i] for i in order])
        (out / name).write_bytes(b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8"))
        counts[name] = len(rows)

    cols = ["item_number", "brand", "desc", "unit_cost", "ean"]
    write_delta("delta_2026-10-01.csv", cols, [0, 1, 2, 3, 4], base_rows)
    second = [list(r) for r in base_rows]
    for r in second:
        if rng.random() < 0.05:
            r[3] = _cost(int(r[5] * 1.05), rng, "plain")
    write_delta("delta_2026-10-15.csv", ["ean", "unit_cost", "desc", "brand", "item_number"], [4, 3, 2, 1, 0], second)
    third = [list(r) for r in second]
    for r in third:
        if rng.random() < 0.03:
            r[3] = _cost(int(r[5] * 1.08), rng, "plain")
    write_delta("delta_2026-10-22.csv", ["item_number", "manufacturer", "desc", "cost_usd", "ean"], [0, 1, 2, 3, 4], third)
    write_delta("delta_2026-10-29_unmapped.csv", ["item_number", "producer", "desc", "unit_cost", "ean"], [0, 1, 2, 3, 4], second[:200])

    with (out / "truth.csv").open("w", newline="", encoding="utf-8") as handle:
        w = csv.DictWriter(handle, fieldnames=["supplier", "part_number", "brand", "canon_brand", "canon_number", "expect"])
        w.writeheader()
        for row in truth:
            w.writerow({"expect": "", **row})
    counts["fitment_aces.csv"] = write_fitment(out / "fitment_aces.csv", uni, rng)
    (out / "manifest.json").write_text(json.dumps({"seed": seed, "universe": size, "rows": counts}, indent=2), encoding="utf-8")
    return counts


VEHICLES = [
    (make, model, engines)
    for make, models in {
        "Altair": [("T200", ["2.0L I4", "3.5L V6"]), ("T300", ["1.6L I4 Turbo", "2.0L I4"])],
        "Brandt": [("Kombi", ["1.8L I4"]), ("Rover S", ["3.5L V6", "2.5L I4"])],
        "Corvane": [("Dart", ["2.4L I4"]), ("Ridge", ["3.6L V6", "2.4L I4"])],
        "Deltoro": [("Mesa", ["2.0L I4 Turbo"]), ("Pico", ["1.3L I3"])],
    }.items()
    for model, engines in models
]
POSITIONED = {"Brake pad set": ["front", "rear"], "Brake rotor": ["front", "rear"], "Strut assembly": ["front", "rear"], "Control arm": ["front"]}


def write_fitment(path: Path, uni: Universe, rng: random.Random) -> int:
    """An ACES style application file for invented vehicles. Part numbers are written messily, as a catalog would."""
    rows = [["brand", "part_number", "year", "make", "model", "submodel", "engine", "position", "note"]]
    for part in uni.parts:
        if rng.random() > 0.35:
            continue
        for _ in range(rng.randint(1, 3)):
            make, model, engines = rng.choice(VEHICLES)
            year = rng.randint(2008, 2024)
            for position in POSITIONED.get(part["title"], [""]):
                rows.append([
                    _brand_name(part["brand"], rng), _redline_number(part["brand"], part["number"], rng),
                    str(year), make, model, rng.choice(["", "Base", "Sport"]), rng.choice(engines), position, "",
                ])
    # rows a loader must refuse: a part nobody lists, a year that is not one
    rows.append(["Marlow", "ZZ-99999", "2015", "Altair", "T200", "", "2.0L I4", "", ""])
    rows.append(["Marlow", rows[1][1], "twenty-fifteen", "Altair", "T200", "", "2.0L I4", "", ""])
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle, lineterminator="\r\n").writerows(rows)
    return len(rows) - 1


def _harbor_item(sku: str, brand: str | None, title: str, cost: str, gtin: str) -> str:
    parts = [f"<sku>{escape(sku)}</sku>"]
    if brand is not None:
        parts.append(f"<manufacturer>{escape(brand)}</manufacturer>")
    parts.append(f"<title>{escape(title)}</title>")
    parts.append(f'<price currency="USD">{escape(cost)}</price>')
    if gtin:
        parts.append(f"<gtin>{gtin}</gtin>")
    return "<item>" + "".join(parts) + "</item>"
