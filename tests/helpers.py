"""Small builders shared by the tests. Everything here is invented data."""
from __future__ import annotations

import csv
import io
from pathlib import Path

from catalog.ingest.adapters import FileAdapter
from catalog.ingest.runner import ingest
from catalog.matching import match_pending
from catalog.models import Supplier
from catalog.normalize_step import normalize_pending

CSV_HEADER = ["PartNo", "Mfr", "Description", "Cost", "UPC"]


def write_csv(path: Path, rows: list[list[str]], header: list[str] | None = None, encoding: str = "utf-8", bom: bool = False) -> Path:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header or CSV_HEADER)
    writer.writerows(rows)
    data = buf.getvalue().encode(encoding, errors="replace")
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)
    return path


def get_supplier(code: str = "s1") -> Supplier:
    supplier, _ = Supplier.objects.get_or_create(code=code, defaults={"name": code.title()})
    return supplier


def run_all(supplier: Supplier, path: Path, batch_size: int = 1000):
    run = ingest(supplier, FileAdapter(path), batch_size=batch_size)
    normalize_pending()
    match_pending()
    return run


def row(pn: str, brand: str = "Marlow", desc: str = "Oil filter", cost: str = "10.00", gtin: str = "") -> list[str]:
    return [pn, brand, desc, cost, gtin]
