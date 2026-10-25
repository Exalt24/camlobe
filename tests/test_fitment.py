"""Fitment loading: the part is found like a feed's part, vehicles are one row each, and a re-load changes nothing."""
import pytest

from catalog.fitment import canon_vehicle_text, load_fitment
from catalog.models import Fitment, Part, PipelineRun, RejectedRow, Vehicle

from .helpers import get_supplier, row, run_all, write_csv

pytestmark = pytest.mark.django_db(transaction=True)

HEADER = ["brand", "part_number", "year", "make", "model", "submodel", "engine", "position", "note"]


def seed_parts(tmp_path):
    run_all(get_supplier(), write_csv(tmp_path / "f.csv", [row("MF-H4123A"), row("DU-ZR4519", "Dunmore")]))


def fit(tmp_path, rows, name="fit.csv"):
    path = tmp_path / name
    write_csv(path, rows, header=HEADER)
    return load_fitment(path)


def test_a_part_written_differently_still_resolves_and_vehicles_are_canonical(tmp_path):
    seed_parts(tmp_path)
    run = fit(
        tmp_path,
        [
            ["Marlow Filters", "h 4123 a", "2019", "Altair", "T200", "", "2.0L I4", "", ""],
            ["MARLOW", "H4123A", "2019", " altair ", "t200", "", "2.0l i4", "", ""],  # the same row, other clothes
            ["Marlow", "H4123A", "2020", "Altair", "T200", "Base", "2.0L I4", "front", "with ABS"],
        ],
    )
    assert run.status == PipelineRun.Status.SUCCEEDED
    assert (run.rows_read, run.rows_written, run.rows_unchanged, run.rows_rejected) == (3, 2, 1, 0)
    assert Vehicle.objects.count() == 2
    v = Vehicle.objects.get(year=2019)
    assert (v.make, v.model, v.engine) == ("ALTAIR", "T200", "2.0L I4")
    assert Fitment.objects.get(position="front").note == "with ABS"


def test_the_same_file_again_changes_nothing(tmp_path):
    seed_parts(tmp_path)
    rows = [["Marlow", "H4123A", str(y), "Altair", "T200", "", "2.0L I4", "", ""] for y in range(2015, 2025)]
    fit(tmp_path, rows)
    before = (sorted(Vehicle.objects.values_list("pk", flat=True)), sorted(Fitment.objects.values_list("pk", flat=True)))
    again = fit(tmp_path, rows, name="again.csv")
    assert (again.rows_written, again.rows_unchanged) == (0, 10)
    assert (sorted(Vehicle.objects.values_list("pk", flat=True)), sorted(Fitment.objects.values_list("pk", flat=True))) == before


def test_rows_are_refused_with_a_reason_and_never_make_a_part(tmp_path):
    seed_parts(tmp_path)
    run = fit(
        tmp_path,
        [
            ["Marlow", "ZZ-99999", "2015", "Altair", "T200", "", "", "", ""],  # no such part
            ["Marlow", "H4123A", "twenty", "Altair", "T200", "", "", "", ""],
            ["Marlow", "H4123A", "1800", "Altair", "T200", "", "", "", ""],
            ["Marlow", "H4123A", "2015", "", "T200", "", "", "", ""],
            ["Marlow", "H4", "2015", "Altair", "T200", "", "", "", ""],  # too short to be a part number
            ["Marlow", "H4123A", "2015", "Altair", "T200", "", "", "", ""],  # the one good row
        ],
    )
    assert run.rows_written == 1 and run.rows_rejected == 5
    reasons = sorted(RejectedRow.objects.values_list("reason", flat=True))
    assert reasons == ["bad_part_number", "bad_year", "bad_year", "missing_make_or_model", "unknown_part"]
    assert Part.objects.count() == 2


def test_the_same_part_on_the_same_vehicle_at_two_positions_is_two_rows(tmp_path):
    seed_parts(tmp_path)
    run = fit(
        tmp_path,
        [
            ["Marlow", "H4123A", "2019", "Altair", "T200", "", "2.0L I4", "front", ""],
            ["Marlow", "H4123A", "2019", "Altair", "T200", "", "2.0L I4", "rear", ""],
        ],
    )
    assert run.rows_written == 2 and Vehicle.objects.count() == 1


def test_a_file_without_the_expected_columns_fails_before_writing(tmp_path):
    seed_parts(tmp_path)
    path = tmp_path / "bad.csv"
    path.write_text("brand,part_number,year,make\r\nMarlow,H4123A,2019,Altair\r\n", encoding="utf-8")
    run = load_fitment(path)
    assert run.status == PipelineRun.Status.FAILED and "no column model" in run.errors[0]["message"]
    assert Fitment.objects.count() == 0


def test_small_batches_give_the_same_result(tmp_path):
    seed_parts(tmp_path)
    rows = [["Marlow", "H4123A", str(2000 + y), "Altair", "T200", "", "", "", ""] for y in range(23)]
    path = tmp_path / "f.csv"
    write_csv(path, rows, header=HEADER)
    run = load_fitment(path, batch_size=5)
    assert run.rows_written == 23 and Fitment.objects.count() == 23


def test_vehicle_text_is_trimmed_and_upper_case():
    assert canon_vehicle_text("  Rover   S ") == "ROVER S"
    assert canon_vehicle_text(None) == ""
