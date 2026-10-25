"""The normalize step: RawPart rows with no NormalizedRow yet get one. Restartable by construction.

The work queue is "raw rows that have no normalized row", so a killed run resumes where it stopped
(each batch commits whole) and a second run finds nothing to do. RawPart is only read.
"""
from __future__ import annotations

from django.db import transaction

from .models import NormalizedRow, PipelineRun, RawPart
from .normalize import NORMALIZER_VERSION, canonical_brand, normalize_gtin, normalize_part_number
from .runs import advisory_lock, add_error, finish_run, start_run


def normalize_pending(batch_size: int = 2000, limit: int | None = None) -> PipelineRun:
    with advisory_lock("normalize"):
        run = start_run(PipelineRun.Kind.NORMALIZE)
        try:
            after_id = 0
            while limit is None or run.rows_read < limit:
                size = batch_size if limit is None else min(batch_size, limit - run.rows_read)
                batch = list(
                    RawPart.objects.filter(normalized__isnull=True, id__gt=after_id).order_by("id")[:size]
                )
                if not batch:
                    break
                rows = [_normalize_one(raw) for raw in batch]
                rejected = sum(1 for r in rows if r.status == NormalizedRow.Status.REJECTED)
                with transaction.atomic():
                    NormalizedRow.objects.bulk_create(rows)
                    counts = {
                        "rows_read": run.rows_read + len(batch),
                        "rows_written": run.rows_written + len(rows) - rejected,
                        "rows_rejected": run.rows_rejected + rejected,
                    }
                    PipelineRun.objects.filter(pk=run.pk).update(**counts)
                for name, value in counts.items():
                    setattr(run, name, value)
                after_id = batch[-1].id
            stale = NormalizedRow.objects.filter(version__lt=NORMALIZER_VERSION).count()
            if stale:
                run.notes["stale_rows"] = stale  # produced by an older rule set; see docs/normalization.md
            return finish_run(run, PipelineRun.Status.SUCCEEDED)
        except Exception as exc:
            add_error(run, "normalize", f"{type(exc).__name__}: {exc}")
            return finish_run(run, PipelineRun.Status.FAILED)


def _normalize_one(raw: RawPart) -> NormalizedRow:
    brand = canonical_brand(raw.brand_raw)
    number = normalize_part_number(raw.part_number_raw, brand)
    gtin, gtin_flag = normalize_gtin(raw.gtin_raw)
    rules = list(number.rules) + ([gtin_flag] if gtin_flag else [])
    if not number.ok:
        return NormalizedRow(
            raw=raw,
            supplier_id=raw.supplier_id,
            status=NormalizedRow.Status.REJECTED,
            reject_reason=number.error,
            brand=brand,
            gtin=gtin,
            rules=rules,
            version=NORMALIZER_VERSION,
            match_state=NormalizedRow.MatchState.SKIPPED,
        )
    return NormalizedRow(
        raw=raw,
        supplier_id=raw.supplier_id,
        status=NormalizedRow.Status.OK,
        brand=brand,
        number_norm=number.value,
        gtin=gtin,
        rules=rules,
        version=NORMALIZER_VERSION,
        match_state=NormalizedRow.MatchState.PENDING,
    )
