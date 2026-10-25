"""The query plans stay on their indexes at 60,000 parts, and each check goes red when its index is gone."""
import pytest
from django.core.management import call_command
from django.db import connection, transaction

from catalog.plans import PlanCheck, build_checks, check_plans, evaluate

pytestmark = pytest.mark.django_db

N = 60000


def seed_bulk() -> None:
    with connection.cursor() as c:
        c.execute("INSERT INTO catalog_supplier (code, name, created_at) VALUES ('bulk', 'bulk', now())")
        c.execute(
            "INSERT INTO catalog_pipelinerun (kind, supplier_id, source_ref, status, started_at, rows_read, rows_written,"
            " rows_unchanged, rows_rejected, errors, notes) SELECT 'ingest', id, 'bulk', 'succeeded', now(), 0, 0, 0, 0,"
            " '[]', '{}' FROM catalog_supplier WHERE code = 'bulk'"
        )
        c.execute(
            "INSERT INTO catalog_part (brand, number_norm, description, gtin, created_at, updated_at, search_version, indexed_version)"
            " SELECT 'B' || mod(i, 6), 'N' || lpad(i::text, 7, '0'), 'd', CASE WHEN mod(i, 3) = 0 THEN '' ELSE lpad(i::text, 14, '0') END,"
            " now(), now(), 1, CASE WHEN mod(i, 500) = 0 THEN 0 ELSE 1 END FROM generate_series(1, %s) i",
            [N],
        )
        c.execute(
            "INSERT INTO catalog_rawpart (supplier_id, run_id, position, idempotency_key, content_hash, part_number_raw,"
            " brand_raw, description_raw, gtin_raw, cost_cents, payload, received_at)"
            " SELECT s.id, r.id, i, md5(i::text), md5(i::text), 'N' || lpad(i::text, 7, '0'), 'B', 'd', '', 100, '{}', now()"
            " FROM generate_series(1, %s) i, catalog_supplier s, catalog_pipelinerun r",
            [N],
        )
        c.execute(
            "INSERT INTO catalog_normalizedrow (raw_id, supplier_id, status, reject_reason, brand, number_norm, gtin, rules,"
            " version, match_state, normalized_at)"
            " SELECT id, supplier_id, 'ok', '', 'B', part_number_raw, '', '[]', 1, CASE WHEN position % 100 = 0 THEN 'pending' ELSE 'matched' END, now()"
            " FROM catalog_rawpart"
        )
        c.execute(
            "INSERT INTO catalog_reviewitem (supplier_id, listing_key, norm_row_id, reason, candidate_part_ids, status, created_at)"
            " SELECT supplier_id, '|' || number_norm, id, 'no_brand', '[]', CASE WHEN id % 2000 = 0 THEN 'pending' ELSE 'linked' END, now()"
            " FROM catalog_normalizedrow WHERE id % 10 = 0"
        )
        c.execute(
            "INSERT INTO catalog_vehicle (year, make, model, submodel, engine)"
            " SELECT y, 'Make' || mk, 'Model' || md, '', '' FROM generate_series(1990, 2024) y,"
            " generate_series(1, 30) mk, generate_series(1, 40) md"
        )
        c.execute(
            "WITH v AS (SELECT array_agg(id) AS ids FROM catalog_vehicle)"
            " INSERT INTO catalog_fitment (part_id, vehicle_id, position, note)"
            " SELECT p.id, v.ids[1 + mod(p.id, array_length(v.ids, 1))], 'front', ''"
            " FROM catalog_part p, v WHERE mod(p.id, 2) = 0"
        )


@pytest.fixture
def bulk(db):
    seed_bulk()


def test_every_hot_query_uses_its_index_at_sixty_thousand_parts(bulk):
    results = check_plans(sample=200)
    assert len(results) == 9
    for r in results:
        assert r.ok, f"{r.name}: {r.problems}"


DROPS = {
    "part_number_idx": ["match: parts by number", "api: search by part number prefix"],
    "part_gtin_idx": ["match: parts by gtin"],
    "rawpart_latest_idx": ["ingest: latest raw row per part number"],
    "norm_pending_idx": ["match: pending queue"],
    "review_status_idx": ["review queue"],
    "part_search_dirty_idx": ["search: parts not yet indexed"],
    "one_vehicle_row": ["api: fitment for a vehicle"],  # the unique constraint is the index (year, make, model, ...)
}


def test_each_check_goes_red_when_its_index_is_dropped(bulk):
    check_plans(sample=200)  # analyzes the tables once
    checks = {c.name: c for c in build_checks(200)}
    for index, names in DROPS.items():
        sid = transaction.savepoint()
        with connection.cursor() as c:
            if index == "one_vehicle_row":
                c.execute("ALTER TABLE catalog_vehicle DROP CONSTRAINT one_vehicle_row")
            else:
                c.execute(f"DROP INDEX {index}")
        for name in names:
            result = evaluate(checks[name])
            assert not result.ok, f"{name} still passed without {index}"
            assert any(index in p or "Seq Scan" in p for p in result.problems)
        transaction.savepoint_rollback(sid)


def test_a_seq_scan_on_a_big_table_is_a_problem_even_with_no_index_named(bulk):
    from catalog.models import Part, Supplier

    scanned = evaluate(PlanCheck("no index on description", Part.objects.filter(description="nothing like this")))
    assert not scanned.ok and any("Seq Scan on catalog_part" in p for p in scanned.problems)
    tiny = evaluate(PlanCheck("a one row table", Supplier.objects.filter(name="bulk")))
    assert tiny.ok, tiny.problems  # a Seq Scan on a small table is the right plan and is not flagged


def test_a_slow_query_fails_its_budget(bulk):
    from catalog.models import Part

    check = PlanCheck("slow on purpose", Part.objects.filter(number_norm__startswith="N0"), budget_ms=0.0001)
    result = evaluate(check)
    assert not result.ok and any("budget" in p for p in result.problems)


def test_the_command_reports_and_fails_on_a_problem(bulk, capsys):
    call_command("check_query_plans", "--sample", "200")
    assert "9 of 9 query plans use their indexes" in capsys.readouterr().out
    with connection.cursor() as c:
        c.execute("DROP INDEX part_gtin_idx")
    with pytest.raises(Exception, match="query plans failed"):
        call_command("check_query_plans", "--sample", "200")


def test_the_command_needs_data():
    with pytest.raises(Exception, match="ingest, normalize and match first"):
        call_command("check_query_plans")
