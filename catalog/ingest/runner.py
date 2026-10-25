"""The ingest runner: restartable, and a second run over the same source changes nothing.

Each batch is one transaction holding the rows it stored, the rejected rows, the run counters and the
checkpoint. A crash can therefore never leave a checkpoint ahead of its rows or rows without their
count. A re-run starts at the checkpoint, and if it were to re-read rows anyway, the unique
idempotency key (one row of one pass) and the "unchanged since the latest stored version" rule keep them from landing twice.
A row is new when its content differs from the NEWEST stored version of the part, so a price that goes 10, 12, 10 is three
versions and the current price is the last one.

For a supplier flagged `full_snapshot`, the batch also records which supplier parts the pass read (rows that
did not change store no RawPart, so nothing else says a part is still listed), and a complete, succeeded pass
ends by delisting what the feed no longer offers (catalog/snapshot.py). What a pass reads is kept as `staging` until the run
completes, so a pass that ends partial leaves the last completed pass (and its seen set) as it was.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from ..models import FeedCheckpoint, PipelineRun, RawPart, RejectedRow, SnapshotSeen, Supplier
from ..runs import add_error, advisory_lock, finish_run, start_run
from ..snapshot import apply_snapshot
from .adapters import FeedAdapter, SourceGap, SourceRecord
from .mapping import (
    MappedRow,
    RowRejected,
    content_hash,
    doubled_columns,
    idempotency_key,
    map_row,
    resolve_headers,
    row_identity,
    unmapped_columns,
)
from .redact import Redactor

# rejections that say nothing about WHICH part the row was: a pass holding one cannot prove what is missing
UNREADABLE = frozenset({"column_count", "undecodable_bytes", "nul_character"})
NUL = chr(0)


class _Batch:
    def __init__(self) -> None:
        self.records: list[SourceRecord] = []
        self.had_gap = False

    def __len__(self) -> int:
        return len(self.records)


def ingest(supplier: Supplier, adapter: FeedAdapter, batch_size: int = 1000, allow_mass_delist: bool = False) -> PipelineRun:
    """Everything this stores or logs about the run passes through ONE Redactor built from the secrets the adapter names
    (`secret_values()`): an exception's text, a gap's message, the adapter's notes and every log record. The source_ref is
    the adapter's own and must hold no secret (identity_url builds one for a URL)."""
    source_ref = adapter.source_ref
    redact = Redactor.for_adapter(adapter)
    with advisory_lock(f"ingest:{supplier.code}:{source_ref}"), redact.log_guard():
        run = start_run(PipelineRun.Kind.INGEST, supplier, source_ref)
        try:
            return _ingest_locked(supplier, adapter, run, batch_size, allow_mass_delist, redact)
        except Exception as exc:  # the batch in flight was rolled back; what was committed before stays
            add_error(run, "ingest", redact.text(f"{type(exc).__name__}: {exc}"))
            return finish_run(run, PipelineRun.Status.FAILED)


def _ingest_locked(
    supplier: Supplier, adapter: FeedAdapter, run: PipelineRun, batch_size: int, allow_mass_delist: bool, redact: Redactor
) -> PipelineRun:
    checkpoint, _ = FeedCheckpoint.objects.get_or_create(supplier=supplier, source_ref=adapter.source_ref)
    fingerprint = adapter.fingerprint()
    run.notes["fingerprint"] = fingerprint
    # a source whose order can change between two runs (a live site) is never resumed by position: row N of the earlier run
    # is not row N now, and skipping N rows would skip rows the site still offers (see ScrapeAdapter.resumable)
    restart = not getattr(adapter, "resumable", True)
    if restart or checkpoint.fingerprint != fingerprint or (checkpoint.completed and not fingerprint):
        # new content (or a live source, which has no identity to compare): read it from the start
        checkpoint.fingerprint = fingerprint
        checkpoint.pass_id = run.pk  # a pass is told apart from every other by the run that began it
        checkpoint.position = 0
        checkpoint.completed = False
        checkpoint.save()  # completed_at stays: it dates the last COMPLETED pass, whose seen set stays what the match step reads
        SnapshotSeen.objects.filter(checkpoint=checkpoint, staging=True).delete()  # a new pass starts with nothing seen
    elif checkpoint.completed:
        run.notes["skipped"] = "source unchanged since the last complete run"
        return finish_run(run, PipelineRun.Status.SUCCEEDED)
    start = checkpoint.position
    if start:
        run.notes["resumed_from"] = start

    headers: dict[str, None] = {}  # an ordered set: of two columns for one field, the left one comes first
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
            add_error(run, f"positions {item.first_position}-{item.last_position}", redact.text(item.message))
            continue
        batch.records.append(item)
        if item.fields:
            headers.update(dict.fromkeys(item.fields))
        if len(batch) >= batch_size:
            flush()
    flush()

    run.notes.update(redact.data(adapter.notes()))
    checkpoint.refresh_from_db()
    if gap_seen:
        return finish_run(run, PipelineRun.Status.PARTIAL)
    with transaction.atomic():  # the pass becomes the latest completed one, with its own seen set, in one step
        SnapshotSeen.objects.filter(checkpoint=checkpoint, staging=False).delete()
        SnapshotSeen.objects.filter(checkpoint=checkpoint, staging=True).update(staging=False)
        checkpoint.completed = True
        checkpoint.completed_at = timezone.now()
        checkpoint.last_run = run
        checkpoint.save()
    if supplier.full_snapshot:
        # a complete, succeeded pass over a snapshot feed: whatever it did not list is no longer offered
        run.notes["snapshot"] = apply_snapshot(supplier, allow_mass_delist=allow_mass_delist)
    return finish_run(run, PipelineRun.Status.SUCCEEDED)


def _store_batch(
    supplier: Supplier,
    run: PipelineRun,
    checkpoint: FeedCheckpoint,
    records: list[SourceRecord],
    headers: dict[str, None],
    advance_to: int | None,
) -> None:
    mapping = resolve_headers(headers) if headers else {}
    if headers:
        run.notes["unmapped_columns"] = unmapped_columns(headers, mapping)
        if doubled_columns(mapping):
            run.notes["doubled_columns"] = doubled_columns(mapping)
    mapped: list[tuple[SourceRecord, MappedRow, str]] = []
    rejected: list[RejectedRow] = []
    seen: set[tuple[str, str]] = set()
    for rec in records:
        reason = rec.reject
        row = None
        # a NUL cannot be stored in a text or JSON column: that row is refused, never the whole run
        if not reason and any(NUL in value for value in rec.fields.values()):
            reason = "nul_character"
        if not reason:
            try:
                row = map_row(rec.fields, mapping)
            except RowRejected as exc:
                reason = exc.reason
        if reason:
            rejected.append(
                RejectedRow(run=run, supplier=supplier, position=rec.position, reason=reason, raw=rec.raw_text.replace(NUL, "�"))
            )
            if reason in UNREADABLE:
                seen.add(("", ""))
            elif (identity := row_identity(rec.fields, mapping)) is not None:
                seen.add(identity)
        else:
            mapped.append((rec, row, content_hash(row)))
            seen.add((row.part_number, row.brand))
            for name, header in row.picked.items():
                counts = run.notes.setdefault("column_choices", {}).setdefault(name, {})
                counts[header] = counts.get(header, 0) + 1

    part_numbers = {row.part_number for _, row, _ in mapped}
    keys = {
        idempotency_key(supplier.code, run.source_ref, rec.position, chash, checkpoint.pass_id): None for rec, _, chash in mapped
    }
    with transaction.atomic():
        seen_keys = set(RawPart.objects.filter(idempotency_key__in=list(keys)).values_list("idempotency_key", flat=True))
        latest = {
            (number, brand): chash
            for number, brand, chash in RawPart.objects.filter(supplier=supplier, part_number_raw__in=part_numbers)
            .order_by("part_number_raw", "brand_raw", "-id")
            .distinct("part_number_raw", "brand_raw")
            .values_list("part_number_raw", "brand_raw", "content_hash")
        }
        to_insert: list[RawPart] = []
        unchanged = 0
        for rec, row, chash in mapped:
            key = idempotency_key(supplier.code, run.source_ref, rec.position, chash, checkpoint.pass_id)
            if key in seen_keys or latest.get((row.part_number, row.brand)) == chash:
                unchanged += 1
                continue
            latest[(row.part_number, row.brand)] = chash
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
        if supplier.full_snapshot and seen:
            SnapshotSeen.objects.bulk_create(
                [SnapshotSeen(checkpoint=checkpoint, staging=True, part_number_raw=n, brand_raw=b) for n, b in seen],
                ignore_conflicts=True,
            )
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
