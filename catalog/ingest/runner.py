"""The ingest runner: restartable, and a second run over the same source changes nothing.

Each batch is one transaction holding the rows it stored, the rejected rows, the run counters and the
checkpoint. A crash can therefore never leave a checkpoint ahead of its rows or rows without their
count. A re-run starts at the checkpoint, and if it were to re-read rows anyway, the unique
idempotency key and the "unchanged since the latest stored version" rule keep them from landing twice.
"""
from __future__ import annotations

from django.db import transaction

from ..models import FeedCheckpoint, PipelineRun, RawPart, RejectedRow, Supplier
from ..runs import add_error, advisory_lock, finish_run, start_run
from .adapters import FeedAdapter, SourceGap, SourceRecord
from .mapping import (
    MappedRow,
    RowRejected,
    SchemaDriftError,
    content_hash,
    idempotency_key,
    map_row,
    resolve_headers,
    unmapped_columns,
)


class _Batch:
    def __init__(self) -> None:
        self.records: list[SourceRecord] = []
        self.had_gap = False

    def __len__(self) -> int:
        return len(self.records)


def ingest(supplier: Supplier, adapter: FeedAdapter, batch_size: int = 1000) -> PipelineRun:
    source_ref = adapter.source_ref
    with advisory_lock(f"ingest:{supplier.code}:{source_ref}"):
        run = start_run(PipelineRun.Kind.INGEST, supplier, source_ref)
        try:
            return _ingest_locked(supplier, adapter, run, batch_size)
        except Exception as exc:  # the batch in flight was rolled back; what was committed before stays
            add_error(run, "ingest", f"{type(exc).__name__}: {exc}")
            return finish_run(run, PipelineRun.Status.FAILED)


def _ingest_locked(supplier: Supplier, adapter: FeedAdapter, run: PipelineRun, batch_size: int) -> PipelineRun:
    checkpoint, _ = FeedCheckpoint.objects.get_or_create(supplier=supplier, source_ref=adapter.source_ref)
    fingerprint = adapter.fingerprint()
    run.notes["fingerprint"] = fingerprint
    if checkpoint.fingerprint != fingerprint or (checkpoint.completed and not fingerprint):
        # new content (or a live source, which has no identity to compare): read it from the start
        checkpoint.fingerprint = fingerprint
        checkpoint.position = 0
        checkpoint.completed = False
        checkpoint.save()
    elif checkpoint.completed:
        run.notes["skipped"] = "source unchanged since the last complete run"
        return finish_run(run, PipelineRun.Status.SUCCEEDED)
    start = checkpoint.position
    if start:
        run.notes["resumed_from"] = start

    headers: set[str] = set()
    contiguous = True
    gap_seen = False
    batch = _Batch()

    def flush() -> None:
        if not batch.records:
            return
        advance = contiguous and not batch.had_gap
        last = batch.records[-1].position
        _store_batch(supplier, run, checkpoint, batch.records, headers, advance_to=last if advance else None)
        batch.records.clear()
        batch.had_gap = False

    for item in adapter.records(start_after=start):
        if isinstance(item, SourceGap):
            flush()  # what was read before the gap is whole, so it moves the checkpoint
            contiguous = False
            gap_seen = True
            batch.had_gap = True
            add_error(run, f"positions {item.first_position}-{item.last_position}", item.message)
            continue
        batch.records.append(item)
        if item.fields:
            headers.update(item.fields)
        if len(batch) >= batch_size:
            flush()
    flush()

    run.notes.update(adapter.notes())
    checkpoint.refresh_from_db()
    if gap_seen:
        return finish_run(run, PipelineRun.Status.PARTIAL)
    checkpoint.completed = True
    checkpoint.last_run = run
    checkpoint.save()
    return finish_run(run, PipelineRun.Status.SUCCEEDED)


def _store_batch(
    supplier: Supplier,
    run: PipelineRun,
    checkpoint: FeedCheckpoint,
    records: list[SourceRecord],
    headers: set[str],
    advance_to: int | None,
) -> None:
    mapping = resolve_headers(headers) if headers else {}
    if headers:
        run.notes["unmapped_columns"] = unmapped_columns(headers, mapping)
    mapped: list[tuple[SourceRecord, MappedRow, str]] = []
    rejected: list[RejectedRow] = []
    for rec in records:
        reason = rec.reject
        row = None
        if not reason:
            try:
                row = map_row(rec.fields, mapping)
            except RowRejected as exc:
                reason = exc.reason
        if reason:
            rejected.append(RejectedRow(run=run, supplier=supplier, position=rec.position, reason=reason, raw=rec.raw_text))
        else:
            mapped.append((rec, row, content_hash(row)))

    part_numbers = {row.part_number for _, row, _ in mapped}
    keys = {
        idempotency_key(supplier.code, run.source_ref, rec.position, chash): None for rec, _, chash in mapped
    }
    with transaction.atomic():
        seen_keys = set(RawPart.objects.filter(idempotency_key__in=list(keys)).values_list("idempotency_key", flat=True))
        latest = dict(
            RawPart.objects.filter(supplier=supplier, part_number_raw__in=part_numbers)
            .order_by("part_number_raw", "-id")
            .distinct("part_number_raw")
            .values_list("part_number_raw", "content_hash")
        )
        to_insert: list[RawPart] = []
        unchanged = 0
        for rec, row, chash in mapped:
            key = idempotency_key(supplier.code, run.source_ref, rec.position, chash)
            if key in seen_keys or latest.get(row.part_number) == chash:
                unchanged += 1
                continue
            latest[row.part_number] = chash
            to_insert.append(
                RawPart(
                    supplier=supplier,
                    run=run,
                    position=rec.position,
                    idempotency_key=key,
                    content_hash=chash,
                    part_number_raw=row.part_number,
                    brand_raw=row.brand,
                    description_raw=row.description,
                    gtin_raw=row.gtin,
                    cost_cents=row.cost_cents,
                    payload=rec.fields,
                )
            )
        RawPart.objects.bulk_create(to_insert)
        RejectedRow.objects.bulk_create(rejected)
        counts = {
            "rows_read": run.rows_read + len(records),
            "rows_written": run.rows_written + len(to_insert),
            "rows_unchanged": run.rows_unchanged + unchanged,
            "rows_rejected": run.rows_rejected + len(rejected),
        }
        PipelineRun.objects.filter(pk=run.pk).update(**counts)
        if advance_to is not None:
            FeedCheckpoint.objects.filter(pk=checkpoint.pk).update(position=advance_to, last_run=run)
    # only after the commit: a rolled back batch must not leave its counts on the in-memory run
    for name, value in counts.items():
        setattr(run, name, value)
