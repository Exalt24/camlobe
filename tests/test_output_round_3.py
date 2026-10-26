"""Third output round: the classes a blind adversary reproduced against the scraper, REST, SOAP and file adapters.

Every section names its class, states the rule, and is a generated test or a table, not one example. The data is invented.
"""
import uuid

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from catalog.ingest.adapters import FileAdapter, SourceRecord
from catalog.ingest.runner import ingest
from catalog.matching import match_pending
from catalog.models import Part, PipelineRun, RawPart, RejectedRow
from catalog.normalize_step import normalize_pending

from .helpers import get_supplier, row, write_csv
from .test_scrape_pages import adapter_for, priced

pytestmark = pytest.mark.django_db(transaction=True)


class Live:
    """A live source (no content identity, so every run reads it again) over a list of (part, cost) rows."""

    resumable = False

    def __init__(self, rows, source_ref="live"):
        self.rows = rows
        self.source_ref = source_ref

    def fingerprint(self):
        return ""

    def notes(self):
        return {}

    def records(self, start_after=0):
        for position, (number, cost) in enumerate(self.rows, start=1):
            yield SourceRecord(position, {"PartNo": number, "Mfr": "Marlow", "Cost": cost}, f"{number},{cost}")


def fresh_supplier():
    return get_supplier(f"o3{uuid.uuid4().hex[:10]}")


def versions(supplier, number):
    return list(RawPart.objects.filter(supplier=supplier, part_number_raw=number).order_by("id").values_list("cost_cents", flat=True))


# ---- F8: the same part twice in one source at two prices -------------------------------------------------------------------------
# Rule: the FIRST row of a part in a run is the part's row. A later row of the same part number and brand that differs is refused
# as `duplicate_part` (counted and kept with its position and raw text); an identical repeat is unchanged. A second run over an
# unchanged source therefore changes nothing, however many parts are listed twice.

PRICES = st.sampled_from(["5.00", "40.00", "9.50"])
LISTED = st.lists(st.tuples(st.sampled_from(["DA1001", "DA1002", "DA1003"]), PRICES), min_size=1, max_size=9)


@given(LISTED, st.integers(min_value=1, max_value=4))
@hyp_settings(max_examples=40, deadline=None)
def test_a_part_listed_twice_with_other_prices_stores_its_first_row_and_a_rerun_changes_nothing(listed, batch_size):
    supplier = fresh_supplier()
    first_price: dict[str, str] = {}
    refused = 0
    for number, cost in listed:
        if first_price.setdefault(number, cost) != cost:
            refused += 1
    first = ingest(supplier, Live(listed), batch_size=batch_size)
    assert first.status == PipelineRun.Status.SUCCEEDED and first.rows_rejected == refused
    assert list(RejectedRow.objects.filter(run=first).values_list("reason", flat=True)) == ["duplicate_part"] * refused
    for number, cost in first_price.items():
        assert versions(supplier, number) == [int(round(float(cost) * 100))]
    stored = RawPart.objects.filter(supplier=supplier).count()
    for _ in range(2):
        again = ingest(supplier, Live(listed), batch_size=batch_size)
        assert again.rows_written == 0 and again.rows_rejected == refused
    assert RawPart.objects.filter(supplier=supplier).count() == stored


def test_a_part_listed_twice_does_not_climb_the_search_version_each_day():
    supplier = fresh_supplier()
    listed = [("DA1001", "5.00"), ("DA1001", "40.00")]
    seen = []
    for _ in range(3):
        ingest(supplier, Live(listed))
        normalize_pending()
        match_pending()
        seen.append(list(Part.objects.filter(number_norm="DA1001").values_list("search_version", flat=True)))
    assert seen[0] == seen[1] == seen[2]


def test_the_same_row_listed_twice_with_the_same_content_is_not_a_refusal(tmp_path):
    supplier = fresh_supplier()
    run = ingest(supplier, FileAdapter(write_csv(tmp_path / "f.csv", [row("AB1001"), row("AB1001"), row("AB1002")])))
    assert (run.rows_written, run.rows_unchanged, run.rows_rejected) == (2, 1, 0)


def test_a_csv_with_a_part_twice_at_two_prices_keeps_the_first_across_batches(tmp_path):
    supplier = fresh_supplier()
    rows = [row("AB1001", cost="5.00"), row("AB1002"), row("AB1003"), row("AB1001", cost="40.00")]
    run = ingest(supplier, FileAdapter(write_csv(tmp_path / "f.csv", rows)), batch_size=2)
    assert (run.rows_written, run.rows_rejected) == (3, 1)
    assert versions(supplier, "AB1001") == [500]
    refused = RejectedRow.objects.get(run=run)
    assert (refused.reason, refused.position) == ("duplicate_part", 4) and "40.00" in refused.raw


def test_a_scraped_part_listed_twice_at_two_prices_changes_nothing_on_the_next_two_walks():
    from .fakes import products_table

    page = products_table([("DA1001", "Marlow", "Oil filter", "5.00"), ("DA1001", "Marlow", "Oil filter x10", "40.00"), ("DA1002", "Marlow", "Hose", "3.00")])
    supplier = fresh_supplier()
    out = [ingest(supplier, adapter_for(lambda url: (200, {}, page))) for _ in range(3)]
    assert [(r.status, r.rows_written, r.rows_rejected) for r in out] == [("succeeded", 2, 1), ("succeeded", 0, 1), ("succeeded", 0, 1)]
    assert versions(supplier, "DA1001") == [500]


# ---- F1: a row is `unchanged` only when it equals the NEWEST stored version of its part -----------------------------------------
# The class: "a key exists" treated as "this is the current value". A REST or SOAP source that is resumed inside one pass (a page
# that fails, a run that dies) reads the same positions again; a price that goes 10.00, 12.00, 10.00 across those runs must end on
# 10.00, and a row read again after a crash must still write nothing twice. The model below is the rule itself: for every row a
# run read and committed, it is written when it differs from the newest version the model holds, and unchanged when it equals it.
# REST and SOAP share the reader (PagedAdapter) and the file path is the one SFTP downloads into, so the tables cover all of them.

from catalog.ingest.mapping import parse_cost_cents  # noqa: E402
from catalog.ingest.paged import PagedAdapter, TransientError  # noqa: E402


class Spy:
    """Wraps an adapter: remembers every record it yielded, and can die (a dropped connection) after `die_after` of them."""

    def __init__(self, inner, number_key, cost_key, die_after=None):
        self.inner, self.number_key, self.cost_key, self.die_after = inner, number_key, cost_key, die_after
        self.yielded: list[tuple[str, int]] = []

    def __getattr__(self, name):  # source_ref, resumable, fingerprint, notes, secret_values
        return getattr(self.inner, name)

    def records(self, start_after=0):
        for item in self.inner.records(start_after):
            if isinstance(item, SourceRecord):
                if self.die_after is not None and len(self.yielded) >= self.die_after:
                    raise ConnectionError("the connection dropped")
                self.yielded.append((item.fields[self.number_key], parse_cost_cents(item.fields[self.cost_key])))
            yield item


class Model:
    """What the rule says: the newest version per part, and every version in order."""

    def __init__(self):
        self.newest: dict[str, int] = {}
        self.history: dict[str, list[int]] = {}

    def feed(self, committed):
        written = unchanged = 0
        for number, cents in committed:
            if self.newest.get(number) == cents:
                unchanged += 1
            else:
                self.newest[number] = cents
                self.history.setdefault(number, []).append(cents)
                written += 1
        return written, unchanged

    def check(self, supplier, run, written, unchanged):
        assert (run.rows_written, run.rows_unchanged) == (written, unchanged)
        for number, history in self.history.items():
            assert versions(supplier, number) == history, number


class Table(PagedAdapter):
    """A paged source over a list of items, with some page offsets down (a gap, not a failed run)."""

    def __init__(self, items, down=(), **kwargs):
        super().__init__("table", page_size=2, max_retries=0, **kwargs)
        self.items, self.down = items, set(down)

    def fetch_page(self, offset, limit):
        if offset in self.down:
            raise TransientError("HTTP 500")
        return self.items[offset : offset + limit], len(self.items)


def table_items(prices):
    return [{"sku": f"TB{i:04d}", "manufacturer": "Marlow", "title": "Oil filter", "price": price} for i, price in enumerate(prices)]


COSTS = st.sampled_from(["10.00", "12.00", "9.50"])
PAGED_RUN = st.tuples(
    st.lists(COSTS, min_size=6, max_size=6),  # today's price of each of the six parts (three pages of two)
    st.sets(st.sampled_from([0, 2, 4]), max_size=2),  # pages that answer 500 today
    st.one_of(st.none(), st.integers(min_value=0, max_value=6)),  # the connection drops after this many rows
)


@given(st.lists(PAGED_RUN, min_size=2, max_size=6))
@hyp_settings(max_examples=60, deadline=None)
def test_a_paged_source_resumed_inside_one_pass_keeps_the_newest_price_whatever_the_prices_do(plan):
    """REST and SOAP share this reader: partial runs, killed runs and full runs in any order, any prices."""
    supplier, model = fresh_supplier(), Model()
    for prices, down, die_after in plan:
        spy = Spy(Table(table_items(prices), down), "sku", "price", die_after)
        run = ingest(supplier, spy, batch_size=1)  # one row a batch: what was yielded before a drop is what was committed
        model.check(supplier, run, *model.feed(spy.yielded))


@pytest.mark.parametrize("prices", [("10.00", "12.00", "10.00"), ("10.00", "10.00", "12.00", "10.00"), ("12.00", "10.00", "12.00", "10.00", "12.00")])
@pytest.mark.parametrize("kind", ["rest", "soap"])
def test_rest_and_soap_resumed_after_a_page_that_keeps_failing_end_on_the_price_the_source_has_now(prices, kind):
    """The adversary's case on the real adapters: page 2 answers 500 on every run, so every run is the same pass."""
    from catalog.ingest.rest import RestAdapter
    from catalog.ingest.soap import SoapAdapter

    from .fakes import Behaviour, rest_server, soap_server

    items = [{"sku": f"MF-H{i:04d}A", "manufacturer": "Marlow", "title": "Oil filter", "price": "5.00", "gtin": ""} for i in range(6)]
    server = (rest_server if kind == "rest" else soap_server)(Behaviour(items, always={2: 500}))
    try:
        supplier = fresh_supplier()
        for today in prices:
            items[5]["price"] = today
            if kind == "rest":
                adapter = RestAdapter(f"http://127.0.0.1:{server.port}/catalog", page_size=2, max_retries=0)
            else:
                adapter = SoapAdapter(f"http://127.0.0.1:{server.port}/catalog?wsdl", page_size=2, max_retries=0)
            run = ingest(supplier, adapter)
            assert run.status == PipelineRun.Status.PARTIAL
            assert versions(supplier, "MF-H0005A")[-1] == int(round(float(today) * 100)), today
        expected = [prices[0]] + [b for a, b in zip(prices, prices[1:]) if a != b]
        assert versions(supplier, "MF-H0005A") == [int(round(float(p) * 100)) for p in expected]
    finally:
        server.close()


FILE_DAY = st.tuples(
    st.lists(COSTS, min_size=6, max_size=6),
    st.lists(st.integers(min_value=0, max_value=7), max_size=2),  # connections that drop after this many rows, before the run that finishes
)


@given(days=st.lists(FILE_DAY, min_size=2, max_size=5), batch_size=st.sampled_from([1, 3]))
@hyp_settings(max_examples=30, deadline=None)
def test_a_file_read_again_after_a_crash_writes_nothing_twice_whatever_the_prices_do_between_days(days, batch_size, tmp_path_factory):
    """A killed run commits whole batches only, the run after it starts at the checkpoint, and a changed file is a new pass."""
    supplier, model = fresh_supplier(), Model()
    folder = tmp_path_factory.mktemp("o3days")
    for prices, drops in days:
        path = write_csv(folder / "feed.csv", [row(f"FD{i:04d}", cost=price) for i, price in enumerate(prices)])  # the same name every day
        for after in drops:
            spy = Spy(FileAdapter(path), "PartNo", "Cost", die_after=after)
            run = ingest(supplier, spy, batch_size=batch_size)
            committed = spy.yielded
            if run.status == PipelineRun.Status.FAILED:
                committed = spy.yielded[: len(spy.yielded) // batch_size * batch_size]
            model.check(supplier, run, *model.feed(committed))
        spy = Spy(FileAdapter(path), "PartNo", "Cost")
        run = ingest(supplier, spy, batch_size=batch_size)
        assert run.status == PipelineRun.Status.SUCCEEDED
        model.check(supplier, run, *model.feed(spy.yielded))


@given(st.lists(st.tuples(COSTS, st.booleans()), min_size=2, max_size=6))
@hyp_settings(max_examples=25, deadline=None)
def test_a_scraped_site_walked_again_after_a_partial_walk_keeps_the_newest_price(plan):

    supplier, model = fresh_supplier(), Model()
    for price, blocked in plan:
        spy = Spy(adapter_for(priced(lambda: price, challenge=lambda: blocked)), "Part No", "Price")
        run = ingest(supplier, spy)
        assert run.status == (PipelineRun.Status.PARTIAL if blocked else PipelineRun.Status.SUCCEEDED)
        model.check(supplier, run, *model.feed(spy.yielded))


# ---- F2: a walk whose own reported total changes between pages is not proven complete --------------------------------------------
# Rule: a total that differs from the one the first answer of the run gave ends the run PARTIAL with a coded gap (E_TOTAL_CHANGED),
# so nothing is delisted, and the next run starts again from the first page (resuming at the checkpoint over a shifted list would
# skip the part that moved across the page boundary and delist it). The scraper reads no total from the site, so it has no such
# signal: that is stated in docs/feeds.md.

from catalog.models import FeedCheckpoint, PartSource, SnapshotSeen  # noqa: E402


def settle():
    normalize_pending()
    match_pending()


def snapshot_supplier():
    supplier = fresh_supplier()
    supplier.full_snapshot = True
    supplier.save()
    return supplier


def listed(supplier):
    return sorted(PartSource.objects.filter(supplier=supplier, delisted_at__isnull=True).values_list("supplier_part_number", flat=True))


class Wobble(PagedAdapter):
    """Ten items, five pages of two, and the total each answer reports is taken from `totals` in turn."""

    def __init__(self, totals, **kwargs):
        super().__init__("wobble", page_size=2, max_retries=0, **kwargs)
        self.totals, self.reported = list(totals), []

    def fetch_page(self, offset, limit):
        total = self.totals[len(self.reported) % len(self.totals)]
        self.reported.append(total)
        items = [{"sku": f"WB{1000 + i}", "manufacturer": "Marlow", "title": "Oil filter", "price": "10.00"} for i in range(10)]
        return items[offset : offset + limit], total


@given(st.lists(st.sampled_from([8, 9, 10]), min_size=1, max_size=6))
@hyp_settings(max_examples=50, deadline=None)
def test_a_walk_is_a_success_only_when_every_answer_reported_the_same_total(totals):
    supplier = snapshot_supplier()
    ingest(supplier, Wobble([10]))  # day 1: a clean pass lists all ten
    settle()
    everything = listed(supplier)
    adapter = Wobble(totals)
    run = ingest(supplier, adapter)
    settle()
    moved = len(set(adapter.reported)) > 1
    assert run.status == (PipelineRun.Status.PARTIAL if moved else PipelineRun.Status.SUCCEEDED)
    if moved:
        assert run.errors[0]["message"].startswith("E_TOTAL_CHANGED")
        assert "snapshot" not in run.notes  # a partial run applies no snapshot
        assert listed(supplier) == everything  # nothing delisted by a walk that was not proven complete
        checkpoint = FeedCheckpoint.objects.get(supplier=supplier)
        assert (checkpoint.position, checkpoint.completed) == (0, False)  # the next run starts again from the first page
        assert not SnapshotSeen.objects.filter(checkpoint=checkpoint, staging=True).exists()
        again = ingest(supplier, Wobble([10]))
        assert again.status == PipelineRun.Status.SUCCEEDED and "resumed_from" not in again.notes


@pytest.mark.parametrize("kind", ["rest", "soap"])
def test_a_part_withdrawn_while_a_rest_or_soap_walk_is_on_its_first_page_delists_nothing_that_day_and_only_it_the_next(kind):
    """The adversary's case on the real adapters: 24 parts, four to a page, RX1001 withdrawn after page one is served."""
    from catalog.ingest.rest import RestAdapter
    from catalog.ingest.soap import SoapAdapter

    from .fakes import Behaviour, rest_server, soap_server

    class Withdraws(Behaviour):
        withdraw = None  # the sku to drop when the SECOND request arrives, so the first answer still reported 24

        def decide(self, offset, limit, auth):
            with self.lock:
                if self.withdraw and len(self.requests) == 1:
                    self.items[:] = [i for i in self.items if i["sku"] != self.withdraw]
            return super().decide(offset, limit, auth)

    items = [{"sku": f"RX{1000 + i}", "manufacturer": "Marlow", "title": "Oil filter", "price": f"{10 + i}.00", "gtin": ""} for i in range(24)]
    behaviour = Withdraws(items)
    server = (rest_server if kind == "rest" else soap_server)(behaviour)

    def walk(supplier):
        if kind == "rest":
            adapter = RestAdapter(f"http://127.0.0.1:{server.port}/catalog", page_size=4, max_retries=0)
        else:
            adapter = SoapAdapter(f"http://127.0.0.1:{server.port}/catalog?wsdl", page_size=4, max_retries=0)
        run = ingest(supplier, adapter)
        settle()
        return run

    try:
        supplier = snapshot_supplier()
        assert walk(supplier).status == PipelineRun.Status.SUCCEEDED
        behaviour.withdraw = "RX1001"
        behaviour.requests.clear()
        day2 = walk(supplier)
        assert day2.status == PipelineRun.Status.PARTIAL and "E_TOTAL_CHANGED" in day2.errors[0]["message"]
        assert len(listed(supplier)) == 24  # nothing delisted, not even the withdrawn part: the walk was not proven complete
        behaviour.withdraw = None
        day3 = walk(supplier)
        assert day3.status == PipelineRun.Status.SUCCEEDED and "resumed_from" not in day3.notes
        assert len(listed(supplier)) == 23 and "RX1004" in listed(supplier) and "RX1001" not in listed(supplier)
    finally:
        server.close()


# ---- F3: a feed cut inside its last row is not stored as a clean success ---------------------------------------------------------
# Rule: two signals only, because only they cannot be an ordinary file: the text ends inside an open quote, or the final record has
# fewer fields than the header and no line break after it. The cut row is not stored and the run is partial (E_FILE_TRUNCATED).
# A cut that leaves every column in place (`123.45` cut to `1`) has no signal, since a valid file may end without a newline:
# KNOWN_LIMIT in docs/feeds.md, pinned below so the documentation stays true. SFTP reads the downloaded file through the same
# FileAdapter, so the test through an SFTP server shares the fix.

import csv  # noqa: E402
import io  # noqa: E402

from catalog.models import FeedCheckpoint  # noqa: E402, F811
from .test_remote_adapters import adapter_for as sftp_adapter_for, sftp  # noqa: E402, F401

DESCRIPTIONS = st.sampled_from(["Pad", "Pad, front", "Rotor, vented, 300 mm", 'Hose 5" long'])


def csv_bytes(rows, final_newline=True):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(["PartNo", "Mfr", "Description", "Cost"])
    writer.writerows(rows)
    text = buf.getvalue()
    return (text if final_newline else text[:-2]).encode("utf-8")


def last_row_text(rows):
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow(rows[-1])
    return buf.getvalue()


def what_a_prefix_of_the_last_row_shows(prefix):
    """By hand, not by the parser under test: (inside an open quote, number of fields complete or begun)."""
    inside, commas = False, 0
    for ch in prefix:
        if ch == '"':
            inside = not inside
        elif ch == "," and not inside:
            commas += 1
    return inside, commas + 1


@given(
    rows=st.lists(st.tuples(DESCRIPTIONS, st.sampled_from(["12.34", "7.00", "123.45"])), min_size=1, max_size=5),
    data=st.data(),
)
@hyp_settings(max_examples=80, deadline=None)
def test_a_csv_cut_anywhere_in_its_last_row_is_partial_when_it_shows_the_cut_and_stored_when_it_cannot(rows, data, tmp_path_factory):
    table = [[f"FT{1000 + i}", "Marlow", description, cost] for i, (description, cost) in enumerate(rows)]
    whole = csv_bytes(table, final_newline=False)
    last = last_row_text(table).encode("utf-8")
    cut_at = data.draw(st.integers(min_value=0, max_value=len(last)))  # 0 is a file that ends at a line break
    body = whole[: len(whole) - len(last) + cut_at]
    path = tmp_path_factory.mktemp("o3cut") / "feed.csv"
    path.write_bytes(body)
    supplier = fresh_supplier()
    run = ingest(supplier, FileAdapter(path))
    prefix = last[:cut_at].decode("utf-8")
    inside, fields = what_a_prefix_of_the_last_row_shows(prefix)
    shows_the_cut = cut_at > 0 and cut_at < len(last) and (inside or fields < 4)
    if shows_the_cut:
        assert run.status == PipelineRun.Status.PARTIAL and run.errors[0]["message"].startswith("E_FILE_TRUNCATED")
        assert RawPart.objects.filter(supplier=supplier).count() == len(table) - 1  # every whole row, never the cut one
        assert not RejectedRow.objects.filter(run=run).exists()
        assert not FeedCheckpoint.objects.get(supplier=supplier).completed
    else:
        assert run.status == PipelineRun.Status.SUCCEEDED
        # a cut that leaves every column is stored as the number it now is, or rejected when it is no number (`123.`): no signal either way
        assert RawPart.objects.filter(supplier=supplier).count() + RejectedRow.objects.filter(run=run).count() == (len(table) if cut_at else len(table) - 1)


def test_known_limit_a_cut_inside_the_last_cell_with_every_column_present_reads_as_an_ordinary_file(tmp_path):
    """No rule separates `...,Caliper,1` (cut from 123.45) from a file that ends without a newline: stored as 100 cents."""
    path = tmp_path / "feed.csv"
    path.write_bytes(csv_bytes([["FT1001", "Marlow", "Pad", "20.00"]]) + b"FT1002,Marlow,Caliper,1")
    run = ingest(fresh_supplier(), FileAdapter(path))
    assert run.status == PipelineRun.Status.SUCCEEDED
    assert RawPart.objects.get(part_number_raw="FT1002").cost_cents == 100


def test_a_whole_csv_with_or_without_its_last_line_break_is_an_ordinary_success(tmp_path):
    rows = [["FT1001", "Marlow", 'Pad, "front"', "20.00"], ["FT1002", "Marlow", "Rotor", "40.00"]]
    for final_newline in (True, False):
        path = tmp_path / f"feed{final_newline}.csv"
        path.write_bytes(csv_bytes(rows, final_newline))
        run = ingest(fresh_supplier(), FileAdapter(path))
        assert run.status == PipelineRun.Status.SUCCEEDED and run.rows_written == 2 and not run.errors


def test_a_short_last_row_with_a_line_break_after_it_is_still_one_rejected_row(tmp_path):
    path = tmp_path / "feed.csv"
    path.write_bytes(csv_bytes([["FT1001", "Marlow", "Pad", "20.00"]]) + b"FT1002,Marlow\r\n")
    run = ingest(fresh_supplier(), FileAdapter(path))
    assert run.status == PipelineRun.Status.SUCCEEDED and run.rows_rejected == 1
    assert RejectedRow.objects.get(run=run).reason == "column_count"


def test_the_same_cut_file_arriving_over_sftp_is_partial_too(sftp, tmp_path):
    from catalog.ingest.sftp import SftpAdapter  # noqa: F401

    (sftp.root / "feed.csv").write_bytes(csv_bytes([["FT1001", "Marlow", "Pad", "20.00"]]) + b'FT1002,Marlow,"Caliper, fro')
    server = sftp()
    supplier = fresh_supplier()
    run = ingest(supplier, sftp_adapter_for(server, "/feed.csv"))
    assert run.status == PipelineRun.Status.PARTIAL and run.errors[0]["message"].startswith("E_FILE_TRUNCATED")
    assert RawPart.objects.filter(supplier=supplier).count() == 1
