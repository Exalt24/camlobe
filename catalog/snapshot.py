"""Delisting: a part a supplier drops from its next full feed stops being offered.

A supplier flagged `full_snapshot` sends its whole catalog every time. After a complete, succeeded pass over
such a feed, every listing of that supplier that the pass did not read gets `delisted_at`; a delisted listing
that the next complete pass reads again is relisted. Search documents, the lowest cost, the REST API and
purchase orders ignore delisted listings.

What counts as "the pass read it" comes from `SnapshotSeen`, because a row that did not change stores no raw
row. The supplier's snapshot is the latest completed pass over any of its sources. Two simultaneous full feeds
of one supplier (brakes in one file, filters in another) are two halves of a catalog, not two snapshots: such a
supplier must not be flagged.

Three things keep a bad file from emptying a catalog:
  - a partial or failed run never delists (only a completed pass is looked at);
  - a pass that rejected a row it could not read (column count, undecodable bytes, a NUL) delists nothing,
    since the unreadable row might have been any part;
  - a pass that would delist more than half of a supplier's listings (when it has at least ten) delists nothing
    unless the operator passes --allow-mass-delist, since a truncated file reads cleanly.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import FeedCheckpoint, Part, PartSource, SnapshotSeen, Supplier
from .normalize import canonical_brand, normalize_part_number

MASS_DELIST_MIN_LISTINGS = 10
MASS_DELIST_FRACTION = 0.5
CHUNK = 1000


def latest_pass(supplier: Supplier) -> FeedCheckpoint | None:
    return (
        FeedCheckpoint.objects.filter(supplier=supplier, completed=True, completed_at__isnull=False)
        .order_by("-completed_at", "-id")
        .first()
    )


def seen_listing_keys(checkpoint: FeedCheckpoint) -> tuple[set[str], bool]:
    """(the listing keys the pass read, whether the pass held a row it could not read)."""
    from .matching_batch import listing_key

    keys: set[str] = set()
    unreadable = False
    for number, brand in SnapshotSeen.objects.filter(checkpoint=checkpoint).values_list("part_number_raw", "brand_raw"):
        if not number:
            unreadable = True
            continue
        canon = canonical_brand(brand)
        result = normalize_part_number(number, canon)
        if result.ok:
            keys.add(listing_key(canon, result.value))
    return keys, unreadable


def _bump(part_ids: set[int], now) -> None:
    for start in range(0, len(part_ids), CHUNK):
        chunk = list(part_ids)[start : start + CHUNK]
        Part.objects.filter(pk__in=chunk).update(updated_at=now, search_version=F("search_version") + 1)


def apply_snapshot(supplier: Supplier, allow_mass_delist: bool = False) -> dict:
    """Bring the supplier's listings in line with its latest complete snapshot. Idempotent. Returns what it did."""
    if not supplier.full_snapshot:
        return {}
    checkpoint = latest_pass(supplier)
    if checkpoint is None:
        return {}
    keys, unreadable = seen_listing_keys(checkpoint)
    now = timezone.now()
    result: dict = {"pass_source": checkpoint.source_ref, "listed_in_pass": len(keys), "delisted": 0, "relisted": 0}
    with transaction.atomic():
        gone = list(PartSource.objects.filter(supplier=supplier, delisted_at__isnull=False).values_list("pk", "listing_key", "part_id"))
        back = [(pk, part_id) for pk, key, part_id in gone if key in keys]
        for start in range(0, len(back), CHUNK):
            PartSource.objects.filter(pk__in=[pk for pk, _ in back[start : start + CHUNK]]).update(delisted_at=None)
        result["relisted"] = len(back)
        touched = {part_id for _, part_id in back}

        active = list(PartSource.objects.filter(supplier=supplier, delisted_at__isnull=True).values_list("pk", "listing_key", "part_id"))
        missing = [(pk, part_id) for pk, key, part_id in active if key not in keys]
        refusal = ""
        if missing and unreadable:
            refusal = "the pass rejected a row it could not read, so it cannot prove what is missing"
        elif missing and not keys:
            refusal = "the pass listed no part at all"
        elif (
            missing
            and not allow_mass_delist
            and len(active) >= MASS_DELIST_MIN_LISTINGS
            and len(missing) > MASS_DELIST_FRACTION * len(active)
        ):
            refusal = f"{len(missing)} of {len(active)} listings would be delisted, more than half: a truncated file reads cleanly"
        if refusal:
            result["delist_refused"] = refusal
        else:
            for start in range(0, len(missing), CHUNK):
                PartSource.objects.filter(pk__in=[pk for pk, _ in missing[start : start + CHUNK]]).update(delisted_at=now)
            result["delisted"] = len(missing)
            touched |= {part_id for _, part_id in missing}
        if touched:
            _bump(touched, now)
    return result
