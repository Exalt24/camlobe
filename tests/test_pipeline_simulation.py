"""Seeded random worlds: crashes and re-runs must never change where the pipeline ends up.

Every world is a sequence of phases. A phase ingests one supplier file, then normalizes and matches.
The same world is run twice from an empty database: once cleanly, and once with random failures in
every stage (a feed that drops mid file, a normalize or match batch that dies, a stage re-run for no
reason, a part run with a small limit). The two end states must be the same, compared by content and
not by row id. The worlds mix the things bugs hide between: one part written many ways, the same
number under two brands, GTINs that agree, disagree and are shared by a different number, missing
brands, a listing that changes price, repeats and files sent again.
"""
import csv
import random
from unittest import mock

import pytest
from django.core.management import call_command

from catalog.ingest.adapters import FileAdapter
from catalog.ingest.runner import ingest
from catalog.matching import match_pending
from catalog.models import NormalizedRow, Part, PartSource, PipelineRun, RawPart, ReviewItem, Supplier
from catalog.normalize import BRAND_PREFIXES
from catalog.normalize_step import normalize_pending

from .helpers import write_csv

pytestmark = pytest.mark.django_db(transaction=True)

SEEDS = list(range(24))
BRANDS = ["Marlow", "Halden", "Dunmore", "Tarn"]


def gtin_for(n: int) -> str:
    body = f"{4000000000000 + n * 104729:012d}"[:12]
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return body + str((10 - total % 10) % 10)


def make_world(seed: int):
    rng = random.Random(seed)
    universe = []
    for i in range(30):
        brand = rng.choice(BRANDS[:3]) if i % 5 else BRANDS[i % 4]
        number = rng.choice(["H", "ZR", "K", "T"]) + f"{rng.randint(100, 999)}" + rng.choice(["", "A", "B"])
        universe.append({"brand": brand, "number": number, "gtin": gtin_for(i) if rng.random() < 0.6 else ""})
    # the same number under two brands, and a part that shares another part's GTIN
    universe.append({**universe[0], "brand": BRANDS[3], "gtin": ""})
    universe.append({**universe[1], "number": universe[1]["number"] + "R"})
    phases = []
    for p in range(rng.randint(3, 5)):
        supplier = rng.choice(["s1", "s2", "s3"])
        rows = []
        for part in rng.sample(universe, k=rng.randint(10, 24)):
            number = part["number"]
            if rng.random() < 0.4:
                number = f"{rng.choice(BRAND_PREFIXES[part['brand'].upper()])}-{number[:2]} {number[2:]}"
            brand = part["brand"] if rng.random() > 0.12 else ""
            gtin = part["gtin"]
            r = rng.random()
            if r < 0.06:
                gtin = gtin_for(rng.randint(100, 200))  # disagrees
            elif r < 0.08:
                gtin = "1234567890123"  # invalid
            cost = f"{rng.randint(1, 30)}.{rng.choice(['00', '50', '99'])}"
            rows.append([number, brand, "Part", cost, gtin])
            if rng.random() < 0.15:
                rows.append(list(rows[-1]))  # an exact duplicate
            if rng.random() < 0.1:
                rows.append([number, brand, "Part", "77.00", gtin])  # the same listing, new price
        phases.append((supplier, f"file{p}.csv", rows))
    return phases


def snapshot():
    def key(part_id):
        part = Part.objects.get(pk=part_id)
        return f"{part.brand}|{part.number_norm}"

    return {
        "parts": sorted(Part.objects.values_list("brand", "number_norm", "gtin", "description")),
        "sources": sorted(
            (ps.supplier.code, ps.listing_key, key(ps.part_id), ps.cost_cents, ps.supplier_part_number, ps.linked_by)
            for ps in PartSource.objects.select_related("supplier")
        ),
        "reviews": sorted(
            (r.supplier.code, r.listing_key, r.reason, r.status, tuple(sorted(key(i) for i in r.candidate_part_ids)))
            for r in ReviewItem.objects.select_related("supplier")
        ),
        "raw": sorted(RawPart.objects.values_list("supplier__code", "part_number_raw", "content_hash", "position")),
        "states": sorted(NormalizedRow.objects.values_list("raw__position", "raw__supplier__code", "match_state", "number_norm")),
    }


class Dying(FileAdapter):
    def __init__(self, path, after, source_ref):
        super().__init__(path, source_ref=source_ref)
        self.after = after

    def records(self, start_after=0):
        for n, rec in enumerate(super().records(start_after)):
            if n >= self.after:
                raise ConnectionError("dropped")
            yield rec


def run_world(phases, tmp_path, rng):
    """rng is None for the clean run."""
    for supplier_code, name, rows in phases:
        supplier, _ = Supplier.objects.get_or_create(code=supplier_code, defaults={"name": supplier_code})
        path = write_csv(tmp_path / f"{supplier_code}_{name}", rows)
        batch = 7
        if rng is not None:
            for _ in range(rng.randint(0, 2)):
                ingest(supplier, Dying(path, rng.randint(0, len(rows) + 3), path.name), batch_size=batch)
        ingest(supplier, FileAdapter(path), batch_size=batch)
        if rng is not None and rng.random() < 0.5:
            ingest(supplier, FileAdapter(path), batch_size=batch)  # sent again
        stage(rng, normalize_pending, "catalog.normalize_step.NormalizedRow.objects", "bulk_create")
        stage(rng, match_pending, "catalog.matching.match_batch", None)


def stage(rng, fn, target, attr):
    """Run a stage to completion, with random deaths and limits in front of it when rng is given."""
    if rng is not None:
        for _ in range(rng.randint(0, 2)):
            fail_at = rng.randint(1, 3)
            counter = {"n": 0}
            if attr:
                owner = NormalizedRow.objects
                real = getattr(owner, attr)

                def boom(*a, _real=real, **k):
                    counter["n"] += 1
                    if counter["n"] == fail_at:
                        raise RuntimeError("killed")
                    return _real(*a, **k)

                with mock.patch.object(owner, attr, side_effect=boom):
                    fn(batch_size=5)
            else:
                from catalog import matching

                real_batch = matching.match_batch

                def boom_batch(rows, _real=real_batch):
                    counter["n"] += 1
                    if counter["n"] == fail_at:
                        raise RuntimeError("killed")
                    return _real(rows)

                with mock.patch.object(matching, "match_batch", side_effect=boom_batch):
                    fn(batch_size=5)
        if rng.random() < 0.5:
            fn(batch_size=5, limit=rng.randint(1, 12))
    fn(batch_size=5)
    fn(batch_size=5)  # a stage run again after it finished must find nothing


@pytest.mark.parametrize("seed", SEEDS)
def test_crashes_and_reruns_end_where_a_clean_run_ends(seed, tmp_path):
    phases = make_world(seed)
    (tmp_path / "clean").mkdir()
    (tmp_path / "chaos").mkdir()
    run_world(phases, tmp_path / "clean", None)
    clean = snapshot()
    call_command("flush", interactive=False)
    run_world(phases, tmp_path / "chaos", random.Random(seed * 31 + 5))
    chaos = snapshot()
    for name in clean:
        assert chaos[name] == clean[name], f"seed {seed}: {name} differs"
    assert clean["parts"], "the world produced nothing"


def test_the_worlds_reach_every_branch_of_the_rule(tmp_path):
    """A simulation that never reaches a branch proves nothing about it, so count what the worlds hit."""
    seen = set()
    for seed in SEEDS[:12]:
        call_command("flush", interactive=False)
        folder = tmp_path / f"w{seed}"
        folder.mkdir()
        run_world(make_world(seed), folder, None)
        seen |= set(ReviewItem.objects.values_list("reason", flat=True))
        seen |= set(PartSource.objects.values_list("linked_by", flat=True))
        if RawPart.objects.values("supplier", "part_number_raw").distinct().count() < RawPart.objects.count():
            seen.add("listing_has_versions")
    assert {"no_brand", "gtin_conflict", "gtin_cross", "exact", "new", "listing_has_versions"} <= seen


def test_the_chaos_really_injects_failures_into_every_stage(tmp_path):
    failed_kinds = set()
    for seed in range(8):
        call_command("flush", interactive=False)
        folder = tmp_path / f"c{seed}"
        folder.mkdir()
        run_world(make_world(seed), folder, random.Random(seed * 31 + 5))
        failed_kinds |= set(PipelineRun.objects.filter(status="failed").values_list("kind", flat=True))
    assert failed_kinds == {"ingest", "normalize", "match"}
