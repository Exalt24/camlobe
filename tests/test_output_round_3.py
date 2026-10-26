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
    from .test_scrape_pages import adapter_for

    page = products_table([("DA1001", "Marlow", "Oil filter", "5.00"), ("DA1001", "Marlow", "Oil filter x10", "40.00"), ("DA1002", "Marlow", "Hose", "3.00")])
    supplier = fresh_supplier()
    out = [ingest(supplier, adapter_for(lambda url: (200, {}, page))) for _ in range(3)]
    assert [(r.status, r.rows_written, r.rows_rejected) for r in out] == [("succeeded", 2, 1), ("succeeded", 0, 1), ("succeeded", 0, 1)]
    assert versions(supplier, "DA1001") == [500]
