"""How the scraper decides a page is a whole page and the walk is over, and what it does when a page is not what it looks like.

The walk ends as a success only when the SITE said it ended: the last page was whole (its product table and its html
were closed) and nothing was cut. Anything else is a coded gap, so the run is partial, delists nothing and never
completes the checkpoint. Pages here are served from memory (`PageSession`), so a body can be cut at any byte.
"""
import random

import pytest

from catalog.ingest.adapters import SourceGap, SourceRecord
from catalog.ingest.paged import FatalError
from catalog.ingest.runner import ingest
from catalog.ingest.scrape import ScrapeAdapter, ScrapeConfig
from catalog.matching import match_pending
from catalog.models import FeedCheckpoint, PartSource, PipelineRun, RawPart
from catalog.normalize_step import normalize_pending

from .fakes import PageSession, products_table
from .helpers import get_supplier

pytestmark = pytest.mark.django_db(transaction=True)

BASE = "http://shop.test"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def adapter_for(route, **changes):
    clock = Clock()
    base = dict(start_url=f"{BASE}/catalog?page=1", delay_seconds=0.0, respect_robots=False, user_agent="camlobe-test/1.0 (a test)")
    base.update(changes)
    return ScrapeAdapter(ScrapeConfig(**base), session=PageSession(route), sleep=clock.sleep, clock=clock.time, jitter=lambda d: d)


def part(i):
    return (f"MF-H{i:04d}A", "Marlow", f"Oil filter {i}", f"{i}.00")


def catalog(n, per=3, overrides=None, **table_kwargs):
    """A route serving n parts, `per` to a page, each page linking to the next. `overrides[page]` replaces that page's body."""
    overrides = overrides or {}
    pages = max(1, -(-n // per))

    def route(url):
        page = int(url.rsplit("page=", 1)[1].split("&")[0].split("#")[0])
        if page in overrides:
            return overrides[page] if isinstance(overrides[page], tuple) else (200, {}, overrides[page])
        chunk = [part(i) for i in range((page - 1) * per + 1, min(n, page * per) + 1)]
        nxt = f'<a rel="next" href="/catalog?page={page + 1}">Next</a>' if page < pages else ""
        return 200, {}, products_table(chunk, nxt=nxt, **table_kwargs)

    return route


def records(adapter):
    return list(adapter.records())


def rows(out):
    return [r for r in out if isinstance(r, SourceRecord)]


def codes(out):
    return [g.message.split(":")[0] for g in out if isinstance(g, SourceGap)]


def snapshot_supplier(code="snap"):
    supplier = get_supplier(code)
    supplier.full_snapshot = True
    supplier.save()
    return supplier


def live_listings(supplier):
    return PartSource.objects.filter(supplier=supplier, delisted_at__isnull=True).count()


# ---- F2: a page that is not proven whole does not end the walk as a success --------------------------------------------


def test_an_empty_first_page_is_a_failed_run_with_a_code_and_no_completed_checkpoint():
    run = ingest(get_supplier("e"), adapter_for(lambda url: (200, {}, products_table([]))))
    assert run.status == PipelineRun.Status.FAILED and "E_PAGE_EMPTY" in run.errors[0]["message"]
    assert not FeedCheckpoint.objects.get().completed


def test_allow_empty_says_a_supplier_with_nothing_to_list_is_a_success():
    run = ingest(get_supplier("e"), adapter_for(lambda url: (200, {}, products_table([])), allow_empty=True))
    assert run.status == PipelineRun.Status.SUCCEEDED and run.rows_read == 0 and FeedCheckpoint.objects.get().completed


def test_an_empty_page_after_a_full_one_is_a_coded_gap_so_the_run_is_partial():
    route = catalog(9, overrides={2: products_table([])})  # a glitch: the shell of the table, no rows, no next link
    run = ingest(get_supplier("e"), adapter_for(route))
    assert run.status == PipelineRun.Status.PARTIAL and run.errors[0]["message"].startswith("E_PAGE_EMPTY")
    assert run.rows_written == 3 and not FeedCheckpoint.objects.get().completed


def test_allow_empty_also_accepts_an_empty_page_the_site_ends_on():
    pages = {1: products_table([part(1), part(2)], nxt='<a rel="next" href="/catalog?page=2">Next</a>'), 2: products_table([])}
    run = ingest(get_supplier("e"), adapter_for(lambda url: (200, {}, pages[int(url[-1])]), allow_empty=True))
    assert run.status == PipelineRun.Status.SUCCEEDED and run.rows_written == 2


@pytest.mark.parametrize("cut_at", ["after_row", "inside_row"])
def test_a_page_cut_off_inside_its_table_stores_nothing_from_it_and_the_run_is_partial(cut_at):
    full = products_table([part(i) for i in (4, 5, 6)], nxt='<a rel="next" href="/catalog?page=3">Next</a>')
    first_end = full.index("</tr>", full.index("<tbody>")) + 5
    body = full[: first_end + 1] if cut_at == "after_row" else full[: full.index("<td>", first_end) + 30]
    run = ingest(get_supplier("c"), adapter_for(catalog(9, overrides={2: body})))
    assert run.status == PipelineRun.Status.PARTIAL and run.errors[0]["message"].startswith("E_PAGE_TRUNCATED")
    assert sorted(RawPart.objects.values_list("part_number_raw", flat=True)) == [part(i)[0] for i in (1, 2, 3)]
    assert not FeedCheckpoint.objects.get().completed


def test_a_last_page_cut_off_after_its_table_is_not_proof_that_the_site_ended():
    body = products_table([part(7), part(8)], close=False)  # the table is whole, the html never closes: a cut before the pager
    run = ingest(get_supplier("c"), adapter_for(catalog(8, overrides={3: body})))
    assert run.status == PipelineRun.Status.PARTIAL and run.errors[0]["message"].startswith("E_PAGE_TRUNCATED")


def test_a_body_shorter_than_the_length_the_site_announced_is_a_gap():
    page = products_table([part(1), part(2)])
    run = ingest(get_supplier("c"), adapter_for(lambda url: (200, {"Content-Length": str(len(page) + 40)}, page)))
    assert run.status == PipelineRun.Status.PARTIAL and run.errors[0]["message"].startswith("E_PAGE_TRUNCATED")
    assert RawPart.objects.count() == 0


def test_an_announced_length_that_matches_is_a_whole_page():
    page = products_table([part(1), part(2)])
    run = ingest(get_supplier("c"), adapter_for(lambda url: (200, {"Content-Length": str(len(page.encode()))}, page)))
    assert run.status == PipelineRun.Status.SUCCEEDED and run.rows_written == 2


def test_a_glitch_page_does_not_delist_parts_the_site_still_offers():
    supplier = snapshot_supplier()
    ingest(supplier, adapter_for(catalog(12)))
    normalize_pending()
    match_pending()
    assert live_listings(supplier) == 12
    run = ingest(supplier, adapter_for(catalog(12, overrides={4: products_table([])})))
    assert run.status == PipelineRun.Status.PARTIAL and live_listings(supplier) == 12 and "snapshot" not in run.notes


def test_a_whole_walk_over_a_snapshot_supplier_still_delists_what_the_site_dropped():
    supplier = snapshot_supplier()
    ingest(supplier, adapter_for(catalog(12)))
    normalize_pending()
    match_pending()
    run = ingest(supplier, adapter_for(catalog(9)))
    assert run.status == PipelineRun.Status.SUCCEEDED and run.notes["snapshot"]["delisted"] == 3 and live_listings(supplier) == 9


def test_every_cut_of_every_page_is_a_gap_or_loses_nothing():
    """The class: a body cut at ANY byte. The walk may end as a success only if every row was read."""
    n, per = 7, 3
    pages = {p: catalog(n, per)(f"{BASE}/catalog?page={p}")[2] for p in (1, 2, 3)}
    want = [part(i)[0] for i in range(1, n + 1)]
    for page, body in pages.items():
        for cut in range(0, len(body)):
            served = dict(pages)
            served[page] = body[:cut]
            adapter = adapter_for(lambda url: (200, {}, served[int(url.rsplit("page=", 1)[1])]))
            try:
                out = records(adapter)
            except FatalError:
                continue  # an empty first page is a failed run
            got = [r.fields["Part No"] for r in rows(out)]
            assert codes(out) or got == want, (page, cut, body[max(0, cut - 20) : cut])
            assert got == want[: len(got)]  # whatever was read is a prefix of the truth, never a row twice or out of order


def test_complete_pages_of_every_ordinary_shape_are_a_success():
    """25 everyday pages: with and without thead and tbody, upper case tags, a pager before or after, attributes, comments."""
    rng = random.Random(3)
    shapes = []
    for i in range(25):
        n = rng.randint(1, 8)
        shapes.append(n)
    done = 0
    for n in shapes:
        per = rng.randint(1, 4)
        style = rng.choice(["plain", "upper", "nohead", "pager_first", "comment", "attrs"])
        pages = -(-n // per)

        def route(url, n=n, per=per, style=style, pages=pages):
            page = int(url.rsplit("page=", 1)[1])
            chunk = [part(i) for i in range((page - 1) * per + 1, min(n, page * per) + 1)]
            nxt = f'<a rel="next" href="/catalog?page={page + 1}">Next</a>' if page < pages else ""
            body = products_table(chunk, nxt="" if style == "pager_first" else nxt)
            if style == "pager_first":
                body = body.replace('<table class="products">', f'{nxt}<table class="products">')
            if style == "upper":
                body = body.replace("<tr>", "<TR>").replace("</tr>", "</TR>").replace("</html>", "</HTML>").replace("</table>", "</TABLE>")
            if style == "nohead":
                body = body.replace("<thead>", "").replace("</thead>", "").replace("<tbody>", "").replace("</tbody>", "")
            if style == "comment":
                body = body.replace("</table>", "</table><!-- <table> generated -->")
            if style == "attrs":
                body = body.replace("<tr>", '<tr class="row" data-x="1">')
            return 200, {"Content-Type": "text/html; charset=utf-8"}, body

        out = records(adapter_for(route))
        assert codes(out) == [] and len(rows(out)) == n, (n, per, style, codes(out))
        done += 1
    assert done == 25


# ---- F3: a resume never skips rows by position over a source whose order can change ----------------------------------


def test_the_scraper_ignores_the_position_it_is_asked_to_resume_after_and_reads_from_the_first_page():
    adapter = adapter_for(catalog(7))
    out = list(adapter.records(start_after=4))
    assert [r.position for r in rows(out)] == list(range(1, 8)) and adapter.resumable is False


def test_a_resume_after_a_partial_run_over_a_site_that_moved_misses_nothing():
    """Part 1 was withdrawn between the runs, so every later part moved up one place: a skip by position would never read part 4."""
    supplier = snapshot_supplier("moved")
    state = {"parts": list(range(1, 10)), "blocked": False}

    def route(url):
        page = int(url.rsplit("page=", 1)[1])
        if page == 2 and state["blocked"]:
            return 403, {}, "no"
        parts = state["parts"]
        chunk = [part(i) for i in parts[(page - 1) * 3 : page * 3]]
        nxt = f'<a rel="next" href="/catalog?page={page + 1}">Next</a>' if page * 3 < len(parts) else ""
        return 200, {}, products_table(chunk, nxt=nxt)

    ingest(supplier, adapter_for(route))
    normalize_pending()
    match_pending()
    state["blocked"] = True
    assert ingest(supplier, adapter_for(route)).status == PipelineRun.Status.PARTIAL
    state["blocked"], state["parts"] = False, list(range(2, 10))
    run = ingest(supplier, adapter_for(route))
    assert run.status == PipelineRun.Status.SUCCEEDED and "resumed_from" not in run.notes
    live = sorted(PartSource.objects.filter(supplier=supplier, delisted_at__isnull=True).values_list("supplier_part_number", flat=True))
    assert live == sorted(part(i)[0] for i in range(2, 10))  # all eight still offered are still listed; only part 1 is gone
    assert run.notes["snapshot"]["delisted"] == 1


def test_a_partial_run_leaves_the_checkpoint_where_the_next_run_starts_over_not_where_it_stopped():
    supplier = get_supplier("again")
    ingest(supplier, adapter_for(catalog(9, overrides={2: (403, {}, "no")})))
    assert FeedCheckpoint.objects.get().position == 3
    run = ingest(supplier, adapter_for(catalog(9)))
    assert run.status == PipelineRun.Status.SUCCEEDED and run.rows_read == 9 and FeedCheckpoint.objects.get().position == 9
