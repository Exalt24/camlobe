"""Vehicle fitment in the shape of ACES: which part fits which year, make, model, submodel and engine.

The input is a CSV with the columns brand, part_number, year, make, model, submodel, engine, position,
note. The part is found the way a feed's part is: canonical brand plus the normalised number, so the
same part written differently still resolves. A row for a part the catalog does not have is rejected
with its reason, never turned into a part. Vehicle names are stored upper case and trimmed, so a
lookup is an index lookup and "Camry" and "CAMRY " are one vehicle. Loading the same file again changes nothing.
"""
from __future__ import annotations

import csv
from pathlib import Path

from django.db import transaction

from .models import Fitment, Part, PipelineRun, RejectedRow, Vehicle
from .normalize import canonical_brand, normalize_part_number
from .runs import add_error, advisory_lock, finish_run, start_run

COLUMNS = ("brand", "part_number", "year", "make", "model", "submodel", "engine", "position", "note")
MIN_YEAR, MAX_YEAR = 1950, 2100


def canon_vehicle_text(text: str | None) -> str:
    return " ".join((text or "").split()).upper()


def _clean_row(row: dict) -> tuple[str, str, int, str, str, str, str, str, str] | str:
    """The cleaned fields, or the reason the row is refused."""
    brand = canonical_brand(row.get("brand"))
    number = normalize_part_number(row.get("part_number"), brand)
    if not number.ok:
        return "bad_part_number"
    try:
        year = int((row.get("year") or "").strip())
    except ValueError:
        return "bad_year"
    if not MIN_YEAR <= year <= MAX_YEAR:
        return "bad_year"
    make, model = canon_vehicle_text(row.get("make")), canon_vehicle_text(row.get("model"))
    if not make or not model:
        return "missing_make_or_model"
    return (
        brand, number.value, year, make, model,
        canon_vehicle_text(row.get("submodel")), canon_vehicle_text(row.get("engine")),
        " ".join((row.get("position") or "").split()).lower(), (row.get("note") or "").strip()[:200],
    )


def load_fitment(path: str | Path, batch_size: int = 2000) -> PipelineRun:
    path = Path(path)
    with advisory_lock(f"fitment:{path.name}"):
        run = start_run(PipelineRun.Kind.FITMENT, source_ref=path.name)
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
                if missing:
                    raise ValueError(f"the file has no column {', '.join(missing)}")
                batch: list[tuple[int, dict]] = []
                for n, row in enumerate(reader, start=1):
                    batch.append((n, row))
                    if len(batch) >= batch_size:
                        _store(run, batch)
                        batch = []
                _store(run, batch)
            return finish_run(run, PipelineRun.Status.SUCCEEDED)
        except Exception as exc:
            add_error(run, "fitment", f"{type(exc).__name__}: {exc}")
            return finish_run(run, PipelineRun.Status.FAILED)


def _store(run: PipelineRun, batch: list[tuple[int, dict]]) -> None:
    if not batch:
        return
    cleaned: list[tuple[int, tuple]] = []
    rejected: list[RejectedRow] = []
    for n, row in batch:
        result = _clean_row(row)
        if isinstance(result, str):
            rejected.append(RejectedRow(run=run, supplier=None, position=n, reason=result, raw=repr(row)[:500]))
        else:
            cleaned.append((n, result))
    with transaction.atomic():
        keys = {(c[0], c[1]) for _, c in cleaned}
        parts = {(p.brand, p.number_norm): p.pk for p in Part.objects.filter(number_norm__in={k[1] for k in keys}) if (p.brand, p.number_norm) in keys}
        usable = []
        for n, c in cleaned:
            if (c[0], c[1]) in parts:
                usable.append(c)
            else:
                rejected.append(RejectedRow(run=run, supplier=None, position=n, reason="unknown_part", raw=f"{c[0]} {c[1]}"))
        wanted = {(c[2], c[3], c[4], c[5], c[6]) for c in usable}
        existing = {
            (v.year, v.make, v.model, v.submodel, v.engine): v.pk
            for v in Vehicle.objects.filter(year__in={w[0] for w in wanted}, make__in={w[1] for w in wanted}, model__in={w[2] for w in wanted})
            if (v.year, v.make, v.model, v.submodel, v.engine) in wanted
        }
        fresh = [Vehicle(year=w[0], make=w[1], model=w[2], submodel=w[3], engine=w[4]) for w in wanted if w not in existing]
        Vehicle.objects.bulk_create(fresh, ignore_conflicts=True)
        if fresh:
            existing.update(
                {
                    (v.year, v.make, v.model, v.submodel, v.engine): v.pk
                    for v in Vehicle.objects.filter(year__in={w[0] for w in wanted}, make__in={w[1] for w in wanted}, model__in={w[2] for w in wanted})
                    if (v.year, v.make, v.model, v.submodel, v.engine) in wanted
                }
            )
        pairs = {}
        for c in usable:
            part_id = parts[(c[0], c[1])]
            vehicle_id = existing[(c[2], c[3], c[4], c[5], c[6])]
            pairs[(part_id, vehicle_id, c[7])] = c[8]
        have = set(
            Fitment.objects.filter(part_id__in={k[0] for k in pairs}, vehicle_id__in={k[1] for k in pairs}).values_list(
                "part_id", "vehicle_id", "position"
            )
        )
        new = [Fitment(part_id=k[0], vehicle_id=k[1], position=k[2], note=v) for k, v in pairs.items() if k not in have]
        Fitment.objects.bulk_create(new, ignore_conflicts=True)
        RejectedRow.objects.bulk_create(rejected)
        counts = {
            "rows_read": run.rows_read + len(batch),
            "rows_written": run.rows_written + len(new),
            "rows_unchanged": run.rows_unchanged + len(usable) - len(new),
            "rows_rejected": run.rows_rejected + len(rejected),
        }
        PipelineRun.objects.filter(pk=run.pk).update(**counts)
    for name, value in counts.items():
        setattr(run, name, value)
