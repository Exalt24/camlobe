"""The match step: group the same part across suppliers, and queue every case the rules will not decide.

THE RULE (also in docs/matching.md). A normalized row is a supplier listing with a canonical brand,
a canonical number and maybe a GTIN. In this order:

  1. The supplier's listing is already linked: refresh its newest version and cost. Done.
  2. The listing is in the review queue: refresh the queued row, or skip it if it was ignored. Done.
  3. No brand: REVIEW (no_brand), candidates are the parts that carry the number. Never guessed.
  4. A part with the same brand and number exists:
       - both sides have a GTIN and they differ: REVIEW (gtin_conflict).
       - the row's GTIN is held by a different part: REVIEW (gtin_cross).
       - otherwise LINK to it (rule "exact").
  5. No such part:
       - the row's GTIN is held by another part (a typo, a renumbering or a rebrand): REVIEW (gtin_cross).
       - otherwise CREATE the part (rule "new") and link.

Same number under a different brand is NOT a match: two manufacturers use the same number all the time.
The rows of a batch are decided in id order, so a run is deterministic. Everything a batch does is one
transaction, and the queue is "pending" rows, so a killed run resumes and a second run finds nothing.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .matching_batch import listing_key, match_batch, resolve_review
from .models import NormalizedRow, PipelineRun
from .runs import add_error, advisory_lock, finish_run, start_run


def match_pending(batch_size: int = 2000, limit: int | None = None) -> PipelineRun:
    with advisory_lock("match"):
        run = start_run(PipelineRun.Kind.MATCH)
        try:
            after_id = 0
            while limit is None or run.rows_read < limit:
                size = batch_size if limit is None else min(batch_size, limit - run.rows_read)
                rows = list(
                    NormalizedRow.objects.filter(
                        match_state=NormalizedRow.MatchState.PENDING, status=NormalizedRow.Status.OK, id__gt=after_id
                    )
                    .select_related("raw")
                    .order_by("id")[:size]
                )
                if not rows:
                    break
                with transaction.atomic():
                    stats = match_batch(rows)
                    counts = {
                        "rows_read": run.rows_read + len(rows),
                        "rows_written": run.rows_written + stats["linked"],
                        "rows_unchanged": run.rows_unchanged + stats["refreshed"],
                        "rows_rejected": run.rows_rejected + stats["review"],
                    }
                    PipelineRun.objects.filter(pk=run.pk).update(**counts)
                for name, value in counts.items():
                    setattr(run, name, value)
                for key in ("created", "review"):
                    run.notes[key] = run.notes.get(key, 0) + stats[key]
                after_id = rows[-1].id
            return finish_run(run, PipelineRun.Status.SUCCEEDED)
        except Exception as exc:
            add_error(run, "match", f"{type(exc).__name__}: {exc}")
            return finish_run(run, PipelineRun.Status.FAILED)




__all__ = ["listing_key", "match_batch", "match_pending", "resolve_review"]
