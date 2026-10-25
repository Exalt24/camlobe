"""Query plan checks: the queries the pipeline and the API depend on must use their indexes.

`check_plans` runs EXPLAIN (ANALYZE, FORMAT JSON) on the real querysets with parameters sampled from
the data, walks the plan and reports a failure when a node reads a big table with a Seq Scan, when the
named index is not used, or when execution takes longer than the budget. It exists because a missing
index is silent: the query still returns the right rows, only slower as the table grows.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from django.db import connection

from .models import Fitment, NormalizedRow, Part, RawPart, ReviewItem, Vehicle

BIG_TABLE_ROWS = 5000  # a Seq Scan on a table with fewer estimated rows is fine
DEFAULT_BUDGET_MS = 250.0


@dataclass
class PlanCheck:
    name: str
    queryset: object
    want_index: str = ""  # an index that must appear in the plan
    budget_ms: float = DEFAULT_BUDGET_MS


@dataclass
class PlanResult:
    name: str
    ok: bool
    ms: float
    problems: list[str] = field(default_factory=list)
    nodes: list[str] = field(default_factory=list)


def _walk(node: dict):
    yield node
    for child in node.get("Plans", []):
        yield from _walk(child)


def _estimated_rows(table: str) -> float:
    with connection.cursor() as cursor:
        cursor.execute("SELECT reltuples FROM pg_class WHERE relname = %s", [table])
        row = cursor.fetchone()
    return float(row[0]) if row else 0.0


def evaluate(check: PlanCheck) -> PlanResult:
    raw = check.queryset.explain(analyze=True, format="json")
    plan = json.loads(raw) if isinstance(raw, str) else raw
    root = plan[0]["Plan"]
    ms = float(plan[0].get("Execution Time", 0.0))
    problems: list[str] = []
    nodes: list[str] = []
    indexes: set[str] = set()
    for node in _walk(root):
        kind = node["Node Type"]
        relation = node.get("Relation Name", "")
        nodes.append(f"{kind} {relation}".strip())
        if node.get("Index Name"):
            indexes.add(node["Index Name"])
        if kind == "Seq Scan" and _estimated_rows(relation) > BIG_TABLE_ROWS:
            problems.append(f"Seq Scan on {relation} (about {int(_estimated_rows(relation))} rows)")
    if check.want_index and check.want_index not in indexes:
        problems.append(f"index {check.want_index} not used (plan used: {sorted(indexes) or 'none'})")
    if ms > check.budget_ms:
        problems.append(f"took {ms:.1f} ms, budget {check.budget_ms:.0f} ms")
    return PlanResult(check.name, not problems, ms, problems, nodes)


def _sample(queryset, column: str, n: int) -> list[str]:
    return list(queryset.order_by("id").values_list(column, flat=True)[:: max(1, queryset.count() // n)][:n])


def build_checks(sample: int = 2000) -> list[PlanCheck]:
    numbers = _sample(Part.objects.all(), "number_norm", sample)
    gtins = _sample(Part.objects.exclude(gtin=""), "gtin", sample)
    first_supplier = RawPart.objects.values_list("supplier_id", flat=True).first()
    part_numbers = _sample(RawPart.objects.filter(supplier_id=first_supplier), "part_number_raw", sample)
    vehicle = Vehicle.objects.first()
    prefix = numbers[0][:6] if numbers else "A"
    checks = [
        PlanCheck("match: parts by number", Part.objects.filter(number_norm__in=numbers), "part_number_idx"),
        PlanCheck("match: parts by gtin", Part.objects.filter(gtin__in=gtins), "part_gtin_idx"),
        PlanCheck(
            "match: pending queue",
            NormalizedRow.objects.filter(match_state="pending", status="ok", id__gt=0).order_by("id")[:2000],
            "norm_pending_idx",
        ),
        PlanCheck(
            "ingest: latest raw row per part number",
            RawPart.objects.filter(supplier_id=first_supplier, part_number_raw__in=part_numbers)
            .order_by("part_number_raw", "-id")
            .distinct("part_number_raw"),
            "rawpart_latest_idx",
        ),
        PlanCheck("api: search by part number prefix", Part.objects.filter(number_norm__startswith=prefix).order_by("number_norm")[:25], "part_number_idx"),
        PlanCheck("review queue", ReviewItem.objects.filter(status="pending").order_by("id")[:50], "review_status_idx"),
    ]
    if vehicle is not None:
        checks.append(
            PlanCheck(
                "api: fitment for a vehicle",
                Fitment.objects.filter(
                    vehicle__make=vehicle.make, vehicle__model=vehicle.model, vehicle__year=vehicle.year
                ).select_related("part"),
                "one_vehicle_row",
            )
        )
    return checks


def analyze_tables() -> None:
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE")


def check_plans(sample: int = 2000) -> list[PlanResult]:
    analyze_tables()
    return [evaluate(check) for check in build_checks(sample)]
