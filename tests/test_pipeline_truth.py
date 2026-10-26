"""The generated messy feeds, run through the whole pipeline and checked against the generator's truth file."""
import csv
from collections import defaultdict
from pathlib import Path

import pytest

from catalog.demo.generator import write_feeds
from catalog.ingest.adapters import FileAdapter
from catalog.ingest.runner import ingest
from catalog.matching import match_pending
from catalog.models import Part, PartSource, PipelineRun, RejectedRow, ReviewItem, Supplier
from catalog.normalize_step import normalize_pending

pytestmark = pytest.mark.django_db(transaction=True)

SIZE = 2500


def load_truth(folder: Path):
    truth = defaultdict(set)
    with (folder / "truth.csv").open(encoding="utf-8") as handle:
        for r in csv.DictReader(handle):
            truth[(r["supplier"], r["part_number"], r["brand"])].add((r["canon_brand"], r["canon_number"]))
    return truth


def run_pipeline(folder: Path, order):
    runs = {}
    for code, name in order:
        sup, _ = Supplier.objects.get_or_create(code=code, defaults={"name": code})
        runs[code] = ingest(sup, FileAdapter(folder / name))
    normalize_pending()
    match_pending()
    return runs


ORDER_A = [("redline", "redline_catalog.csv"), ("harbor", "harbor_catalog.xml"), ("delta", "delta_2026-10-01.csv")]
ORDER_B = list(reversed(ORDER_A))


@pytest.fixture(scope="module")
def feeds(tmp_path_factory):
    folder = tmp_path_factory.mktemp("feeds")
    write_feeds(folder, size=SIZE, seed=11)
    return folder


def test_the_feeds_are_messy_in_the_ways_described(feeds):
    redline = (feeds / "redline_catalog.csv").read_bytes()
    assert b"\x81" in redline  # an undefined windows-1252 byte
    assert b"\xc9" in redline  # a latin-1 capital E acute (as a single byte)
    assert any(b > 0x7F for b in (feeds / "delta_2026-10-01.csv").read_bytes()[:3])  # the BOM
    assert b"<manufacturer>" in (feeds / "harbor_catalog.xml").read_bytes()
    header = lambda n: (feeds / n).read_text(encoding="utf-8-sig").splitlines()[0]
    assert header("delta_2026-10-01.csv") != header("delta_2026-10-15.csv") != header("delta_2026-10-22.csv")


def test_no_part_is_merged_wrongly_and_none_is_split(feeds):
    truth = load_truth(feeds)
    runs = run_pipeline(feeds, ORDER_A)
    assert all(r.status == PipelineRun.Status.SUCCEEDED for r in runs.values())
    by_part = defaultdict(set)
    by_canon = defaultdict(set)
    checked = 0
    for ps in PartSource.objects.select_related("part", "supplier", "raw_part"):
        canon = truth[(ps.supplier.code, ps.raw_part.part_number_raw, ps.raw_part.brand_raw)]
        assert len(canon) == 1, "the generator wrote the same raw text for two different parts"
        (c,) = canon
        by_part[ps.part_id].add(c)
        by_canon[c].add(ps.part_id)
        assert (ps.part.brand, ps.part.number_norm) == c  # linked to the right part
        checked += 1
    assert checked > 4000
    assert all(len(v) == 1 for v in by_part.values())  # one part never holds two real parts
    assert all(len(v) == 1 for v in by_canon.values())  # one real part is never two parts
    cross_suppliers = Part.objects.filter(sources__supplier__code="harbor").filter(sources__supplier__code="redline").distinct().count()
    assert cross_suppliers > 500  # most parts really were matched across suppliers


def test_the_parts_do_not_depend_on_the_order_the_feeds_arrive_in(feeds):
    run_pipeline(feeds, ORDER_A)
    first = set(Part.objects.values_list("brand", "number_norm"))
    sources_a = PartSource.objects.count()
    from django.core.management import call_command

    call_command("flush", interactive=False)
    run_pipeline(feeds, ORDER_B)
    assert set(Part.objects.values_list("brand", "number_norm")) == first
    assert PartSource.objects.count() == sources_a


def test_messy_rows_are_rejected_with_reasons_and_the_review_queue_has_the_ambiguous_ones(feeds):
    run_pipeline(feeds, ORDER_A)
    reasons = set(RejectedRow.objects.values_list("reason", flat=True))
    assert reasons == {"blank_part_number", "bad_cost", "undecodable_bytes", "duplicate_part"}  # the repeated row with a new price
    queue = defaultdict(int)
    for reason in ReviewItem.objects.values_list("reason", flat=True):
        queue[reason] += 1
    assert queue["no_brand"] > 50 and queue["gtin_cross"] >= 1 and queue["gtin_conflict"] >= 1
    # nothing queued was also linked: compare the exact (supplier, listing) pairs
    queued = set(ReviewItem.objects.values_list("supplier_id", "listing_key"))
    linked = set(PartSource.objects.values_list("supplier_id", "listing_key"))
    assert queued and not (queued & linked)


def test_duplicates_collapse_and_a_rerun_of_every_stage_changes_nothing(feeds):
    runs = run_pipeline(feeds, ORDER_A)
    assert runs["redline"].rows_unchanged > 20  # exact duplicate rows were not stored twice
    counts = (Part.objects.count(), PartSource.objects.count(), ReviewItem.objects.count())
    again = run_pipeline(feeds, ORDER_A)
    assert all(r.rows_written == 0 and "skipped" in r.notes for r in again.values())
    assert (Part.objects.count(), PartSource.objects.count(), ReviewItem.objects.count()) == counts


def test_the_delta_files_in_a_month_with_a_reorder_a_rename_and_a_break(feeds):
    sup, _ = Supplier.objects.get_or_create(code="delta", defaults={"name": "delta"})
    first = ingest(sup, FileAdapter(feeds / "delta_2026-10-01.csv"))
    reordered = ingest(sup, FileAdapter(feeds / "delta_2026-10-15.csv"))
    renamed = ingest(sup, FileAdapter(feeds / "delta_2026-10-22.csv"))
    broken = ingest(sup, FileAdapter(feeds / "delta_2026-10-29_unmapped.csv"))
    assert first.rows_written > 1000
    # the second file differs from the first in about 5 percent of its costs, and only those are stored
    assert 0.02 * first.rows_written < reordered.rows_written < 0.1 * first.rows_written
    assert renamed.rows_written > 0 and renamed.rows_unchanged > 0.8 * renamed.rows_read
    assert broken.status == PipelineRun.Status.FAILED and "producer" in broken.errors[0]["message"]
    assert broken.rows_written == 0
