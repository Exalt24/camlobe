"""How the scraper decides a page is a whole page and the walk is over, and what it does when a page is not what it looks like.

The walk ends as a success only when the SITE said it ended: the last page was whole (its product table and its html
were closed) and nothing was cut. Anything else is a coded gap, so the run is partial, delists nothing and never
completes the checkpoint. Pages here are served from memory (`PageSession`), so a body can be cut at any byte.
"""
import random
import re

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

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


# ---- F4: page text is decoded with the page's own declaration, never with ISO-8859-1 by default -----------------------

WORDS = ["Würth", "Bremsscheibe für Ø 280 mm", "Zubehör 5 € ab", "Łódź filtr", "日本語 部品", "plain ascii"]


def one_page(body_bytes, content_type="text/html"):
    return lambda url: (200, {"Content-Type": content_type}, body_bytes)


def stored_fields(route):
    out = records(adapter_for(route))
    assert codes(out) == []
    return rows(out)[0].fields


def page_bytes(brand, description, encoding, head="", bom=b""):
    html = products_table([("MF-1", brand, description, "12.50")], head=head)
    return bom + html.encode(encoding)


def test_a_utf8_page_with_no_charset_anywhere_is_read_as_utf8_and_not_as_latin1():
    fields = stored_fields(one_page(page_bytes("Würth", "Bremsscheibe für Ø 280 mm", "utf-8")))
    assert fields["Brand"] == "Würth" and fields["Description"] == "Bremsscheibe für Ø 280 mm"


def test_the_stored_row_of_a_utf8_page_without_a_charset_is_not_mojibake():
    run = ingest(get_supplier("u"), adapter_for(one_page(page_bytes("Würth", "Bremsscheibe für Ø 280 mm", "utf-8"))))
    stored = RawPart.objects.get()
    assert run.status == PipelineRun.Status.SUCCEEDED and (stored.brand_raw, stored.description_raw) == ("Würth", "Bremsscheibe für Ø 280 mm")


@pytest.mark.parametrize(
    "encoding, declared_in",
    [
        ("utf-8", "header"), ("utf-8", "meta"), ("utf-8", "bom"), ("utf-16", "bom"), ("utf-16", "header"),
        ("cp1252", "header"), ("cp1252", "meta"), ("cp1252", "meta_http_equiv"), ("iso-8859-2", "header"), ("shift_jis", "meta"),
    ],
)
def test_every_declaration_a_page_may_make_is_honoured_for_every_text(encoding, declared_in):
    """The class: whatever the text, the encoding and the way the page says which one it is, the stored text is the original."""
    for brand in WORDS:
        try:
            brand.encode(encoding)
        except UnicodeEncodeError:
            continue  # a text the encoding cannot hold is not a page in that encoding
        label = {"cp1252": "windows-1252", "utf-16": "utf-16", "utf-8": "utf-8"}.get(encoding, encoding)
        head, content_type, bom = "", "text/html", b""
        if declared_in == "header":
            content_type = f"text/html; charset={label}"
        elif declared_in == "meta":
            head = f'<meta charset="{label}">'
        elif declared_in == "meta_http_equiv":
            head = f'<meta http-equiv="Content-Type" content="text/html; charset={label}">'
        elif declared_in == "bom":
            bom = {"utf-8": b"\xef\xbb\xbf", "utf-16": b""}[encoding]
        body = page_bytes(brand, "x", encoding, head=head, bom=bom)
        if encoding == "utf-16" and declared_in == "bom":
            body = ("﻿" + products_table([("MF-1", brand, "x", "12.50")])).encode("utf-16")  # the codec writes the BOM
        assert stored_fields(one_page(body, content_type))["Brand"] == brand, (brand, encoding, declared_in)


def test_the_header_outranks_a_meta_tag_as_a_browser_does():
    body = page_bytes("Würth", "x", "utf-8", head='<meta charset="iso-8859-1">')
    assert stored_fields(one_page(body, "text/html; charset=utf-8"))["Brand"] == "Würth"


def test_a_page_that_declares_latin1_is_read_as_windows_1252_so_a_euro_sign_survives():
    body = page_bytes("Zubehör 5 €", "x", "cp1252", head='<meta charset="iso-8859-1">')
    assert stored_fields(one_page(body))["Brand"] == "Zubehör 5 €"


def test_a_page_that_cannot_be_decoded_is_a_coded_gap_and_nothing_is_stored():
    body = page_bytes("Würth", "x", "cp1252")  # legacy bytes, and the page says nothing: not UTF-8
    run = ingest(get_supplier("bad"), adapter_for(one_page(body)))
    assert run.status == PipelineRun.Status.PARTIAL and run.errors[0]["message"].startswith("E_PAGE_ENCODING")
    assert RawPart.objects.count() == 0


def test_a_meta_tag_that_says_utf16_cannot_be_right_because_it_was_read_as_ascii():
    body = page_bytes("Würth", "x", "utf-8", head='<meta charset="utf-16">')
    assert stored_fields(one_page(body))["Brand"] == "Würth"


def test_a_latin1_page_holding_a_byte_windows_1252_leaves_undefined_is_still_read():
    body = page_bytes("Würth", "x", "cp1252").replace(b"x</td>", b"x\x81</td>")  # 0x81 is a C1 control in latin-1
    out = records(adapter_for(one_page(body, "text/html; charset=iso-8859-1")))
    assert codes(out) == [] and rows(out)[0].fields["Brand"] == "Würth"


# ---- F5: a next link that is not a real next page is the end of the walk, not a loop ------------------------------------

NOT_A_PAGE = [
    ("#", ""), ("", ""), ("  ", ""), ("javascript:void(0)", ""), ("JavaScript:;", ""),
    ("/catalog?page=2", ""), ("?page=2", ""), ("/catalog?page=2#top", ""),  # the page itself, with or without a fragment
    ("/catalog?page=3", ' class="page-link disabled"'), ("/catalog?page=3", ' aria-disabled="true"'),
    ("/catalog?page=3", " disabled"), ("/catalog?page=3", ' class="next is-disabled"'),
]


def two_pages_with_last_next(href, attrs):
    def route(url):
        page = int(url.rsplit("page=", 1)[1].split("#")[0])
        link = f'<a rel="next" href="{href}"{attrs}>Next</a>' if page == 2 else '<a rel="next" href="/catalog?page=2">Next</a>'
        return 200, {}, products_table([part(page)], nxt=link)

    return route


@pytest.mark.parametrize("href, attrs", NOT_A_PAGE)
def test_a_next_link_that_is_no_page_ends_a_complete_run_as_a_success(href, attrs):
    run = ingest(get_supplier("last"), adapter_for(two_pages_with_last_next(href, attrs)))
    assert run.status == PipelineRun.Status.SUCCEEDED and run.rows_written == 2 and not run.errors


def test_a_link_to_a_different_page_already_read_is_still_a_loop():
    def route(url):
        page = int(url.rsplit("page=", 1)[1].split("#")[0])
        return 200, {}, products_table([part(page)], nxt=f'<a rel="next" href="/catalog?page={1 if page == 2 else 2}#top">Next</a>')

    run = ingest(get_supplier("loop"), adapter_for(route))
    assert run.status == PipelineRun.Status.PARTIAL and run.errors[0]["message"].startswith("E_PAGE_LOOP")


def test_a_fragment_on_the_real_next_link_is_dropped_before_the_page_is_asked_for_or_compared():
    seen = []

    def route(url):
        seen.append(url)
        page = int(url.rsplit("page=", 1)[1].split("#")[0])
        return 200, {}, products_table([part(page)], nxt=f'<a rel="next" href="/catalog?page={page + 1}#top">Next</a>' if page < 3 else "")

    out = records(adapter_for(route))
    assert codes(out) == [] and len(rows(out)) == 3 and all("#" not in url for url in seen)


def test_an_ordinary_next_link_is_still_followed():
    out = records(adapter_for(catalog(7)))
    assert codes(out) == [] and len(rows(out)) == 7


# ---- second output round: a pass is told apart from the next, a restarted walk is a new pass ---------------------------------


def priced(price_of, n=2, challenge=None):
    """A route of n one part pages whose first part costs `price_of()` today; `challenge()` true makes page 2 a bot challenge."""

    def route(url):
        page = int(url.rsplit("page=", 1)[1])
        if page == 2 and challenge and challenge():
            return 200, {}, "<html><body><h1>Checking your browser</h1></body></html>"
        row = ("MF-H0001A", "Marlow", "Oil filter 1", price_of()) if page == 1 else part(page)
        nxt = f'<a rel="next" href="/catalog?page={page + 1}">Next</a>' if page < n else ""
        return 200, {}, products_table([row], nxt=nxt)

    return route


def newest_cost(number="MF-H0001A"):
    return RawPart.objects.filter(part_number_raw=number).order_by("-id").values_list("cost_cents", flat=True).first()


def test_a_price_that_goes_10_12_10_over_three_walks_of_the_site_ends_on_10():
    price = {"now": "10.00"}
    supplier = get_supplier("flip")
    counts = []
    for today in ("10.00", "12.00", "10.00"):
        price["now"] = today
        run = ingest(supplier, adapter_for(priced(lambda: price["now"])))
        counts.append((run.status, run.rows_written, run.rows_unchanged))
    assert counts == [("succeeded", 2, 0), ("succeeded", 1, 1), ("succeeded", 1, 1)]
    assert list(RawPart.objects.filter(part_number_raw="MF-H0001A").order_by("id").values_list("cost_cents", flat=True)) == [1000, 1200, 1000]


def test_a_price_that_flips_back_between_two_partial_walks_is_still_the_newest_one():
    """Every walk of a live site starts again, so a partial walk is not a half of a pass: its keys are its own."""
    price = {"now": "10.00"}
    blocked = {"on": True}
    supplier = get_supplier("flip2")
    for today in ("10.00", "12.00", "10.00"):
        price["now"] = today
        run = ingest(supplier, adapter_for(priced(lambda: price["now"], challenge=lambda: blocked["on"])))
        assert run.status == PipelineRun.Status.PARTIAL
    assert newest_cost() == 1000 and RawPart.objects.filter(part_number_raw="MF-H0001A").count() == 3


def test_a_walk_read_again_after_a_partial_one_stores_nothing_twice():
    blocked = {"on": True}
    supplier = get_supplier("again")
    route = priced(lambda: "10.00", challenge=lambda: blocked["on"])
    first = ingest(supplier, adapter_for(route))
    blocked["on"] = False
    second = ingest(supplier, adapter_for(route))
    assert (first.rows_written, second.rows_written, second.rows_unchanged) == (1, 1, 1)
    assert RawPart.objects.count() == 2


# ---- F3: every error `requests` raises while a page is fetched or read is a coded outcome --------------------------------------


import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

import requests  # noqa: E402


# what retrying cannot change: the test's own statement of the split, not read from the adapter
CONFIG = tuple(
    getattr(requests.exceptions, name)
    for name in ("InvalidURL", "InvalidProxyURL", "InvalidSchema", "MissingSchema", "InvalidHeader", "InvalidJSONError", "JSONDecodeError",
                 "URLRequired", "UnrewindableBodyError", "TooManyRedirects")
)


def every_request_exception():
    """EVERY subclass of RequestException that exists in the installed `requests`, found by walking the class tree."""
    found, todo = set(), [requests.exceptions.RequestException]
    while todo:
        for sub in todo.pop().__subclasses__():
            if sub not in found:
                found.add(sub)
                todo.append(sub)
    return sorted(found | {requests.exceptions.RequestException}, key=lambda c: c.__name__)


def build_exception(cls):
    for args in ((), ("boom: http://proxyuser:Sup3rS3cret@proxy.example:3128",), ("msg", "doc", 0)):
        try:
            return cls(*args)
        except TypeError:
            continue
    raise AssertionError(f"cannot build {cls.__name__}")


class RaisingSession(PageSession):
    """Serves the route, and raises `exc` for the request at `page` (every try, or only the first `times`)."""

    def __init__(self, route, exc, page=1, times=None):
        super().__init__(route)
        self.exc, self.page, self.times, self.raised = exc, page, times, 0

    def request(self, method, url, **kwargs):
        if f"page={self.page}" in url and (self.times is None or self.raised < self.times):
            self.raised += 1
            self.log.append(url)
            raise self.exc
        return super().request(method, url, **kwargs)


def adapter_raising(exc, page=1, times=None, **changes):
    clock = Clock()
    base = dict(start_url=f"{BASE}/catalog?page=1", delay_seconds=0.0, respect_robots=False, max_retries=2, backoff_base=0.1, backoff_cap=1.0)
    base.update(changes)
    session = RaisingSession(catalog(6), exc, page, times)
    return ScrapeAdapter(ScrapeConfig(**base), session=session, sleep=clock.sleep, clock=clock.time, jitter=lambda d: d), session


@pytest.mark.parametrize("cls", every_request_exception(), ids=lambda c: c.__name__)
@pytest.mark.parametrize("page", [1, 2])
@pytest.mark.parametrize("proxied", [False, True], ids=["direct", "proxy"])
def test_every_exception_requests_can_raise_is_a_coded_gap_or_a_failure_with_a_fixed_phrase(cls, page, proxied):
    """The class (property over the subclasses): nothing `requests` raises escapes as an uncoded traceback, and no text of it is stored."""
    adapter, session = adapter_raising(build_exception(cls), page=page, **({"proxies": ["http://127.0.0.1:3128"]} if proxied else {}))
    out, failure = [], None
    try:
        for item in adapter.records():
            out.append(item)
    except FatalError as exc:
        failure = str(exc)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"{cls.__name__} escaped as {type(exc).__name__}: {exc}")
    if failure is not None:
        assert failure.startswith("E_REQUEST_INVALID: ") and "Sup3rS3cret" not in failure and "boom" not in failure
        assert issubclass(cls, CONFIG)
    else:
        assert not issubclass(cls, CONFIG)
        end = [g for g in out if isinstance(g, SourceGap)]
        assert len(end) == 1 and out[-1] is end[0] and re.match(r"E_[A-Z_]+: ", end[0].message)
        assert "Sup3rS3cret" not in end[0].message and "boom" not in end[0].message
        refused = issubclass(cls, (requests.exceptions.Timeout, requests.exceptions.ConnectionError))
        want = "E_PAGE_TRUNCATED" if issubclass(cls, requests.exceptions.ChunkedEncodingError) else (
            "E_PAGE_ENCODING" if issubclass(cls, requests.exceptions.ContentDecodingError) else (
                ("E_PROXY_UNAVAILABLE" if proxied else "E_UNREACHABLE") if refused else "E_TRANSPORT"))
        assert end[0].message.startswith(want + ": "), (cls.__name__, end[0].message)
        assert [r.fields["Part No"] for r in rows(out)] == [part(i)[0] for i in range(1, 4)][: 3 * (page - 1)]  # earlier pages are kept
        assert session.log.count(f"{BASE}/catalog?page={page}") == 3  # tried, then twice more with the backoff


class CutSite:
    """A site on 127.0.0.1 whose page 2 is cut after its headers: a short Content-Length body, or a chunked body that stops mid chunk."""

    def __init__(self, mode):
        self.mode, self.hits = mode, {1: 0, 2: 0}
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                page = int(self.path.rsplit("page=", 1)[1])
                site.hits[page] += 1
                if page == 1:
                    body = products_table([part(i) for i in (1, 2, 3)], nxt='<a rel="next" href="/catalog?page=2">Next</a>').encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = products_table([part(i) for i in (4, 5, 6)]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                if site.mode == "length":
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body[: len(body) // 2])
                else:
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    self.wfile.write(f"{len(body):x}\r\n".encode() + body[: len(body) // 2])
                self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(2)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.mark.parametrize("mode", ["length", "chunked"])
def test_a_body_cut_after_the_headers_is_retried_then_a_gap_that_keeps_the_pages_before_it(mode):
    site = CutSite(mode)
    try:
        clock = Clock()
        cfg = ScrapeConfig(start_url=f"{site.url}/catalog?page=1", delay_seconds=0.0, respect_robots=False, max_retries=2, backoff_base=0.1, backoff_cap=1.0)
        run = ingest(snapshot_supplier("cut"), ScrapeAdapter(cfg, sleep=clock.sleep, clock=clock.time, jitter=lambda d: d))
    finally:
        site.close()
    assert run.status == PipelineRun.Status.PARTIAL and run.errors[0]["message"].startswith("E_PAGE_TRUNCATED")
    assert "ChunkedEncodingError" in run.errors[0]["message"] and "IncompleteRead" not in run.errors[0]["message"]
    assert sorted(RawPart.objects.values_list("part_number_raw", flat=True)) == [part(i)[0] for i in (1, 2, 3)]
    assert site.hits == {1: 1, 2: 3}  # the cut page was asked for again with the existing backoff, twice
    assert not FeedCheckpoint.objects.get().completed and live_listings(snapshot_supplier("cut")) == 0


def test_a_cut_that_heals_on_the_retry_is_read_as_if_nothing_had_happened():
    adapter, session = adapter_raising(requests.exceptions.ChunkedEncodingError("cut"), page=2, times=1)
    out = records(adapter)
    assert codes(out) == [] and len(rows(out)) == 6 and adapter.notes()["retries"] == 1


# ---- F4: a link is resolved against the address the page was served from --------------------------------------------------------


def redirecting(pages, next_href="page/2/"):
    """/parts answers a trailing slash redirect, /parts/ is page 1 with a RELATIVE next link, /parts/page/2/ is page 2."""
    seen = []

    def route(url):
        seen.append(url)
        path = url.replace(BASE, "")
        if path == "/parts":
            return 301, {"Location": "/parts/"}, ""
        if path == "/parts/":
            return 200, {}, products_table([part(1), part(2)], nxt=f'<a rel="next" href="{next_href}">Next</a>' if pages > 1 else "")
        if path == "/parts/page/2/":
            return 200, {}, products_table([part(3), part(4)])
        return 404, {}, "<html><body>no</body></html>"

    return route, seen


def test_a_relative_next_link_after_a_trailing_slash_redirect_is_resolved_against_the_address_served():
    route, seen = redirecting(2)
    out = records(adapter_for(route, start_url=f"{BASE}/parts"))
    assert codes(out) == [] and len(rows(out)) == 4
    assert [u.replace(BASE, "") for u in seen] == ["/parts", "/parts/", "/parts/page/2/"]


def test_a_next_link_back_to_the_page_served_after_a_redirect_is_the_page_itself_and_ends_the_walk():
    """The same page under both of its addresses: /parts redirected to /parts/, and the pager's last link names either one."""
    for href in ("/parts/", "/parts", ""):
        route, _ = redirecting(2, next_href=href)
        out = records(adapter_for(route, start_url=f"{BASE}/parts"))
        assert codes(out) == [] and len(rows(out)) == 2, href


@pytest.mark.parametrize("back", ["/parts", "/parts/"])
def test_the_walk_notices_a_loop_through_a_redirect_by_either_name_of_the_first_page(back):
    def route(url):
        path = url.replace(BASE, "")
        if path == "/parts":
            return 301, {"Location": "/parts/"}, ""
        if path == "/parts/":
            return 200, {}, products_table([part(1)], nxt='<a rel="next" href="/parts/two/">Next</a>')
        if path == "/parts/two/":
            return 200, {}, products_table([part(2)], nxt=f'<a rel="next" href="{back}">Next</a>')  # back to the first page
        return 404, {}, ""

    out = records(adapter_for(route, start_url=f"{BASE}/parts"))
    assert codes(out) == ["E_PAGE_LOOP"] and len(rows(out)) == 2  # the first page is not read a second time


def test_a_redirect_that_lands_on_another_site_is_still_refused_and_a_next_link_there_too():
    def route(url):
        return 301, {"Location": "http://other.test/parts/"}, ""

    assert codes(records(adapter_for(route, start_url=f"{BASE}/parts"))) == ["E_OFFSITE"]


def based(base_tags, served="/parts/", next_href="page/2/"):
    """A page at `served` whose head holds `base_tags`; page 2 lives wherever the base says, under its own name."""
    seen = []

    def route(url):
        seen.append(url.replace(BASE, ""))
        path = url.replace(BASE, "")
        if path == served:
            return 200, {}, products_table([part(1), part(2)], head=base_tags, nxt=f'<a rel="next" href="{next_href}">Next</a>')
        if path.endswith("/page/2/"):
            return 200, {}, products_table([part(3), part(4)])
        return 404, {}, "<html><body>no</body></html>"

    return route, seen


@pytest.mark.parametrize(
    "tags, second",
    [
        ('<base href="/shop/">', "/shop/page/2/"),
        ('<base href="/shop/"><base href="/other/">', "/shop/page/2/"),  # the first one wins, the rest are ignored
        ('<base target="_blank"><base href="/shop/">', "/shop/page/2/"),  # a base with no href does not count
        ("", "/parts/page/2/"),  # no base: the address the page was served from
        ('<base href="sub/">', "/parts/sub/page/2/"),  # a relative base is resolved against the page
    ],
)
def test_a_base_element_with_an_href_is_the_base_of_the_next_link_and_the_first_one_wins(tags, second):
    route, seen = based(tags)
    out = records(adapter_for(route, start_url=f"{BASE}/parts/"))
    assert codes(out) == [] and len(rows(out)) == 4 and seen == ["/parts/", second]


def test_a_base_element_after_a_redirect_is_resolved_against_the_address_served():
    seen = []

    def route(url):
        path = url.replace(BASE, "")
        seen.append(path)
        if path == "/parts":
            return 301, {"Location": "/parts/"}, ""
        if path == "/parts/":
            return 200, {}, products_table([part(1)], head='<base href="more/">', nxt='<a rel="next" href="page/2/">Next</a>')
        if path == "/parts/more/page/2/":
            return 200, {}, products_table([part(2)])
        return 404, {}, ""

    assert codes(records(adapter_for(route, start_url=f"{BASE}/parts"))) == [] and seen[-1] == "/parts/more/page/2/"


# ---- F5, F6, F7: the table's own rows and cells, a th in a body row, a spanned cell, a second header row ------------------------


def table_page(html):
    return adapter_for(lambda url: (200, {}, f'<html><head></head><body><table class="products">{html}</table></body></html>'))


def read_table(html):
    out = records(table_page(html))
    assert codes(out) == [], codes(out)
    return [(r.reject, r.fields) for r in rows(out)]


HEAD4 = "<tr><th>Part No</th><th>Brand</th><th>Description</th><th>Price</th></tr>"
ROW = lambda n, price="10.00": f"<tr><td>MF-{n}</td><td>Marlow</td><td>Filter {n}</td><td>{price}</td></tr>"
WANT = lambda n, price="10.00": {"Part No": f"MF-{n}", "Brand": "Marlow", "Description": f"Filter {n}", "Price": price}
BREAKS = '<table class="breaks"><tr><td>1-9</td><td>$10.00</td></tr><tr><td>10+</td><td>$9.00</td></tr></table>'


def test_a_table_nested_in_a_cell_is_the_text_of_that_cell_and_its_rows_are_not_rows_of_the_product_table():
    html = (
        "<thead><tr><th>Part No</th><th>Brand</th><th>Description</th><th>Price</th><th>Qty breaks</th></tr></thead><tbody>"
        f"<tr><td>MF-1</td><td>Marlow</td><td>Oil filter</td><td>10.00</td><td>{BREAKS}</td></tr>"
        "<tr><td>MF-2</td><td>Marlow</td><td>Air filter</td><td>5.00</td><td>none</td></tr></tbody>"
    )
    got = read_table(html)
    assert [f["Part No"] for _, f in got] == ["MF-1", "MF-2"] and all(reject == "" for reject, _ in got)
    assert got[0][1]["Qty breaks"] == "1-9 $10.00 10+ $9.00"


def test_a_table_nested_in_the_header_or_with_no_tbody_is_still_one_table():
    html = f"<tr><th>Part No</th><th>Brand</th><th>Description</th><th>Price</th></tr><tr><td>MF-1</td><td>Marlow</td><td>x</td><td>{BREAKS}</td></tr>"
    got = read_table(html)
    assert len(got) == 1 and got[0][0] == "" and got[0][1]["Price"] == "1-9 $10.00 10+ $9.00"


def test_a_part_number_in_a_th_scope_row_is_a_cell_like_any_other():
    html = (
        '<thead><tr><th scope="col">Part No</th><th scope="col">Brand</th><th scope="col">Description</th><th scope="col">Price</th></tr></thead>'
        '<tbody><tr><th scope="row">MF-1</th><td>Marlow</td><td>Filter 1</td><td>10.00</td></tr>'
        '<tr><th scope="row">MF-2</th><td>Marlow</td><td>Filter 2</td><td>10.00</td></tr></tbody>'
    )
    assert read_table(html) == [("", WANT(1)), ("", WANT(2))]


def test_a_th_only_row_in_the_body_is_a_heading_not_a_part():
    html = f"<thead>{HEAD4}</thead><tbody>{ROW(1)}<tr><th colspan=\"4\">Brake parts</th></tr>{ROW(2)}</tbody>"
    assert read_table(html) == [("", WANT(1)), ("", WANT(2))]


def test_a_header_cell_with_colspan_counts_as_that_many_columns_and_a_body_cell_with_colspan_fills_them():
    html = (
        '<thead><tr><th>Part No</th><th>Brand</th><th>Description</th><th colspan="2">Price</th></tr></thead><tbody>'
        "<tr><td>MF-1</td><td>Marlow</td><td>Oil filter</td><td>10.00</td><td><button>Add</button></td></tr>"
        '<tr><td>MF-2</td><td>Marlow</td><td colspan="3">Air filter, no price listed</td></tr></tbody>'
    )
    got = read_table(html)
    assert got[0] == ("", {"Part No": "MF-1", "Brand": "Marlow", "Description": "Oil filter", "Price": "10.00", "Price (2)": "Add"})
    assert got[1][0] == "" and got[1][1]["Description"] == "Air filter, no price listed" and got[1][1]["Price"] == ""


@pytest.mark.parametrize("filter_first", [False, True])
def test_a_second_header_row_such_as_a_filter_row_is_neither_data_nor_a_second_header(filter_first):
    labels = HEAD4
    filters = "<tr><th><input name=\"q1\"></th><th><input name=\"q2\"></th><th><input name=\"q3\"></th><th></th></tr>"
    thead = (filters + labels) if filter_first else (labels + filters)
    assert read_table(f"<thead>{thead}</thead><tbody>{ROW(1)}{ROW(2)}</tbody>") == [("", WANT(1)), ("", WANT(2))]


ORDINARY_TABLES = {
    "thead and tbody": f"<thead>{HEAD4}</thead><tbody>{ROW(1)}{ROW(2)}</tbody>",
    "no thead, th first row": f"{HEAD4}{ROW(1)}{ROW(2)}",
    "header row inside tbody": f"<tbody>{HEAD4}{ROW(1)}{ROW(2)}</tbody>",
    "td header row": f"<tr><td>Part No</td><td>Brand</td><td>Description</td><td>Price</td></tr>{ROW(1)}{ROW(2)}",
    "tfoot after tbody": f"<thead>{HEAD4}</thead><tbody>{ROW(1)}{ROW(2)}</tbody><tfoot><tr><td colspan=\"4\">2 parts</td></tr></tfoot>",
    "empty cell": f"<thead>{HEAD4}</thead><tbody>{ROW(1)}{ROW(2, '')}</tbody>",
    "upper case tags": re.sub(r"</?[a-z]+", lambda m: m.group(0).upper(), f"<thead>{HEAD4}</thead><tbody>{ROW(1)}{ROW(2)}</tbody>"),
    "attributes and links": '<thead class="x">' + HEAD4.replace("<th>", '<th scope="col" class="h">') + '</thead><tbody id="b">' + ROW(1).replace("<td>", '<td class="c">') + ROW(2) + "</tbody>",
    "unclosed cells and rows": "<thead><tr><th>Part No<th>Brand<th>Description<th>Price</thead><tbody><tr><td>MF-1<td>Marlow<td>Filter 1<td>10.00<tr><td>MF-2<td>Marlow<td>Filter 2<td>10.00</tbody>",
}


@pytest.mark.parametrize("name", list(ORDINARY_TABLES))
def test_the_ordinary_table_shapes_read_exactly_as_before(name):
    want = [("", WANT(1)), ("", WANT(2, "" if name == "empty cell" else "10.00"))]
    assert read_table(ORDINARY_TABLES[name]) == want


@given(
    st.integers(2, 6),  # columns
    st.integers(1, 5),  # rows
    st.booleans(),  # a thead
    st.booleans(),  # a tbody around the rows
    st.booleans(),  # a filter row after the labels
    st.booleans(),  # the first cell of a body row is a th scope=row
    st.integers(-1, 5),  # the column whose header spans two columns (-1: none)
    st.integers(-1, 4),  # the row of the column under which a table is nested (-1: none)
    st.data(),
)
@hyp_settings(max_examples=120, deadline=None)
def test_any_mix_of_table_structure_reads_as_the_grid_it_describes(cols, nrows, thead, tbody, filter_row, row_th, span_at, nest_row, data):
    """The class: table structure read by recursive find_all instead of the table's own rows and cells. Whatever combination of
    a thead, a filter row, row header cells, a spanned header and a nested table the page uses, the records are the grid."""
    header = [f"Col{c}" for c in range(cols)]
    grid = [[f"r{r}c{c}" for c in range(cols)] for r in range(nrows)]
    span_at = span_at if 0 <= span_at < cols - 1 else -1  # the spanned header covers the column and the one after it
    head_cells, c = [], 0
    while c < cols:
        if c == span_at:
            head_cells.append(f'<th colspan="2">{header[c]}</th>')
            c += 2
        else:
            head_cells.append(f"<th>{header[c]}</th>")
            c += 1
    expected_header = list(header)
    if span_at >= 0:
        expected_header[span_at + 1] = f"{header[span_at]} (2)"
    labels = "<tr>" + "".join(head_cells) + "</tr>"
    filters = "<tr>" + "".join('<th><input name="f"></th>' for _ in head_cells) + "</tr>"
    body, want = [], []
    for r, values in enumerate(grid):
        cells, wanted = [], list(values)
        for c, value in enumerate(values):
            inner = value
            if r == nest_row and c == cols - 1:
                inner += '<table class="inner"><tr><td>a</td><td>b</td></tr></table>'
                wanted[c] = f"{value} a b"
            tag = "th" if (row_th and c == 0) else "td"
            cells.append(f'<{tag}{" scope=\"row\"" if tag == "th" else ""}>{inner}</{tag}>')
        body.append("<tr>" + "".join(cells) + "</tr>")
        want.append(dict(zip(expected_header, wanted)))
    rows_html = f"<tbody>{''.join(body)}</tbody>" if tbody else "".join(body)
    head = f"<thead>{labels}{filters if filter_row else ''}</thead>{rows_html}" if thead else labels + rows_html
    got = read_table(head)
    assert [fields for _, fields in got] == want and all(reject == "" for reject, _ in got)


# ---- F11: rel is a set of case insensitive tokens ------------------------------------------------------------------------------


def rel_site(rel):
    def route(url):
        page = int(url.rsplit("page=", 1)[1])
        nxt = f'<a rel="{rel}" href="/catalog?page={page + 1}">Next</a>' if page < 2 else ""
        return 200, {}, products_table([part(page)], nxt=nxt)

    return route


@pytest.mark.parametrize(
    "rel, follows",
    [
        ("next", True), ("NEXT", True), ("Next", True), ("nofollow next", True), ("next nofollow", True), ("noopener  next noreferrer", True),
        ("nofollow\tNext", True), (" next ", True), ("nofollow\nnext", True),
        ("nextpage", False), ("unnext", False), ("next-page", False), ("prev", False), ("nofollow", False), ("", False), ("prev nofollow", False),
    ],
)
def test_a_rel_value_matches_the_next_link_by_its_tokens_and_ignoring_case(rel, follows):
    out = records(adapter_for(rel_site(rel)))
    assert codes(out) == [] and len(rows(out)) == (2 if follows else 1)


def test_the_default_next_selector_still_matches_a_plain_rel_next_and_a_configured_one_is_used_as_written():
    assert len(rows(records(adapter_for(rel_site("next"))))) == 2
    assert len(rows(records(adapter_for(rel_site("nofollow next"), next_selector="a[rel=next]")))) == 1  # exactly what the config says


# ---- F8: a page that has the product table is a data page, whatever else is on it ---------------------------------------------


SIGN_IN_BOX = '<header><form action="/account/login" method="post" class="mini-login"><input name="email"><input type="password" name="pass"><button>Dealer sign in</button></form></header>'


class MethodSession(PageSession):
    """A PageSession whose route also sees the method and the form: route(method, url, data) -> (status, headers, body)."""

    def request(self, method, url, **kwargs):
        self.log.append((method, url))
        status, headers, body = self.route(method, url, kwargs.get("data"))
        response = requests.Response()
        response.status_code, response.url = status, url
        response._content = body.encode() if isinstance(body, str) else body
        response.headers.update(headers)
        response.encoding = "utf-8"
        return response


def with_box(html):
    return html.replace("<body>", "<body>" + SIGN_IN_BOX, 1)


@pytest.mark.parametrize("login_configured", [False, True])
def test_a_sign_in_box_in_the_header_of_an_open_catalog_does_not_make_it_a_login_wall(login_configured):
    def route(url):
        return 200, {}, with_box(products_table([part(1), part(2)]))

    extra = dict(login_url=f"{BASE}/login", username_env="U", password_env="P") if login_configured else {}
    clock = Clock()
    cfg = ScrapeConfig(start_url=f"{BASE}/catalog?page=1", delay_seconds=0.0, respect_robots=False, **extra)
    adapter = ScrapeAdapter(cfg, session=PageSession(route), environ={"U": "buyer", "P": "pw"}, sleep=clock.sleep, clock=clock.time)
    out = records(adapter)
    assert codes(out) == [] and len(rows(out)) == 2 and adapter.notes()["logins"] == 0


def test_a_password_field_and_no_product_table_is_still_a_login_wall():
    page = "<html><body><form action=\"/login\"><input type=\"password\" name=\"pw\"></form></body></html>"
    assert codes(records(adapter_for(lambda url: (200, {}, page)))) == ["E_LOGIN_REQUIRED"]


def test_an_expired_session_page_with_a_password_field_and_no_table_is_logged_in_again_even_when_the_site_shows_a_box_elsewhere():
    login_page = '<html><body><form action="/login" method="post"><input type="hidden" name="csrf_token" value="t"><input name="username"><input type="password" name="password"></form></body></html>'
    state = {"in": False, "gets": 0}

    def route(method, url, data):
        if url.endswith("/login") and method == "GET":
            return 200, {}, login_page
        if url.endswith("/login") and method == "POST":
            state["in"] = True
            return 200, {}, with_box(products_table([part(1)], nxt='<a rel="next" href="/catalog?page=2">Next</a>'))  # lands on page 1 with the box
        if "page=2" in url and state["gets"] == 0:
            state["gets"] += 1
            state["in"] = False
            return 200, {}, login_page  # the session died: page 2 is the login form
        if not state["in"]:
            return 200, {}, login_page
        page = int(url.rsplit("page=", 1)[1])
        return 200, {}, with_box(products_table([part(page)], nxt='<a rel="next" href="/catalog?page=2">Next</a>' if page == 1 else ""))

    clock = Clock()
    cfg = ScrapeConfig(start_url=f"{BASE}/catalog?page=1", login_url=f"{BASE}/login", username_env="U", password_env="P", delay_seconds=0.0, respect_robots=False)
    adapter = ScrapeAdapter(cfg, session=MethodSession(route), environ={"U": "buyer", "P": "pw"}, sleep=clock.sleep, clock=clock.time)
    out = records(adapter)
    assert codes(out) == [] and [r.fields["Part No"] for r in rows(out)] == [part(1)[0], part(2)[0]]
    assert adapter.notes()["logins"] == 2 and adapter.notes()["relogins"] == 1


# ---- F12: robots.txt answering a redirect (RFC 9309 2.3.1.2: follow up to five, apply the rules to the initial site) --------------


DISALLOW = "User-agent: *\nDisallow: /catalog\n"


def robots_route(robots, other_hosts=()):
    """`robots(path)` answers requests for /robots.txt and the files it redirects to; every other URL is the catalog."""
    seen = []

    def route(url):
        seen.append(url)
        parts = url.split("/", 3)
        host, path = parts[2], "/" + (parts[3] if len(parts) > 3 else "")
        if path.startswith("/catalog"):
            return 200, {}, products_table([part(1), part(2)])
        return robots(host, path)

    return route, seen


def run_with_robots(robots):
    route, seen = robots_route(robots)
    adapter = adapter_for(route, respect_robots=True)
    out = records(adapter)
    return out, seen, adapter.notes()


def chain(n, final, host="shop.test"):
    """robots.txt -> /r1 -> ... -> /r<n> redirects, then `final` at /r<n> ... (n hops in all)."""

    def robots(h, path):
        step = 0 if path == "/robots.txt" else int(path[2:])
        if step < n:
            return 301, {"Location": f"/r{step + 1}"}, ""
        return final

    return robots


@pytest.mark.parametrize("hops", [1, 2, 5])
def test_a_robots_txt_redirect_is_followed_up_to_five_times_and_its_rules_apply_to_the_site(hops):
    out, seen, notes = run_with_robots(chain(hops, (200, {}, DISALLOW)))
    assert codes(out) == ["E_ROBOTS"] and rows(out) == [] and notes["robots"] == "read"
    assert not any("/catalog" in u for u in seen) and len(seen) == hops + 1


def test_a_robots_txt_that_redirects_to_a_file_that_allows_everything_lets_the_walk_through():
    out, seen, notes = run_with_robots(chain(1, (200, {}, "User-agent: *\nDisallow: /private\n")))
    assert codes(out) == [] and len(rows(out)) == 2 and notes["robots"] == "read"


def test_more_than_five_redirects_for_robots_txt_count_as_unavailable_and_the_walk_goes_on():
    out, seen, notes = run_with_robots(chain(6, (200, {}, DISALLOW)))
    assert codes(out) == [] and len(rows(out)) == 2 and notes["robots"].startswith("none")
    assert len([u for u in seen if "/r" in u or "robots" in u]) == 6  # the first file and five redirects, no more


def test_a_robots_txt_that_redirects_to_itself_is_unavailable_not_a_loop_without_end():
    out, seen, notes = run_with_robots(lambda host, path: (302, {"Location": "/robots.txt"}, ""))
    assert codes(out) == [] and notes["robots"].startswith("none") and len(seen) <= 8


def test_a_robots_txt_redirect_may_leave_the_host_and_the_rules_found_there_apply_to_the_initial_site():
    def robots(host, path):
        if host == "shop.test":
            return 301, {"Location": "https://static.example/robots.txt"}, ""
        return 200, {}, DISALLOW

    out, seen, _ = run_with_robots(robots)
    assert codes(out) == ["E_ROBOTS"] and seen == [f"{BASE}/robots.txt", "https://static.example/robots.txt"]


def test_the_status_a_redirected_robots_txt_ends_on_is_read_as_for_any_robots_txt():
    allowed = run_with_robots(chain(1, (404, {}, "not here")))
    assert codes(allowed[0]) == [] and allowed[2]["robots"] == "none (HTTP 404)"  # a 4xx: unavailable, anything may be fetched
    server_error = run_with_robots(chain(1, (500, {}, "oops")))
    assert codes(server_error[0]) == ["E_ROBOTS_UNAVAILABLE"] and rows(server_error[0]) == []  # a 5xx: everything is disallowed


def test_a_robots_txt_redirect_with_no_location_is_unavailable():
    out, _, notes = run_with_robots(lambda host, path: (301, {}, ""))
    assert codes(out) == [] and len(rows(out)) == 2 and notes["robots"].startswith("none")
