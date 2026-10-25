"""The ingest runner: restartable, safe to re-run, and honest about what it rejected."""
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import psycopg
import pytest
from django.conf import settings
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st
from django.db import IntegrityError, connection
from django.db.utils import DatabaseError

from catalog.ingest.adapters import FileAdapter, SourceGap, SourceRecord
from catalog.ingest.runner import ingest
from catalog.models import FeedCheckpoint, PipelineRun, RawPart, RejectedRow
from catalog.runs import AlreadyRunning

from .helpers import CSV_HEADER, get_supplier, row, write_csv

pytestmark = pytest.mark.django_db(transaction=True)

ROOT = Path(__file__).resolve().parents[1]


def hundred_rows(n=100):
    return [row(f"AB{i:04d}", cost=f"{i + 1}.00") for i in range(n)]


class Dying(FileAdapter):
    """Reads like a file adapter and raises after `after` records, the way a dropped connection would."""

    def __init__(self, path, after):
        super().__init__(path)
        self.after = after

    def records(self, start_after=0):
        for n, rec in enumerate(super().records(start_after)):
            if n >= self.after:
                raise ConnectionError("the feed connection dropped")
            yield rec


def snapshot():
    return sorted(RawPart.objects.values_list("idempotency_key", "part_number_raw", "cost_cents"))


def test_rows_are_stored_with_counts_and_rejects_recorded(tmp_path):
    s = get_supplier()
    path = write_csv(
        tmp_path / "f.csv",
        [row("AB1001"), row(""), row("AB1002", cost="N/A"), row("AB1003", cost="$5.00"), row("AB1003", cost="5")],
    )
    run = ingest(s, FileAdapter(path))
    assert run.status == PipelineRun.Status.SUCCEEDED
    assert (run.rows_read, run.rows_written, run.rows_unchanged, run.rows_rejected) == (5, 2, 1, 2)
    assert sorted(RejectedRow.objects.values_list("reason", flat=True)) == ["bad_cost", "blank_part_number"]
    stored = RawPart.objects.get(part_number_raw="AB1003")
    assert stored.cost_cents == 500 and stored.payload["Cost"] == "$5.00"  # as received, and the integer beside it


def test_a_second_run_over_the_same_file_changes_nothing(tmp_path):
    s = get_supplier()
    path = write_csv(tmp_path / "f.csv", hundred_rows())
    first = ingest(s, FileAdapter(path))
    before = snapshot()
    second = ingest(s, FileAdapter(path))
    assert first.rows_written == 100
    assert second.status == PipelineRun.Status.SUCCEEDED
    assert (second.rows_read, second.rows_written) == (0, 0)
    assert "unchanged" in second.notes["skipped"]
    assert snapshot() == before


def test_the_same_content_under_a_new_file_name_stores_nothing_new(tmp_path):
    s = get_supplier()
    ingest(s, FileAdapter(write_csv(tmp_path / "monday.csv", hundred_rows())))
    before = snapshot()
    run = ingest(s, FileAdapter(write_csv(tmp_path / "tuesday.csv", hundred_rows())))
    assert (run.rows_read, run.rows_written, run.rows_unchanged) == (100, 0, 100)
    assert snapshot() == before


def test_one_number_listed_under_two_brands_is_unchanged_when_a_dated_feed_arrives_again(tmp_path):
    s = get_supplier()
    rows = [row("123", "Marlow"), row("123", "Tarn"), row("456", "Marlow")]
    first = ingest(s, FileAdapter(write_csv(tmp_path / "2026-10-01.csv", rows)))
    assert (first.rows_read, first.rows_written, first.rows_unchanged) == (3, 3, 0)
    before = snapshot()
    again = ingest(s, FileAdapter(write_csv(tmp_path / "2026-10-02.csv", rows)))
    assert (again.rows_read, again.rows_written, again.rows_unchanged) == (3, 0, 3)
    assert snapshot() == before


def test_a_changed_file_with_the_same_name_reads_again_and_stores_only_what_changed(tmp_path):
    s = get_supplier()
    path = tmp_path / "f.csv"
    write_csv(path, hundred_rows())
    ingest(s, FileAdapter(path))
    rows = hundred_rows()
    rows[5][3] = "99.99"
    rows.append(row("AB9999"))
    write_csv(path, rows)
    run = ingest(s, FileAdapter(path))
    assert "resumed_from" not in run.notes
    assert (run.rows_read, run.rows_written, run.rows_unchanged) == (101, 2, 99)
    assert RawPart.objects.filter(part_number_raw="AB0005").count() == 2  # both versions are kept


def test_a_price_that_changes_and_changes_back_is_stored_each_time(tmp_path):
    s = get_supplier()
    for name, cost in (("a.csv", "5.00"), ("b.csv", "6.00"), ("c.csv", "5.00")):
        ingest(s, FileAdapter(write_csv(tmp_path / name, [row("AB1001", cost=cost)])))
    versions = list(RawPart.objects.filter(part_number_raw="AB1001").order_by("id").values_list("cost_cents", flat=True))
    assert versions == [500, 600, 500]  # the last one is the current price, not a duplicate of the first


def test_a_price_that_returns_to_an_earlier_value_in_the_same_file_is_the_newest_version(tmp_path):
    """The same file name and row position give run 3 the key run 1 had; the key says only that THAT row landed once, it does
    not say the row is the part's newest version (10.00, 12.00, 10.00 left 12.00 as the current price)."""
    s = get_supplier()
    path = tmp_path / "f.csv"
    counts = []
    for cost in ("10.00", "12.00", "10.00"):
        write_csv(path, [row("AB1001", cost=cost), row("AB1002", cost="5.00")])
        run = ingest(s, FileAdapter(path))
        counts.append((run.rows_written, run.rows_unchanged))
    versions = list(RawPart.objects.filter(part_number_raw="AB1001").order_by("id").values_list("cost_cents", flat=True))
    assert versions == [1000, 1200, 1000] and counts == [(2, 0), (1, 1), (1, 1)]
    assert RawPart.objects.filter(part_number_raw="AB1002").count() == 1


@given(st.lists(st.dictionaries(st.sampled_from(["AB1001", "AB1002", "AB1003"]), st.sampled_from(["10.00", "12.00", "9.50"]), min_size=1), min_size=2, max_size=5))
@hyp_settings(max_examples=40, deadline=None)
def test_whatever_the_prices_do_between_runs_the_newest_stored_version_is_the_latest_price(tmp_path_factory, days):
    """The class: a row counted unchanged because its key exists although it is not the newest stored version. Any sequence of
    prices over the same file name (so the same positions) stores exactly one version per change and ends on the last price."""
    import uuid

    s = get_supplier(f"p{uuid.uuid4().hex[:12]}")
    path = tmp_path_factory.mktemp("days") / "f.csv"
    expected: dict[str, list[int]] = {}
    for day in days:
        parts = sorted(day.items())
        write_csv(path, [row(number, cost=cost) for number, cost in parts])
        ingest(s, FileAdapter(path))
        for number, cost in parts:
            cents = int(round(float(cost) * 100))
            if not expected.get(number) or expected[number][-1] != cents:
                expected.setdefault(number, []).append(cents)
    for number, versions in expected.items():
        stored = list(RawPart.objects.filter(supplier=s, part_number_raw=number).order_by("id").values_list("cost_cents", flat=True))
        assert stored == versions


def test_exact_duplicate_rows_in_one_file_are_stored_once(tmp_path):
    s = get_supplier()
    run = ingest(s, FileAdapter(write_csv(tmp_path / "f.csv", [row("AB1001"), row("AB1001"), row("AB1001")])))
    assert (run.rows_written, run.rows_unchanged) == (1, 2)


def test_a_dropped_connection_fails_the_run_and_the_next_run_resumes(tmp_path):
    s = get_supplier()
    path = write_csv(tmp_path / "f.csv", hundred_rows())
    failed = ingest(s, Dying(path, after=35), batch_size=10)
    assert failed.status == PipelineRun.Status.FAILED
    assert "dropped" in failed.errors[0]["message"]
    cp = FeedCheckpoint.objects.get()
    assert cp.position == 30 and not cp.completed  # three whole batches committed, the 5 in flight were not
    assert RawPart.objects.count() == 30
    assert (failed.rows_read, failed.rows_written) == (30, 30)  # the run row counts what is stored, no more
    resumed = ingest(s, FileAdapter(path), batch_size=10)
    assert resumed.status == PipelineRun.Status.SUCCEEDED
    assert resumed.notes["resumed_from"] == 30 and resumed.rows_read == 70
    assert len(snapshot()) == 100
    assert FeedCheckpoint.objects.get().completed


def test_resumed_result_equals_an_uninterrupted_run(tmp_path):
    path = write_csv(tmp_path / "f.csv", hundred_rows())
    s = get_supplier("one")
    ingest(s, Dying(path, after=45), batch_size=10)
    ingest(s, FileAdapter(path), batch_size=10)
    interrupted = sorted(RawPart.objects.filter(supplier=s).values_list("part_number_raw", "cost_cents", "position"))
    s2 = get_supplier("two")
    ingest(s2, FileAdapter(path), batch_size=10)
    clean = sorted(RawPart.objects.filter(supplier=s2).values_list("part_number_raw", "cost_cents", "position"))
    assert interrupted == clean and len(clean) == 100


def test_a_failing_batch_leaves_no_rows_and_no_counts(tmp_path):
    s = get_supplier()
    path = write_csv(tmp_path / "f.csv", hundred_rows())
    calls = {"n": 0}
    real = RejectedRow.objects.bulk_create

    def boom(objs, *a, **k):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("disk full")
        return real(objs, *a, **k)

    with mock.patch.object(RejectedRow.objects, "bulk_create", side_effect=boom):
        run = ingest(s, FileAdapter(path), batch_size=10)
    assert run.status == PipelineRun.Status.FAILED
    assert RawPart.objects.count() == 20  # batches 1 and 2; batch 3 rolled back whole
    assert FeedCheckpoint.objects.get().position == 20
    run.refresh_from_db()
    assert run.rows_read == 20 and run.rows_written == 20


def test_a_renamed_column_nobody_mapped_fails_before_writing_anything(tmp_path):
    s = get_supplier()
    path = write_csv(tmp_path / "f.csv", hundred_rows(), header=["PartNo", "Producer", "Description", "Cost", "UPC"])
    run = ingest(s, FileAdapter(path))
    assert run.status == PipelineRun.Status.FAILED
    assert "brand" in run.errors[0]["message"] and "Producer" in run.errors[0]["message"]
    assert RawPart.objects.count() == 0
    assert not FeedCheckpoint.objects.get().completed


def test_a_known_rename_and_a_reordered_file_load_the_same_rows(tmp_path):
    s = get_supplier()
    ingest(s, FileAdapter(write_csv(tmp_path / "a.csv", hundred_rows())))
    before = snapshot()
    reordered = [[r[4], r[3], r[2], r[1], r[0]] for r in hundred_rows()]
    run = ingest(
        s, FileAdapter(write_csv(tmp_path / "b.csv", reordered, header=["UPC", "Cost", "Description", "Manufacturer", "PartNo"]))
    )
    assert (run.rows_written, run.rows_unchanged) == (0, 100)
    assert snapshot() == before


def test_the_adapter_reports_a_gap_and_the_run_is_partial_and_resumable(tmp_path):
    s = get_supplier()

    class Gappy:
        source_ref = "api"

        def __init__(self, fixed):
            self.fixed = fixed

        def fingerprint(self):
            return ""

        def notes(self):
            return {}

        def records(self, start_after=0):
            for i in range(1, 31):
                if i <= start_after:
                    continue
                if 11 <= i <= 20 and not self.fixed:
                    if i == 11:
                        yield SourceGap(11, 20, "page 2 failed after 3 tries")
                    continue
                yield SourceRecord(i, {"PartNo": f"AB{i:04d}", "Mfr": "Marlow", "Cost": "1.00"})

    run = ingest(s, Gappy(fixed=False), batch_size=5)
    assert run.status == PipelineRun.Status.PARTIAL
    assert run.errors[0]["where"] == "positions 11-20"
    assert RawPart.objects.count() == 20  # everything around the gap was stored
    cp = FeedCheckpoint.objects.get()
    assert cp.position == 10 and not cp.completed  # the checkpoint stops at the gap
    again = ingest(s, Gappy(fixed=True), batch_size=5)
    assert again.status == PipelineRun.Status.SUCCEEDED
    assert RawPart.objects.count() == 30  # only the missing page was added
    assert again.notes["resumed_from"] == 10 and again.rows_written == 10 and again.rows_unchanged == 10


def test_rereading_rows_after_a_gap_does_not_store_a_row_twice():
    """The checkpoint stops at a gap, so the rows after it are read again. Their idempotency keys stop them landing twice,
    even when the part has two versions after the gap and the latest stored one is not the row being re-read."""
    s = get_supplier()

    class AfterGap:
        source_ref = "api"

        def __init__(self, fixed):
            self.fixed = fixed

        def fingerprint(self):
            return ""

        def notes(self):
            return {}

        def records(self, start_after=0):
            for i in range(1, 31):
                if i <= start_after:
                    continue
                if 11 <= i <= 20 and not self.fixed:
                    if i == 11:
                        yield SourceGap(11, 20, "page 2 failed")
                    continue
                cost = {25: "6.00", 28: "7.00"}.get(i, "1.00")
                part = "AB9999" if i in (25, 28) else f"AB{i:04d}"
                yield SourceRecord(i, {"PartNo": part, "Mfr": "Marlow", "Cost": cost})

    ingest(s, AfterGap(fixed=False), batch_size=5)
    again = ingest(s, AfterGap(fixed=True), batch_size=5)
    assert again.status == PipelineRun.Status.SUCCEEDED
    assert RawPart.objects.filter(part_number_raw="AB9999").count() == 2  # one row per version, not four
    assert list(RawPart.objects.filter(part_number_raw="AB9999").order_by("id").values_list("cost_cents", flat=True)) == [600, 700]


def test_a_live_source_with_no_fingerprint_is_read_again_each_time():
    s = get_supplier()

    class Live:
        source_ref = "live"

        def fingerprint(self):
            return ""

        def notes(self):
            return {}

        def records(self, start_after=0):
            yield SourceRecord(1, {"PartNo": "AB1001", "Mfr": "Marlow", "Cost": "1.00"})

    assert ingest(s, Live()).rows_written == 1
    second = ingest(s, Live())
    assert second.rows_read == 1 and second.rows_unchanged == 1  # read again, nothing new stored


def test_two_runs_of_one_source_cannot_overlap(tmp_path):
    s = get_supplier()
    path = write_csv(tmp_path / "f.csv", hundred_rows())
    with psycopg.connect(dsn(), autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", [f"ingest:{s.code}:{path.name}"])
        with pytest.raises(AlreadyRunning):
            ingest(s, FileAdapter(path))
    assert RawPart.objects.count() == 0
    assert ingest(s, FileAdapter(path)).rows_written == 100  # the lock is gone with the other connection


def test_a_run_left_running_by_a_dead_process_is_closed_at_the_next_start(tmp_path):
    s = get_supplier()
    path = write_csv(tmp_path / "f.csv", hundred_rows(5))
    ghost = PipelineRun.objects.create(kind="ingest", supplier=s, source_ref=path.name)
    ingest(s, FileAdapter(path))
    ghost.refresh_from_db()
    assert ghost.status == PipelineRun.Status.FAILED and "abandoned" in ghost.errors[0]["message"]


def test_raw_rows_are_append_only_in_the_database(tmp_path):
    s = get_supplier()
    ingest(s, FileAdapter(write_csv(tmp_path / "f.csv", hundred_rows(3))))
    raw = RawPart.objects.first()
    with pytest.raises(DatabaseError, match="append only"):
        RawPart.objects.filter(pk=raw.pk).update(part_number_raw="CHANGED")
    with pytest.raises(DatabaseError, match="append only"):
        RawPart.objects.filter(pk=raw.pk).delete()
    assert RawPart.objects.count() == 3


def test_an_idempotency_key_can_only_land_once(tmp_path):
    s = get_supplier()
    ingest(s, FileAdapter(write_csv(tmp_path / "f.csv", hundred_rows(1))))
    raw = RawPart.objects.get()
    with pytest.raises(IntegrityError):
        RawPart.objects.create(
            supplier=s, run=raw.run, position=1, idempotency_key=raw.idempotency_key, content_hash="x",
            part_number_raw="ZZ9999", payload={},
        )


def test_xml_and_bom_csv_through_the_runner(tmp_path):
    s = get_supplier()
    xml = tmp_path / "h.xml"
    xml.write_text(
        '<?xml version="1.0" encoding="UTF-8"?><catalog><item><sku>MF-H4123A</sku><manufacturer>Marlow</manufacturer>'
        '<price currency="USD">1,234.50</price><gtin>5012345678900</gtin></item></catalog>',
        encoding="utf-8",
    )
    run = ingest(s, FileAdapter(xml))
    assert run.status == PipelineRun.Status.SUCCEEDED
    raw = RawPart.objects.get()
    assert (raw.part_number_raw, raw.cost_cents, raw.gtin_raw) == ("MF-H4123A", 123450, "5012345678900")
    assert run.notes["unmapped_columns"] == ["price@currency"]


def dsn() -> str:
    d = settings.DATABASES["default"]
    return f"host={d['HOST']} port={d['PORT']} dbname={connection.settings_dict['NAME']} user={d['USER']} password={d['PASSWORD']}"


def test_a_process_killed_mid_run_leaves_a_consistent_checkpoint_and_the_rerun_finishes(tmp_path):
    s = get_supplier()
    path = write_csv(tmp_path / "big.csv", [row(f"AB{i:05d}", cost=f"{i % 90 + 1}.00") for i in range(6000)])
    env = {**os.environ, "CAMLOBE_DB_NAME": connection.settings_dict["NAME"]}
    proc = subprocess.Popen(
        [sys.executable, "manage.py", "ingest_feed", s.code, "--file", str(path), "--batch-size", "20"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 120
    while time.time() < deadline:
        cp = FeedCheckpoint.objects.filter(supplier=s).first()
        if cp and cp.position >= 100:
            break
        time.sleep(0.05)
    proc.kill()
    proc.wait()
    cp = FeedCheckpoint.objects.get(supplier=s)
    stored = RawPart.objects.filter(supplier=s).count()
    assert 100 <= cp.position < 6000 and not cp.completed, "the process finished before it was killed"
    assert stored == cp.position  # every row up to the checkpoint is stored, none beyond it
    assert PipelineRun.objects.filter(status="running").count() == 1  # the dead process never closed its run
    dead = PipelineRun.objects.get(status="running")
    assert (dead.rows_read, dead.rows_written) == (cp.position, cp.position)  # its counts were saved with each batch
    resumed = ingest(s, FileAdapter(path), batch_size=500)
    assert resumed.status == PipelineRun.Status.SUCCEEDED
    assert resumed.notes["resumed_from"] == cp.position
    assert RawPart.objects.filter(supplier=s).count() == 6000
    assert PipelineRun.objects.filter(status="running").count() == 0


# ---- ordinary feed shapes that used to fail the whole run -------------------------------------------


def test_a_csv_with_both_upc_and_ean_columns_loads_and_takes_the_first_valid_gtin(tmp_path):
    path = tmp_path / "both.csv"
    path.write_text(
        "PartNo,Brand,Description,Cost,UPC,EAN\n"
        "AB100,Marlow,both valid,1.00,012345678905,0012345678905\n"  # the same code in two widths: the UPC column is first
        "AB101,Marlow,first is junk,1.00,12345,5012345678900\n"
        "AB102,Marlow,only junk,1.00,12345,999\n"
        "AB103,Marlow,neither,1.00,,\n",
        encoding="utf-8",
    )
    run = ingest(get_supplier(), FileAdapter(path))
    assert run.status == PipelineRun.Status.SUCCEEDED and run.rows_written == 4
    assert {r.part_number_raw: r.gtin_raw for r in RawPart.objects.all()} == {
        "AB100": "012345678905", "AB101": "5012345678900", "AB102": "12345", "AB103": ""
    }  # a junk GTIN with nothing better is kept, so the normalise step still flags it as invalid
    assert run.notes["doubled_columns"] == {"gtin": ["UPC", "EAN"]}
    # which column carried each row's GTIN: the two valid ones, and the junk value that was kept because nothing was better
    assert run.notes["column_choices"]["gtin"] == {"UPC": 2, "EAN": 1}


def test_two_columns_for_a_required_field_are_still_refused(tmp_path):
    path = tmp_path / "two_numbers.csv"
    path.write_text("SKU,MPN,Brand,Cost\nAB100,XY200,Marlow,1.00\n", encoding="utf-8")
    run = ingest(get_supplier(), FileAdapter(path))
    assert run.status == PipelineRun.Status.FAILED and "more than one column maps to the same field" in run.errors[0]["message"]
    assert RawPart.objects.count() == 0


def test_a_utf16_excel_export_goes_through_the_runner(tmp_path):
    path = tmp_path / "unicode.txt"
    text = "PartNo\tMfr\tDescription\tCost\tUPC\r\nAB100\tMarlow\tPompe à eau\t1.00\t\r\n"
    path.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    run = ingest(get_supplier(), FileAdapter(path, fmt="csv"))
    assert run.status == PipelineRun.Status.SUCCEEDED and run.rows_written == 1 and run.notes["encoding"] == "utf-16"
    assert RawPart.objects.get().description_raw == "Pompe à eau"


def test_a_namespaced_xml_feed_goes_through_the_runner(tmp_path):
    path = tmp_path / "ns.xml"
    path.write_text(
        '<catalog xmlns="urn:example:catalog"><item><sku>AB100</sku><manufacturer>Marlow</manufacturer><price>1.00</price></item></catalog>',
        encoding="utf-8",
    )
    run = ingest(get_supplier(), FileAdapter(path))
    assert run.status == PipelineRun.Status.SUCCEEDED and run.rows_written == 1


def test_a_nul_in_one_row_rejects_that_row_and_not_the_run(tmp_path):
    path = tmp_path / "nul.csv"
    path.write_bytes(",".join(CSV_HEADER).encode() + b"\r\nAB100,Marlow,Oil\x00filter,1.00,\r\nAB101,Marlow,Fine,2.00,\r\n")
    run = ingest(get_supplier(), FileAdapter(path))
    assert run.status == PipelineRun.Status.SUCCEEDED and (run.rows_written, run.rows_rejected) == (1, 1)
    assert RejectedRow.objects.get().reason == "nul_character" and "\x00" not in RejectedRow.objects.get().raw
