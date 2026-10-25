"""One batch of the match step (the rule is documented at the top of matching.py)."""
from __future__ import annotations

from collections import defaultdict

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import NormalizedRow, Part, PartSource, ReviewItem


def listing_key(brand: str, number_norm: str) -> str:
    return f"{brand}|{number_norm}"


class _Decision:
    """What the rule decided for one listing in a batch. `row` is the newest row seen for the listing."""

    __slots__ = ("row", "key", "part", "rule", "reason", "candidates")

    def __init__(self, row, key, part=None, rule="", reason="", candidates=()):
        self.row, self.key, self.part, self.rule, self.reason, self.candidates = row, key, part, rule, reason, list(candidates)

    @property
    def queued(self) -> bool:
        return bool(self.reason)


def _core(number: str) -> str:
    return number.lstrip("0")


def _zero_variants(by_brand_core: dict, row: NormalizedRow) -> list[Part]:
    """Parts of the row's brand whose number is the row's number with more or fewer leading zeros."""
    if not row.number_norm.isdigit() or not _core(row.number_norm):
        return []
    return [p for p in by_brand_core.get((row.brand, _core(row.number_norm)), []) if p.number_norm != row.number_norm]


def _same(a: Part | None, b: Part | None) -> bool:
    return a is b or (a is not None and b is not None and a.pk is not None and a.pk == b.pk)


def match_batch(rows: list[NormalizedRow]) -> dict[str, int]:
    """Apply the rule to a batch of pending rows, in id order. The caller wraps it in one transaction."""
    now = timezone.now()
    M = NormalizedRow.MatchState
    stats: dict[str, int] = defaultdict(int)

    wanted = {(r.supplier_id, listing_key(r.brand, r.number_norm)) for r in rows}
    supplier_ids = {k[0] for k in wanted}
    key_names = {k[1] for k in wanted}
    sources = {
        (s.supplier_id, s.listing_key): s
        for s in PartSource.objects.filter(supplier_id__in=supplier_ids, listing_key__in=key_names)
        if (s.supplier_id, s.listing_key) in wanted
    }
    reviews = {
        (r.supplier_id, r.listing_key): r
        for r in ReviewItem.objects.filter(supplier_id__in=supplier_ids, listing_key__in=key_names)
        if (r.supplier_id, r.listing_key) in wanted
    }
    gtins = {r.gtin for r in rows if r.gtin}
    cores = {_core(r.number_norm) for r in rows if r.brand and r.number_norm.isdigit()} - {""}
    found = {p.pk: p for p in Part.objects.filter(number_norm__in={r.number_norm for r in rows})}
    zero_kin = list(Part.objects.filter(number_core__in=cores)) if cores else []
    by_brand_core: dict[tuple[str, str], list[Part]] = defaultdict(list)
    for p in zero_kin:
        if p.number_norm.isdigit():
            by_brand_core[(p.brand, _core(p.number_norm))].append(p)
    if gtins:
        found.update({p.pk: p for p in Part.objects.filter(gtin__in=gtins)})
    by_brand_number: dict[tuple[str, str], Part] = {(p.brand, p.number_norm): p for p in found.values()}
    by_number: dict[str, list[Part]] = defaultdict(list)
    by_gtin: dict[str, list[Part]] = defaultdict(list)
    for p in found.values():
        by_number[p.number_norm].append(p)
        if p.gtin:
            by_gtin[p.gtin].append(p)

    decided: dict[tuple[int, str], _Decision] = {}
    new_parts: list[Part] = []
    gtin_updates: dict[int, Part] = {}
    dirty_sources: dict[int, PartSource] = {}
    dirty_reviews: dict[int, ReviewItem] = {}
    touched: set[int] = set()
    state: dict[int, str] = {}

    for row in rows:
        key = listing_key(row.brand, row.number_norm)
        sk = (row.supplier_id, key)
        if sk in decided:  # the same listing again in this batch: the newer version carries the data
            earlier = decided[sk]
            if row.raw_id > earlier.row.raw_id:
                earlier.row = row
            state[row.pk] = M.REVIEW if earlier.queued else M.MATCHED
            stats["refreshed"] += 1
            continue
        source = sources.get(sk)
        if source is not None:
            if row.raw_id > source.raw_part_id:
                source.raw_part_id = row.raw_id
                source.cost_cents = row.raw.cost_cents
                source.supplier_part_number = row.raw.part_number_raw
                source.last_seen = now
                dirty_sources[source.pk] = source
                touched.add(source.part_id)
            state[row.pk] = M.MATCHED
            stats["refreshed"] += 1
            continue
        review = reviews.get(sk)
        if review is not None:
            if review.status == ReviewItem.Status.PENDING:
                if row.pk > review.norm_row_id:
                    review.norm_row_id = row.pk
                    dirty_reviews[review.pk] = review
                state[row.pk] = M.REVIEW
            else:  # resolved: a link exists if it was linked, and an ignored listing stays ignored
                state[row.pk] = M.SKIPPED
            stats["refreshed"] += 1
            continue

        if not row.brand:
            decided[sk] = _Decision(row, key, reason=ReviewItem.Reason.NO_BRAND, candidates=by_number.get(row.number_norm, []))
            state[row.pk] = M.REVIEW
            continue
        part = by_brand_number.get((row.brand, row.number_norm))
        holders = [p for p in by_gtin.get(row.gtin, []) if not _same(p, part)] if row.gtin else []
        if part is not None and row.gtin and part.gtin and row.gtin != part.gtin:
            decided[sk] = _Decision(row, key, reason=ReviewItem.Reason.GTIN_CONFLICT, candidates=[part])
            state[row.pk] = M.REVIEW
        elif holders:
            decided[sk] = _Decision(row, key, reason=ReviewItem.Reason.GTIN_CROSS, candidates=holders)
            state[row.pk] = M.REVIEW
        elif part is not None:
            if row.gtin and not part.gtin:
                part.gtin = row.gtin
                by_gtin[row.gtin].append(part)
                if part.pk is not None:
                    gtin_updates[part.pk] = part
            decided[sk] = _Decision(row, key, part=part, rule="exact")
            state[row.pk] = M.MATCHED
        elif _zero_variants(by_brand_core, row):
            # the same digits padded with a different number of zeros: one part or two cannot be told from the number alone
            decided[sk] = _Decision(row, key, reason=ReviewItem.Reason.ZERO_VARIANT, candidates=_zero_variants(by_brand_core, row))
            state[row.pk] = M.REVIEW
        else:
            part = Part(
                brand=row.brand,
                number_norm=row.number_norm,
                description=row.raw.description_raw,
                gtin=row.gtin,
                updated_at=now,
            )
            new_parts.append(part)
            by_brand_number[(part.brand, part.number_norm)] = part
            by_number[part.number_norm].append(part)
            if part.number_norm.isdigit():
                by_brand_core[(part.brand, _core(part.number_norm))].append(part)
            if part.gtin:
                by_gtin[part.gtin].append(part)
            decided[sk] = _Decision(row, key, part=part, rule="new")
            state[row.pk] = M.MATCHED

    if new_parts:
        Part.objects.bulk_create(new_parts)
    links = [d for d in decided.values() if not d.queued]
    queue = [d for d in decided.values() if d.queued]
    PartSource.objects.bulk_create(
        [
            PartSource(
                part=d.part,
                supplier_id=d.row.supplier_id,
                listing_key=d.key,
                raw_part_id=d.row.raw_id,
                supplier_part_number=d.row.raw.part_number_raw,
                cost_cents=d.row.raw.cost_cents,
                linked_by=d.rule,
                last_seen=now,
            )
            for d in links
        ]
    )
    ReviewItem.objects.bulk_create(
        [
            ReviewItem(
                supplier_id=d.row.supplier_id,
                listing_key=d.key,
                norm_row=d.row,
                reason=d.reason,
                candidate_part_ids=sorted({p.pk for p in d.candidates}),
            )
            for d in queue
        ]
    )
    touched |= {d.part.pk for d in links}
    if dirty_sources:
        PartSource.objects.bulk_update(
            list(dirty_sources.values()), ["raw_part", "cost_cents", "supplier_part_number", "last_seen"]
        )
    if dirty_reviews:
        ReviewItem.objects.bulk_update(list(dirty_reviews.values()), ["norm_row"])
    if gtin_updates:
        Part.objects.bulk_update(list(gtin_updates.values()), ["gtin"])
    new_ids = {p.pk for p in new_parts}
    if touched - new_ids:
        Part.objects.filter(pk__in=touched - new_ids).update(updated_at=now, search_version=F("search_version") + 1)
    by_state: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        row.match_state = state[row.pk]
        by_state[state[row.pk]].append(row.pk)
    for new_state, ids in by_state.items():
        NormalizedRow.objects.filter(pk__in=ids).update(match_state=new_state)
    stats["created"] = len(new_parts)
    stats["linked"] = len(links)
    stats["review"] = len(queue)
    return stats


def resolve_review(
    item: ReviewItem, *, part: Part | None = None, new_part: bool = False, brand: str = "", ignore: bool = False
) -> ReviewItem:
    """A person's decision on a queued listing: link it to a part, make it a new part, or ignore it."""
    if item.status != ReviewItem.Status.PENDING:
        raise ValueError(f"review item {item.pk} is already {item.status}")
    if sum(bool(x) for x in (part, new_part, ignore)) != 1:
        raise ValueError("choose exactly one of part, new_part, ignore")
    now = timezone.now()
    row = item.norm_row
    with transaction.atomic():
        if ignore:
            item.status = ReviewItem.Status.IGNORED
            row.match_state = NormalizedRow.MatchState.SKIPPED
        else:
            if new_part:
                part_brand = row.brand or brand
                if not part_brand:
                    raise ValueError("this listing has no brand: pass one for the new part")
                part, _ = Part.objects.get_or_create(
                    brand=part_brand,
                    number_norm=row.number_norm,
                    defaults={"description": row.raw.description_raw, "gtin": row.gtin, "updated_at": now},
                )
                item.status = ReviewItem.Status.NEW_PART
            else:
                item.status = ReviewItem.Status.LINKED
            PartSource.objects.create(
                part=part,
                supplier_id=row.supplier_id,
                listing_key=item.listing_key,
                raw_part_id=row.raw_id,
                supplier_part_number=row.raw.part_number_raw,
                cost_cents=row.raw.cost_cents,
                linked_by="review",
                last_seen=now,
            )
            Part.objects.filter(pk=part.pk).update(updated_at=now, search_version=F("search_version") + 1)
            item.resolved_part = part
            row.match_state = NormalizedRow.MatchState.MATCHED
        item.resolved_at = now
        item.save()
        row.save(update_fields=["match_state"])
        if not ignore:
            from .snapshot import apply_snapshot  # a listing a person links is still checked against the supplier's full feed

            apply_snapshot(row.supplier)
    return item
