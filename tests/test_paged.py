"""PagedAdapter: backoff, Retry-After, rate limiting, gaps, resume offsets, short pages. No network."""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from catalog.ingest.adapters import SourceGap, SourceRecord
from catalog.ingest.paged import FatalError, PagedAdapter, PermanentError, TransientError, flatten


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Scripted(PagedAdapter):
    """Serves pages from `items`; `outcomes[offset]` is a list of exceptions to raise first."""

    def __init__(self, items, outcomes=None, total=True, **kw):
        clock = kw.pop("clock", None) or Clock()
        super().__init__(
            "scripted", sleep=clock.sleep, clock=clock.time, jitter=kw.pop("jitter", lambda d: d), **kw
        )
        self.clock = clock
        self.items = items
        self.outcomes = {k: list(v) for k, v in (outcomes or {}).items()}
        self.give_total = total
        self.calls = []

    def fetch_page(self, offset, limit):
        self.calls.append((offset, self.clock.now))
        queue = self.outcomes.get(offset)
        if queue:
            exc = queue.pop(0)
            if exc is not None:
                raise exc
        return self.items[offset : offset + limit], (len(self.items) if self.give_total else None)


def items(n):
    return [{"sku": f"AB{i:04d}"} for i in range(n)]


def read(adapter, start=0):
    return list(adapter.records(start_after=start))


def positions(out):
    return [r.position for r in out if isinstance(r, SourceRecord)]


def test_backoff_doubles_and_stops_at_the_cap():
    a = Scripted(items(10), {0: [TransientError("503")] * 5}, page_size=10, max_retries=5, backoff_base=0.5, backoff_cap=3.0)
    assert positions(read(a)) == list(range(1, 11))
    assert a.clock.sleeps == [0.5, 1.0, 2.0, 3.0, 3.0]
    assert a.notes()["retries"] == 5 and a.notes()["requests"] == 6


def test_jitter_is_applied_to_the_backoff_but_never_beyond_it():
    a = Scripted(items(5), {0: [TransientError("x")] * 3}, page_size=5, backoff_base=1.0, jitter=lambda d: d / 2)
    read(a)
    assert a.clock.sleeps == [0.5, 1.0, 2.0]


@pytest.mark.parametrize(
    "retry_after,expected",
    [(7.0, 7.0), (30.0, 10.0), (0.1, 0.5)],  # honoured, capped, and never shorter than the backoff
)
def test_retry_after_is_honoured_within_the_cap(retry_after, expected):
    a = Scripted(items(5), {0: [TransientError("429", retry_after=retry_after)]}, page_size=5, backoff_base=0.5, backoff_cap=10.0)
    read(a)
    assert a.clock.sleeps == [expected]


def test_a_page_that_keeps_failing_becomes_a_gap_and_the_rest_is_still_read():
    a = Scripted(items(30), {10: [TransientError("503")] * 99}, page_size=10, max_retries=2)
    out = read(a)
    assert positions(out) == list(range(1, 11)) + list(range(21, 31))
    gaps = [r for r in out if isinstance(r, SourceGap)]
    assert [(g.first_position, g.last_position) for g in gaps] == [(11, 20)]
    assert "gave up after 3 attempts" in gaps[0].message
    assert a.notes()["requests"] == 5 and a.notes()["retries"] == 2  # 3 attempts on the bad page, 2 pages fine
    assert len(a.clock.sleeps) == 2  # it does not wait again after the last attempt
    assert a.notes()["failed_pages"] == [10]


def test_a_permanent_error_is_a_gap_without_a_retry():
    a = Scripted(items(30), {10: [PermanentError("HTTP 404")]}, page_size=10)
    out = read(a)
    assert positions(out) == list(range(1, 11)) + list(range(21, 31))
    assert a.notes()["retries"] == 0 and a.clock.sleeps == []


def test_a_fatal_error_stops_everything_at_once():
    a = Scripted(items(30), {0: [FatalError("HTTP 401")]}, page_size=10)
    with pytest.raises(FatalError):
        read(a)
    assert a.notes()["requests"] == 1


def test_three_failed_pages_in_a_row_mean_the_endpoint_is_down():
    a = Scripted(items(100), {o: [PermanentError("404")] * 9 for o in (10, 20, 30)}, page_size=10)
    with pytest.raises(FatalError, match="3 pages in a row"):
        read(a)


def test_a_success_resets_the_run_of_failures():
    outcomes = {o: [PermanentError("404")] * 9 for o in (10, 20, 40, 50)}
    out = read(Scripted(items(100), outcomes, page_size=10))
    assert len([r for r in out if isinstance(r, SourceGap)]) == 4


def test_requests_are_spaced_by_the_rate_limit():
    a = Scripted(items(50), page_size=10, rate_limit_per_sec=2.0)
    read(a)
    times = [t for _, t in a.calls]
    assert all(b - a_ >= 0.5 - 1e-9 for a_, b in zip(times, times[1:]))
    assert a.notes()["throttle_waits"] == 4


def test_no_rate_limit_means_no_waiting():
    a = Scripted(items(50), page_size=10)
    read(a)
    assert a.clock.sleeps == [] and a.notes()["throttle_waits"] == 0


def test_a_slow_caller_is_not_made_to_wait_again():
    a = Scripted(items(20), page_size=10, rate_limit_per_sec=2.0)
    gen = a.records()
    next(gen)
    a.clock.now += 5.0  # the consumer was busy for longer than the interval
    list(gen)
    assert a.notes()["throttle_waits"] == 0


@pytest.mark.parametrize("n,page", [(25, 10), (30, 10), (1, 10), (10, 10), (0, 10)])
def test_every_item_is_yielded_once_for_any_set_size(n, page):
    out = read(Scripted(items(n), page_size=page))
    assert positions(out) == list(range(1, n + 1))
    assert [r.fields["sku"] for r in out] == [f"AB{i:04d}" for i in range(n)]


def test_without_a_total_an_exact_multiple_ends_on_the_empty_page():
    a = Scripted(items(30), page_size=10, total=False)
    assert positions(read(a)) == list(range(1, 31))
    assert a.notes()["requests"] == 4


def test_a_page_capped_below_what_was_asked_stops_the_run_instead_of_skipping_rows():
    class Capped(Scripted):
        def fetch_page(self, offset, limit):
            return super().fetch_page(offset, min(limit, 4))

    with pytest.raises(FatalError, match="caps the page size"):
        read(Capped(items(30), page_size=10))


def test_resume_starts_at_the_page_that_holds_the_checkpoint():
    a = Scripted(items(50), page_size=10)
    out = read(a, start=23)
    assert positions(out) == list(range(24, 51))
    assert a.calls[0][0] == 20


@given(st.integers(0, 120), st.integers(1, 17), st.data())
@settings(max_examples=150, deadline=None)
def test_any_size_any_page_any_resume_point_yields_exactly_the_rest(n, page, data):
    start = data.draw(st.integers(0, n))
    out = read(Scripted(items(n), page_size=page), start=start)
    assert positions(out) == list(range(start + 1, n + 1))
    assert [r.fields["sku"] for r in out] == [f"AB{i:04d}" for i in range(start, n)]


@given(st.integers(2, 80), st.integers(1, 9), st.sets(st.integers(0, 79), max_size=3))
@settings(max_examples=100, deadline=None)
def test_gaps_name_exactly_the_positions_that_are_missing(n, page, bad_pages):
    bad = {p * page for p in bad_pages if p * page < n}
    outcomes = {o: [PermanentError("404")] * 9 for o in bad}
    try:
        out = read(Scripted(items(n), outcomes, page_size=page))
    except FatalError:
        return  # three failed pages in a row: covered above
    seen = set(positions(out))
    missing = set()
    for g in (r for r in out if isinstance(r, SourceGap)):
        missing |= set(range(g.first_position, g.last_position + 1))
    assert seen | missing == set(range(1, n + 1)) and not (seen & missing)


def test_flatten_nested_and_none():
    assert flatten({"a": 1, "b": None, "c": {"d": "x", "e": {"f": 2}}}) == {"a": "1", "b": "", "c.d": "x", "c.e.f": "2"}
