"""The normalize step: a queue of raw rows with no normalized row, so it restarts and re-runs cleanly."""
from unittest import mock

import pytest

from catalog.ingest.adapters import FileAdapter
from catalog.ingest.runner import ingest
from catalog.models import NormalizedRow, PipelineRun, RawPart
from catalog.normalize import NORMALIZER_VERSION
from catalog.normalize_step import normalize_pending

from .helpers import get_supplier, row, write_csv

pytestmark = pytest.mark.django_db(transaction=True)


def load(tmp_path, rows):
    s = get_supplier()
    ingest(s, FileAdapter(write_csv(tmp_path / "f.csv", rows)))
    return s


def raw_state():
    return sorted(RawPart.objects.values_list("pk", "part_number_raw", "brand_raw", "description_raw", "cost_cents", "payload", "content_hash"))


def test_rows_are_normalized_and_the_raw_rows_are_untouched(tmp_path):
    load(tmp_path, [row("mf-h4123a", "Marlow Filters", gtin="5012345678900"), row("0000200022", "TARN THERMAL"), row("AB", "Marlow")])
    before = raw_state()
    run = normalize_pending()
    assert run.status == PipelineRun.Status.SUCCEEDED and run.rows_read == 3
    assert (run.rows_written, run.rows_rejected) == (2, 1)
    rows = {n.raw.part_number_raw: n for n in NormalizedRow.objects.select_related("raw")}
    first = rows["mf-h4123a"]
    assert (first.brand, first.number_norm, first.gtin, first.version) == ("MARLOW", "H4123A", "05012345678900", NORMALIZER_VERSION)
    assert set(first.rules) == {"R2", "R3", "R4"}
    assert rows["0000200022"].number_norm == "200022"
    bad = rows["AB"]
    assert (bad.status, bad.reject_reason, bad.match_state, bad.number_norm) == ("rejected", "too_short", "skipped", "")
    assert raw_state() == before


def test_a_second_run_finds_nothing_to_do(tmp_path):
    load(tmp_path, [row(f"AB{i:04d}") for i in range(30)])
    normalize_pending()
    before = sorted(NormalizedRow.objects.values_list("pk", "number_norm", "normalized_at"))
    run = normalize_pending()
    assert (run.rows_read, run.rows_written) == (0, 0)
    assert sorted(NormalizedRow.objects.values_list("pk", "number_norm", "normalized_at")) == before


def test_a_run_that_dies_mid_way_resumes_with_the_remaining_rows(tmp_path):
    load(tmp_path, [row(f"AB{i:04d}") for i in range(50)])
    real = NormalizedRow.objects.bulk_create
    calls = {"n": 0}

    def boom(objs, *a, **k):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("killed")
        return real(objs, *a, **k)

    with mock.patch.object(NormalizedRow.objects, "bulk_create", side_effect=boom):
        failed = normalize_pending(batch_size=10)
    assert failed.status == PipelineRun.Status.FAILED
    assert NormalizedRow.objects.count() == 20 and failed.rows_read == 20
    rest = normalize_pending(batch_size=10)
    assert rest.rows_read == 30 and NormalizedRow.objects.count() == 50
    assert RawPart.objects.filter(normalized__isnull=True).count() == 0


def test_a_limit_stops_early(tmp_path):
    load(tmp_path, [row(f"AB{i:04d}") for i in range(25)])
    assert normalize_pending(batch_size=10, limit=15).rows_read == 15
    assert NormalizedRow.objects.count() == 15


def test_stale_rows_from_an_older_rule_set_are_reported(tmp_path):
    load(tmp_path, [row("AB1001")])
    normalize_pending()
    NormalizedRow.objects.update(version=NORMALIZER_VERSION - 1)
    run = normalize_pending()
    assert run.notes["stale_rows"] == 1
