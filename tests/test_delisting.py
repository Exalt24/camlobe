"""A part a supplier drops from its next full feed stops being offered: search, lowest cost, the API and purchase orders."""
import datetime as dt

import pytest
from django.test import Client

from catalog.edi.orders import OrderError, purchase_order_for
from catalog.ingest.adapters import FileAdapter, SourceGap, SourceRecord
from catalog.ingest.paged import PagedAdapter, PermanentError
from catalog.ingest.runner import ingest
from catalog.matching import match_pending
from catalog.models import Part, PartSource, PipelineRun, SnapshotSeen
from catalog.normalize_step import normalize_pending
from catalog.search import index
from catalog.search.documents import documents_for
from catalog.snapshot import apply_snapshot

from .helpers import get_supplier, row, run_all, write_csv

pytestmark = pytest.mark.django_db(transaction=True)
TODAY = dt.date(2026, 10, 26)

X = row("H4001A", "Marlow", "Pump", "10.00")
Y = row("H4002A", "Marlow", "Hose", "5.00")


def snapshot_supplier(code="s1", flag=True):
    supplier = get_supplier(code)
    supplier.full_snapshot = flag
    supplier.save()
    return supplier


def part(number):
    return Part.objects.get(number_norm=number)


def listing(number, supplier):
    return PartSource.objects.get(part=part(number), supplier=supplier)


def day(tmp_path, name, rows):
    return write_csv(tmp_path / name, rows)


def test_a_part_dropped_from_the_next_full_feed_is_delisted_and_leaves_search_cost_and_orders(tmp_path, db_search):
    s = snapshot_supplier()
    run_all(s, day(tmp_path, "day1.csv", [X, Y]))
    assert listing("H4002A", s).delisted_at is None
    before = part("H4002A").search_version
    run = run_all(s, day(tmp_path, "day2.csv", [row("H4001A", "Marlow", "Pump", "11.00")]))
    assert run.status == PipelineRun.Status.SUCCEEDED and run.notes["snapshot"]["delisted"] == 1
    assert listing("H4002A", s).delisted_at is not None and listing("H4001A", s).delisted_at is None
    assert part("H4002A").search_version == before + 1  # the index sync will pick the change up

    docs = {d["number"]: d for d in documents_for(Part.objects.all())}
    assert (docs["H4001A"]["suppliers"], docs["H4001A"]["lowest_cost_cents"]) == (["s1"], 1100)
    assert (docs["H4002A"]["suppliers"], docs["H4002A"]["supplier_count"], docs["H4002A"]["lowest_cost_cents"]) == ([], 0, None)

    found = Client().get("/api/parts/search/", {"q": "H4002A"}).json()["results"][0]
    assert found["supplier_count"] == 0 and found["lowest_cost_cents"] is None
    detail = Client().get(f"/api/parts/{part('H4002A').pk}/").json()
    assert detail["sources"] == [] and [d["supplier"] for d in detail["delisted_sources"]] == ["s1"]

    assert purchase_order_for("s1", [(part("H4001A").pk, 2)], "PO-1", TODAY).lines[0].unit_price_cents == 1100
    with pytest.raises(OrderError, match="E_DELISTED") as caught:
        purchase_order_for("s1", [(part("H4002A").pk, 1)], "PO-2", TODAY)
    assert caught.value.code == "E_DELISTED"


def test_a_row_that_did_not_change_is_still_listed(tmp_path):
    """Unchanged rows store no raw row, so 'still in the feed' has to come from what the pass read."""
    s = snapshot_supplier()
    run_all(s, day(tmp_path, "day1.csv", [X, Y]))
    run = run_all(s, day(tmp_path, "day2.csv", [X, Y]))
    assert run.rows_written == 0 and run.rows_unchanged == 2
    assert run.notes["snapshot"]["delisted"] == 0
    assert not PartSource.objects.filter(delisted_at__isnull=False).exists()


def test_a_supplier_that_is_not_a_snapshot_keeps_every_listing(tmp_path):
    s = snapshot_supplier(flag=False)
    run_all(s, day(tmp_path, "day1.csv", [X, Y]))
    run_all(s, day(tmp_path, "day2.csv", [X]))
    assert not PartSource.objects.filter(delisted_at__isnull=False).exists()
    assert not SnapshotSeen.objects.exists()  # and nothing is kept for it


def test_a_delisted_part_that_reappears_is_listed_again(tmp_path):
    s = snapshot_supplier()
    run_all(s, day(tmp_path, "day1.csv", [X, Y]))
    run_all(s, day(tmp_path, "day2.csv", [X]))
    assert listing("H4002A", s).delisted_at is not None
    version = part("H4002A").search_version
    run = run_all(s, day(tmp_path, "day3.csv", [X, Y]))  # Y is back, byte for byte what it was on day 1
    assert run.rows_written == 0 and run.notes["snapshot"]["relisted"] == 1
    assert listing("H4002A", s).delisted_at is None and part("H4002A").search_version == version + 1
    assert purchase_order_for("s1", [(part("H4002A").pk, 1)], "PO-3", TODAY).lines[0].unit_price_cents == 500


def test_other_suppliers_listings_are_untouched(tmp_path):
    s, other = snapshot_supplier("s1"), snapshot_supplier("s2", flag=False)
    run_all(s, day(tmp_path, "a1.csv", [X, Y]))
    run_all(other, day(tmp_path, "b1.csv", [X, Y]))
    run_all(s, day(tmp_path, "a2.csv", [X]))
    assert listing("H4002A", s).delisted_at is not None and listing("H4002A", other).delisted_at is None
    assert documents_for(Part.objects.filter(number_norm="H4002A"))[0]["suppliers"] == ["s2"]


def test_ingest_then_match_later_still_delists_a_part_the_newer_feed_does_not_list(tmp_path):
    s = snapshot_supplier()
    ingest(s, FileAdapter(day(tmp_path, "day1.csv", [X, Y])))
    ingest(s, FileAdapter(day(tmp_path, "day2.csv", [X])))  # the catalog has not been matched in between
    normalize_pending()
    match_pending()
    assert listing("H4001A", s).delisted_at is None and listing("H4002A", s).delisted_at is not None


class ListAdapter(PagedAdapter):
    def __init__(self, items, fail_at=(), **kwargs):
        super().__init__(source_ref="https://feeds.example/catalog", page_size=2, sleep=lambda s: None, **kwargs)
        self.items, self.fail_at = items, set(fail_at)

    def fetch_page(self, offset, limit):
        if offset in self.fail_at:
            raise PermanentError("HTTP 400")
        return self.items[offset : offset + limit], len(self.items)


def item(n, cost="1.00"):
    return {"sku": f"H40{n:02d}A", "manufacturer": "Marlow", "title": "Part", "price": cost}


def test_a_partial_run_never_delists_and_the_run_that_finishes_the_pass_does(tmp_path):
    s = snapshot_supplier()
    ingest(s, ListAdapter([item(n) for n in range(1, 7)]))
    normalize_pending(), match_pending()
    run = ingest(s, ListAdapter([item(n) for n in (1, 2, 3, 4, 6)], fail_at={2}))  # item 5 is gone, and page 2 cannot be read
    normalize_pending(), match_pending()
    assert run.status == PipelineRun.Status.PARTIAL
    assert not PartSource.objects.filter(delisted_at__isnull=False).exists()
    run = ingest(s, ListAdapter([item(n) for n in (1, 2, 3, 4, 6)]))  # the next run resumes at the gap and completes the pass
    assert run.status == PipelineRun.Status.SUCCEEDED and run.notes["snapshot"]["delisted"] == 1
    assert listing("H4005A", s).delisted_at is not None and listing("H4006A", s).delisted_at is None


def test_a_failed_run_never_delists(tmp_path):
    s = snapshot_supplier()
    run_all(s, day(tmp_path, "day1.csv", [X, Y]))
    bad = tmp_path / "day2.csv"
    bad.write_text("Colour,Size\nred,big\n", encoding="utf-8")  # no column maps to a part number: the run fails before it stores anything
    run = ingest(s, FileAdapter(bad))
    assert run.status == PipelineRun.Status.FAILED
    assert not PartSource.objects.filter(delisted_at__isnull=False).exists()


def test_a_pass_with_a_row_it_could_not_read_delists_nothing(tmp_path):
    s = snapshot_supplier()
    run_all(s, day(tmp_path, "day1.csv", [X, Y]))
    path = day(tmp_path, "day2.csv", [X])
    with path.open("a", encoding="utf-8") as handle:
        handle.write("H4002A,Marlow\r\n")  # a split row: it could have been any part, including Y
    run = run_all(s, path)
    assert run.status == PipelineRun.Status.SUCCEEDED and run.rows_rejected == 1
    assert "could not read" in run.notes["snapshot"]["delist_refused"]
    assert not PartSource.objects.filter(delisted_at__isnull=False).exists()


def test_a_pass_that_lists_nothing_delists_nothing(tmp_path):
    s = snapshot_supplier()
    run_all(s, day(tmp_path, "day1.csv", [X, Y]))
    run = run_all(s, day(tmp_path, "day2.csv", []))
    assert run.status == PipelineRun.Status.SUCCEEDED and "no part" in run.notes["snapshot"]["delist_refused"]
    assert not PartSource.objects.filter(delisted_at__isnull=False).exists()


def test_a_pass_that_would_delist_most_of_the_catalog_needs_the_operator_to_say_so(tmp_path):
    s = snapshot_supplier()
    rows = [row(f"H40{n:02d}A", "Marlow", "Part", "1.00") for n in range(1, 13)]
    run_all(s, day(tmp_path, "day1.csv", rows))
    ingest(s, FileAdapter(day(tmp_path, "day2.csv", rows[:2])))
    normalize_pending(), match_pending()
    refused = PipelineRun.objects.filter(kind="ingest").order_by("-id").first().notes["snapshot"]
    assert "more than half" in refused["delist_refused"] and not PartSource.objects.filter(delisted_at__isnull=False).exists()
    # one more than half is refused and exactly half is not: a smaller loss is an ordinary change
    run = ingest(s, FileAdapter(day(tmp_path, "day3.csv", rows[:6])))
    assert run.notes["snapshot"]["delisted"] == 6
    # with the flag the large loss goes through
    s2 = snapshot_supplier("s2")
    run_all(s2, day(tmp_path, "e1.csv", rows))
    run = ingest(s2, FileAdapter(day(tmp_path, "e2.csv", rows[:2])), allow_mass_delist=True)
    assert run.notes["snapshot"]["delisted"] == 10


def test_applying_a_snapshot_twice_changes_nothing_the_second_time(tmp_path):
    s = snapshot_supplier()
    run_all(s, day(tmp_path, "day1.csv", [X, Y]))
    run_all(s, day(tmp_path, "day2.csv", [X]))
    version = part("H4002A").search_version
    assert apply_snapshot(s)["delisted"] == 0 and part("H4002A").search_version == version


def test_the_index_forgets_the_supplier_after_the_sync(tmp_path, meili):
    s = snapshot_supplier()
    run_all(s, day(tmp_path, "day1.csv", [X, Y]))
    index.rebuild()
    assert index.search("H4002A")["results"][0]["suppliers"] == ["s1"]
    run_all(s, day(tmp_path, "day2.csv", [X]))
    index.sync()
    hit = index.search("H4002A")["results"][0]
    assert hit["suppliers"] == [] and hit["lowest_cost_cents"] is None


def test_a_listing_a_person_links_is_checked_against_the_latest_full_feed(tmp_path):
    from catalog.matching import resolve_review
    from catalog.models import ReviewItem

    s = snapshot_supplier()
    nameless = row("NB100", "", "Gasket", "3.00")
    run_all(s, day(tmp_path, "day1.csv", [X, nameless]))
    item_in_feed = ReviewItem.objects.get()
    run_all(s, day(tmp_path, "day2.csv", [X, nameless]))
    resolve_review(item_in_feed, new_part=True, brand="Marlow")
    assert PartSource.objects.get(listing_key="|NB100").delisted_at is None  # the latest feed still lists it

    run_all(s, day(tmp_path, "day3.csv", [X, row("NB200", "", "Seal", "2.00")]))
    gone = ReviewItem.objects.get(listing_key="|NB200")
    run_all(s, day(tmp_path, "day4.csv", [X]))
    resolve_review(gone, new_part=True, brand="Marlow")
    assert PartSource.objects.get(listing_key="|NB200").delisted_at is not None  # linked after the feed dropped it


def test_the_ingest_command_flags_a_snapshot_supplier_and_reports_the_delisting(tmp_path, capsys):
    from django.core.management import call_command
    from django.core.management.base import CommandError

    from catalog.models import Supplier

    day1, day2 = day(tmp_path, "day1.csv", [X, Y]), day(tmp_path, "day2.csv", [X])
    call_command("ingest_feed", "s1", "--snapshot", "--file", str(day1))
    call_command("normalize_parts"), call_command("match_parts")
    call_command("ingest_feed", "s1", "--file", str(day2))  # the flag is remembered: it is not given again
    out = capsys.readouterr().out
    assert "s1 is now a full snapshot supplier" in out and "snapshot: delisted=1 relisted=0" in out
    assert Supplier.objects.get(code="s1").full_snapshot
    call_command("ingest_feed", "s1", "--incremental", "--file", str(day(tmp_path, "day3.csv", [X, Y])))
    assert not Supplier.objects.get(code="s1").full_snapshot
    with pytest.raises(CommandError, match="opposites"):
        call_command("ingest_feed", "s1", "--snapshot", "--incremental", "--file", str(day1))


def test_a_row_rejected_for_its_cost_is_still_a_row_the_feed_lists(tmp_path):
    s = snapshot_supplier()
    run_all(s, day(tmp_path, "day1.csv", [X, Y]))
    run = run_all(s, day(tmp_path, "day2.csv", [X, row("H4002A", "Marlow", "Hose", "five dollars")]))  # the cost is junk, the part is there
    assert run.rows_rejected == 1 and run.notes["snapshot"]["delisted"] == 0
    assert listing("H4002A", s).delisted_at is None


def test_a_supplier_that_is_not_a_snapshot_is_never_touched_even_when_asked(tmp_path):
    s = snapshot_supplier(flag=False)
    run_all(s, day(tmp_path, "day1.csv", [X, Y]))
    run_all(s, day(tmp_path, "day2.csv", [X]))
    assert apply_snapshot(s) == {} and not PartSource.objects.filter(delisted_at__isnull=False).exists()


# ---- second output round: a pass that has not finished never replaces the latest completed one -------------------------


class Feed:
    """A source with an address of its own: it lists parts 1..n, and with `cut` a page fails after that many rows (a partial pass).
    `resumable=False` is a live site (a scrape restarts every run), `fingerprint` a file's content identity ("" for a live API)."""

    def __init__(self, ref, n, cut=None, resumable=True, fingerprint=""):
        self.source_ref, self.n, self.cut, self.resumable, self._fingerprint = ref, n, cut, resumable, fingerprint

    def fingerprint(self):
        return self._fingerprint

    def notes(self):
        return {}

    def records(self, start_after=0):
        for i in range(1, self.n + 1):
            if i <= start_after and self.resumable:
                continue
            if self.cut is not None and i == self.cut + 1:
                yield SourceGap(i, self.n, "E_CHALLENGE: the page carries a bot challenge")
                return
            yield SourceRecord(i, {"PartNo": f"H40{i:02d}A", "Mfr": "Marlow", "Cost": "1.00"})


def live_count(s):
    return PartSource.objects.filter(supplier=s, delisted_at__isnull=True).count()


@pytest.mark.parametrize("resumable, fingerprints", [(False, ("", "", "")), (True, ("", "", "")), (True, ("a", "b", "c"))])
def test_a_partial_pass_leaves_the_latest_completed_pass_as_it_was_and_delists_nothing(resumable, fingerprints):
    """The class: the supplier's snapshot is the latest COMPLETED pass over any of its sources. A source that starts its pass
    again used to clear its own completed flag and its seen set, so one partial run made the match step fall back to an older
    pass of ANOTHER source (the site's old address, a file of last month) and delist what the site still offers."""
    s = snapshot_supplier()
    ingest(s, Feed("old-address", 8, resumable=resumable, fingerprint="o"))
    normalize_pending(), match_pending()
    ingest(s, Feed("new-address", 12, resumable=resumable, fingerprint=fingerprints[0]))
    normalize_pending(), match_pending()
    assert live_count(s) == 12
    partial = ingest(s, Feed("new-address", 12, cut=6, resumable=resumable, fingerprint=fingerprints[1]))
    normalize_pending(), match_pending()
    assert partial.status == PipelineRun.Status.PARTIAL
    assert live_count(s) == 12 and not PartSource.objects.filter(delisted_at__isnull=False).exists()
    assert apply_snapshot(s)["pass_source"] == "new-address"  # the pass that stays the latest is the last completed one
    kept = SnapshotSeen.objects.filter(checkpoint__source_ref="new-address", staging=False).count()
    assert kept == 12  # and its seen set is untouched by the pass that has not finished
    done = ingest(s, Feed("new-address", 10, resumable=resumable, fingerprint=fingerprints[2]))
    assert done.status == PipelineRun.Status.SUCCEEDED and done.notes["snapshot"]["delisted"] == 2  # the pass that finishes replaces it
    assert live_count(s) == 10
    assert SnapshotSeen.objects.filter(checkpoint__source_ref="new-address", staging=False).count() == 10
    assert not SnapshotSeen.objects.filter(staging=True).exists()


def test_a_run_that_died_inside_a_new_pass_is_resumed_with_the_parts_it_had_already_seen(tmp_path):
    from catalog.ingest.adapters import FileAdapter

    class Dying(FileAdapter):
        def records(self, start_after=0):
            for n, rec in enumerate(super().records(start_after)):
                if n >= 7:
                    raise ConnectionError("the connection dropped")
                yield rec

    s = snapshot_supplier()
    rows = [row(f"H40{n:02d}A", "Marlow", "Part", "1.00") for n in range(1, 21)]
    run_all(s, day(tmp_path, "feed.csv", rows))
    path = day(tmp_path, "feed.csv", [r[:3] + ["1.50", ""] for r in rows[:15]])  # the same file name, new content, five parts gone
    assert ingest(s, Dying(path), batch_size=5).status == PipelineRun.Status.FAILED
    assert live_count(s) == 20  # a dead pass delists nothing
    done = ingest(s, FileAdapter(path), batch_size=5)
    assert done.status == PipelineRun.Status.SUCCEEDED and done.notes["snapshot"]["delisted"] == 5
    normalize_pending(), match_pending()
    assert live_count(s) == 15


@pytest.mark.django_db(transaction=True)
def test_a_pass_that_was_running_when_the_staging_migration_ran_keeps_what_it_had_read():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.migrate([("catalog", "0006_pass_id_on_the_checkpoint")])
    old = executor.loader.project_state([("catalog", "0006_pass_id_on_the_checkpoint")]).apps
    Supplier, Checkpoint, Seen = old.get_model("catalog", "Supplier"), old.get_model("catalog", "FeedCheckpoint"), old.get_model("catalog", "SnapshotSeen")
    sup = Supplier.objects.create(code="mig", name="Mig", full_snapshot=True)
    running = Checkpoint.objects.create(supplier=sup, source_ref="running", completed=False)
    done = Checkpoint.objects.create(supplier=sup, source_ref="done", completed=True, completed_at=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc))
    Seen.objects.create(checkpoint=running, part_number_raw="A1", brand_raw="")
    Seen.objects.create(checkpoint=done, part_number_raw="B1", brand_raw="")
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    new = executor.loader.project_state(executor.loader.graph.leaf_nodes()).apps
    staged = dict(new.get_model("catalog", "SnapshotSeen").objects.values_list("part_number_raw", "staging"))
    assert staged == {"A1": True, "B1": False}
    new.get_model("catalog", "SnapshotSeen").objects.all().delete()
    new.get_model("catalog", "FeedCheckpoint").objects.all().delete()
    new.get_model("catalog", "Supplier").objects.all().delete()


def test_what_a_dead_pass_read_is_not_counted_as_seen_by_the_pass_that_replaces_it(tmp_path):
    """The file changed again before the dead pass was resumed, so the new pass starts afresh: parts 1 to 5 were read by the
    dead one and are not in the file now, and the pass that completes must delist them."""
    from catalog.ingest.adapters import FileAdapter

    class Dying(FileAdapter):
        def records(self, start_after=0):
            for n, rec in enumerate(super().records(start_after)):
                if n >= 7:
                    raise ConnectionError("the connection dropped")
                yield rec

    s = snapshot_supplier()
    rows = [row(f"H40{n:02d}A", "Marlow", "Part", "1.00") for n in range(1, 21)]
    run_all(s, day(tmp_path, "feed.csv", rows))
    assert ingest(s, Dying(day(tmp_path, "feed.csv", [r[:3] + ["1.50", ""] for r in rows[:15]])), batch_size=5).status == PipelineRun.Status.FAILED
    done = ingest(s, FileAdapter(day(tmp_path, "feed.csv", rows[5:])), batch_size=5)
    assert done.status == PipelineRun.Status.SUCCEEDED and done.notes["snapshot"]["delisted"] == 5
