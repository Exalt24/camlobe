"""Paged remote sources (REST, SOAP): rate limit, retry with backoff, and partial failure.

The base class owns everything that is the same for every paged API, and a subclass only says how to
fetch one page. The rules:

  - requests are spaced at least 1 / rate_limit_per_sec apart (an injected clock and sleep make it testable);
  - a timeout, a connection error, a 429 or a 5xx is retried with exponential backoff, a Retry-After
    from the server is honoured (never longer than the cap), and attempts are counted;
  - a page that still fails after its retries, or a 4xx that retrying cannot fix, becomes a SourceGap and
    the next page is still read, so one bad page does not lose the others. The run is reported partial and
    the checkpoint stops at the gap;
  - a 401 or 403 stops the run at once, because every page would fail the same way;
  - three failed pages in a row mean the endpoint is down, and the run stops instead of producing gaps forever;
  - a page shorter than asked, in the middle of a set whose total is known, stops the run: the server capped
    the page size, and carrying on would skip rows silently;
  - when the total is NOT known, a short page proves nothing (the server may cap its pages), so paging goes on,
    each request starting where the last answer ended, until a page comes back empty;
  - a page whose items are exactly those of a page already read in this run (a server that answers every offset
    with page 0, a cache in front of it) is a gap coded E_PAGE_REPEATED: its rows are not stored a second time and
    the rows that belong at that offset are missing, so the run must not look complete.
"""
from __future__ import annotations

import hashlib
import json
import random
import time
from typing import Callable, Iterator

from .adapters import SourceGap, SourceRecord

MAX_CONSECUTIVE_FAILED_PAGES = 3
MIN_ITEMS_TO_CALL_A_REPEAT = 2  # a one item page can match an earlier one when a feed lists the same row twice
MAX_PAGES = 100_000  # with no total, a server that never answers an empty page would otherwise be read for ever


class TransientError(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(Exception):
    """This page cannot be fetched and retrying will not help."""


class FatalError(Exception):
    """Nothing from this source will work (bad credentials, an endpoint that is down, a capped page size)."""


class PageFailed(Exception):
    pass


def page_identity(items: list[dict]) -> str:
    return hashlib.sha256(json.dumps(items, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()


def flatten(item: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in item.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(value, f"{name}."))
        elif value is None:
            out[name] = ""
        else:
            out[name] = str(value)
    return out


class PagedAdapter:
    def __init__(
        self,
        source_ref: str,
        page_size: int = 100,
        max_retries: int = 4,
        backoff_base: float = 0.5,
        backoff_cap: float = 10.0,
        rate_limit_per_sec: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[float], float] | None = None,
    ):
        self.source_ref = source_ref
        self.page_size = page_size
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.min_interval = 1.0 / rate_limit_per_sec if rate_limit_per_sec else 0.0
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter or (lambda d: random.uniform(d / 2, d))
        self._last_request: float | None = None
        self._stats = {"requests": 0, "retries": 0, "throttle_waits": 0, "failed_pages": []}

    # subclasses implement this
    def fetch_page(self, offset: int, limit: int) -> tuple[list[dict], int | None]:
        raise NotImplementedError

    def fingerprint(self) -> str:
        return ""  # a live API has no content identity: every run reads it again

    def notes(self) -> dict:
        return {**self._stats, "failed_pages": list(self._stats["failed_pages"])}

    def _throttle(self) -> None:
        if self.min_interval and self._last_request is not None:
            wait = self.min_interval - (self._clock() - self._last_request)
            if wait > 0:
                self._stats["throttle_waits"] += 1
                self._sleep(wait)
        self._last_request = self._clock()

    def _fetch_with_retry(self, offset: int) -> tuple[list[dict], int | None]:
        last = ""
        for attempt in range(self.max_retries + 1):
            self._throttle()
            self._stats["requests"] += 1
            try:
                return self.fetch_page(offset, self.page_size)
            except TransientError as exc:
                last = str(exc)
                if attempt == self.max_retries:
                    break
                self._stats["retries"] += 1
                delay = min(self.backoff_cap, self.backoff_base * 2**attempt)
                if exc.retry_after is not None:
                    delay = min(self.backoff_cap, max(delay, exc.retry_after))
                else:
                    delay = self._jitter(delay)
                self._sleep(delay)
            except PermanentError as exc:
                raise PageFailed(f"offset {offset}: {exc}") from exc
        raise PageFailed(f"offset {offset}: {last} (gave up after {self.max_retries + 1} attempts)")

    def records(self, start_after: int = 0) -> Iterator[SourceRecord | SourceGap]:
        offset = (start_after // self.page_size) * self.page_size
        total: int | None = None
        failed_in_a_row = 0
        seen_pages: dict[str, int] = {}  # page identity -> the offset it was first read at
        pages = 0
        while total is None or offset < total:
            pages += 1
            if pages > MAX_PAGES:
                raise FatalError(f"more than {MAX_PAGES} pages and no end: the source does not stop")
            try:
                items, page_total = self._fetch_with_retry(offset)
            except PageFailed as exc:
                failed_in_a_row += 1
                self._stats["failed_pages"].append(offset)
                if failed_in_a_row >= MAX_CONSECUTIVE_FAILED_PAGES:
                    raise FatalError(f"{failed_in_a_row} pages in a row failed; the endpoint looks down: {exc}") from exc
                last = offset + self.page_size if total is None else min(total, offset + self.page_size)
                yield SourceGap(offset + 1, last, str(exc))
                offset += self.page_size
                continue
            if page_total is not None:
                total = page_total
            identity = page_identity(items) if len(items) >= MIN_ITEMS_TO_CALL_A_REPEAT else ""
            if identity and identity in seen_pages:
                message = (
                    f"E_PAGE_REPEATED: the page at offset {offset} is the page read at offset {seen_pages[identity]} again, "
                    f"so the rows that belong at {offset + 1} and after are missing"
                )
                self._stats["failed_pages"].append(offset)
                if total is None:
                    raise FatalError(f"{message}; with no total the end of the set cannot be found")
                failed_in_a_row += 1
                if failed_in_a_row >= MAX_CONSECUTIVE_FAILED_PAGES:
                    raise FatalError(f"{failed_in_a_row} pages in a row failed; the endpoint looks down: {message}")
                yield SourceGap(offset + 1, min(total, offset + self.page_size), message)
                offset += self.page_size
                continue
            failed_in_a_row = 0  # a page that is new, whatever fetch or repeat came before it
            if identity:
                seen_pages[identity] = offset
            short = len(items) < self.page_size
            if short and total is not None and offset + len(items) < total:
                raise FatalError(
                    f"page at offset {offset} had {len(items)} items, fewer than the {self.page_size} asked for, "
                    f"with {total - offset - len(items)} still to come: the server caps the page size lower"
                )
            for i, item in enumerate(items):
                position = offset + i + 1
                if position > start_after:
                    yield SourceRecord(position, flatten(item), json.dumps(item, default=str)[:2000])
            if not items or (short and total is not None):
                break
            # a known total with full pages steps by the page size; with no total the next page starts where this one ended
            offset += self.page_size if total is not None else len(items)
