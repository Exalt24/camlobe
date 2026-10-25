"""The match rule, one test per branch and one per near miss. Docs: docs/matching.md."""
import pytest

from catalog.ingest.adapters import FileAdapter
from catalog.ingest.runner import ingest
from catalog.matching import match_pending, resolve_review
from catalog.models import NormalizedRow, Part, PartSource, ReviewItem
from catalog.normalize import normalize_gtin
from catalog.normalize_step import normalize_pending

from .helpers import get_supplier, row, run_all, write_csv

pytestmark = pytest.mark.django_db(transaction=True)


def gtin(n: int) -> str:
    """A valid 13 digit GTIN built from a small number."""
    body = f"{5000000000000 + n * 7919:012d}"[:12]
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return body + str((10 - total % 10) % 10)


def load(tmp_path, supplier_code, rows, name=None):
    s = get_supplier(supplier_code)
    return run_all(s, write_csv(tmp_path / (name or f"{supplier_code}.csv"), rows))


def parts():
    return sorted(Part.objects.values_list("brand", "number_norm"))


def test_the_same_part_from_two_suppliers_in_different_clothes_is_one_part(tmp_path):
    load(tmp_path, "s1", [row("MF-H4123A", "Marlow")])
    load(tmp_path, "s2", [row("h4123 a", "MARLOW FILTRATION"), row("H 4123 A", "marlow")])
    assert parts() == [("MARLOW", "H4123A")]
    part = Part.objects.get()
    assert sorted(part.sources.values_list("supplier__code", flat=True)) == ["s1", "s2"]
    assert part.sources.get(supplier__code="s2").linked_by == "exact"
    assert part.sources.get(supplier__code="s1").linked_by == "new"


def test_a_number_padded_with_more_zeros_is_queued_not_split_into_two_parts(tmp_path):
    load(tmp_path, "s1", [row("0045678", "Marlow")])
    load(tmp_path, "s2", [row("00045678", "Marlow")])
    item = ReviewItem.objects.get()
    assert item.reason == "zero_variant" and item.status == "pending"
    assert parts() == [("MARLOW", "0045678")] and item.candidate_part_ids == [Part.objects.get().pk]
    assert not PartSource.objects.filter(supplier__code="s2").exists()
    assert NormalizedRow.objects.get(supplier__code="s2").match_state == "review"


def test_the_padded_form_arriving_first_queues_the_shorter_one_the_same_way(tmp_path):
    load(tmp_path, "s1", [row("00045678", "Marlow")])
    load(tmp_path, "s2", [row("0045678", "Marlow")])
    assert ReviewItem.objects.get().reason == "zero_variant"
    assert Part.objects.count() == 1


def test_two_zero_variants_in_one_batch_are_decided_the_same_as_across_two_runs(tmp_path):
    load(tmp_path, "s1", [row("0045678", "Marlow"), row("00045678", "Marlow")])
    assert Part.objects.count() == 1 and ReviewItem.objects.get().reason == "zero_variant"


def test_a_person_can_link_a_zero_variant_to_the_part_or_keep_it_as_its_own(tmp_path):
    load(tmp_path, "s1", [row("0045678", "Marlow")])
    load(tmp_path, "s2", [row("00045678", "Marlow")])
    load(tmp_path, "s3", [row("000045678", "Marlow")])
    items = list(ReviewItem.objects.order_by("pk"))
    assert len(items) == 2
    resolve_review(items[0], part=Part.objects.get())
    resolve_review(items[1], new_part=True)
    assert parts() == [("MARLOW", "0045678"), ("MARLOW", "45678")]
    assert PartSource.objects.count() == 3


def test_numbers_that_only_look_alike_are_not_zero_variants(tmp_path):
    load(tmp_path, "s1", [row("0045678", "Marlow"), row("AB0123", "Marlow"), row("0123456", "Halden")])
    load(tmp_path, "s2", [row("45679", "Marlow"), row("AB123", "Marlow"), row("0123456", "Marlow")])
    assert not ReviewItem.objects.exists()
    assert Part.objects.count() == 6


def test_the_same_padded_number_from_two_suppliers_still_links_exactly(tmp_path):
    load(tmp_path, "s1", [row("0045678", "Marlow")])
    load(tmp_path, "s2", [row("0045678", "Marlow")])
    assert Part.objects.get().sources.count() == 2 and not ReviewItem.objects.exists()


def test_the_same_number_under_another_brand_is_a_different_part(tmp_path):
    load(tmp_path, "s1", [row("H4123A", "Marlow")])
    load(tmp_path, "s2", [row("H4123A", "Halden")])
    assert parts() == [("HALDEN", "H4123A"), ("MARLOW", "H4123A")]
    assert not ReviewItem.objects.exists()


def test_a_row_without_a_brand_is_queued_with_the_parts_that_carry_its_number(tmp_path):
    load(tmp_path, "s1", [row("H4123A", "Marlow"), row("H4123A", "Halden")])
    load(tmp_path, "s2", [row("H4123A", "")])
    item = ReviewItem.objects.get()
    assert item.reason == "no_brand" and item.status == "pending"
    assert item.candidate_part_ids == sorted(Part.objects.values_list("pk", flat=True))
    assert Part.objects.count() == 2 and not PartSource.objects.filter(supplier__code="s2").exists()
    assert NormalizedRow.objects.get(supplier__code="s2").match_state == "review"


def test_a_row_without_a_brand_and_no_candidate_creates_nothing(tmp_path):
    load(tmp_path, "s1", [row("H4123A", "")])
    item = ReviewItem.objects.get()
    assert item.reason == "no_brand" and item.candidate_part_ids == []
    assert Part.objects.count() == 0 and PartSource.objects.count() == 0


def test_two_suppliers_that_disagree_on_the_gtin_are_queued_not_merged_or_split(tmp_path):
    load(tmp_path, "s1", [row("H4123A", gtin=gtin(1))])
    load(tmp_path, "s2", [row("H4123A", gtin=gtin(2))])
    item = ReviewItem.objects.get()
    assert item.reason == "gtin_conflict"
    part = Part.objects.get()
    assert item.candidate_part_ids == [part.pk]
    assert part.gtin == normalize_gtin(gtin(1))[0]  # the first one stays
    assert part.sources.count() == 1


def test_a_different_number_with_a_known_gtin_is_queued_not_created(tmp_path):
    load(tmp_path, "s1", [row("H4123A", gtin=gtin(1))])
    load(tmp_path, "s2", [row("H4123B", gtin=gtin(1))])  # a renumbering, or a typo: nobody decides by guess
    item = ReviewItem.objects.get()
    assert item.reason == "gtin_cross" and item.candidate_part_ids == [Part.objects.get().pk]
    assert parts() == [("MARLOW", "H4123A")]


def test_the_same_gtin_under_another_brand_is_queued(tmp_path):
    load(tmp_path, "s1", [row("H4123A", "Marlow", gtin=gtin(1))])
    load(tmp_path, "s2", [row("ZR4519", "Dunmore", gtin=gtin(1))])
    assert ReviewItem.objects.get().reason == "gtin_cross"
    assert parts() == [("MARLOW", "H4123A")]


def test_agreeing_gtins_link_and_a_missing_one_is_filled_in(tmp_path):
    load(tmp_path, "s1", [row("H4123A", gtin="")])
    load(tmp_path, "s2", [row("H4123A", gtin=gtin(1))])
    load(tmp_path, "s3", [row("H4123A", gtin=gtin(1))])
    part = Part.objects.get()
    assert part.sources.count() == 3 and part.gtin == normalize_gtin(gtin(1))[0]
    assert not ReviewItem.objects.exists()


def test_an_invalid_gtin_is_dropped_and_causes_no_conflict(tmp_path):
    load(tmp_path, "s1", [row("H4123A", gtin=gtin(1))])
    load(tmp_path, "s2", [row("H4123A", gtin="5012345678901")])  # wrong check digit
    assert Part.objects.get().sources.count() == 2
    assert "gtin_invalid" in NormalizedRow.objects.get(supplier__code="s2").rules


def test_a_new_version_of_a_linked_listing_refreshes_it_and_touches_the_part(tmp_path):
    load(tmp_path, "s1", [row("H4123A", cost="5.00")], name="a.csv")
    before = Part.objects.get().updated_at
    load(tmp_path, "s1", [row("H4123A", cost="6.50")], name="b.csv")
    source = PartSource.objects.get()
    assert source.cost_cents == 650 and Part.objects.count() == 1
    assert Part.objects.get().updated_at > before
    assert source.raw_part.cost_cents == 650


def test_two_versions_of_one_listing_in_one_batch_make_one_link_with_the_newest_data(tmp_path):
    load(tmp_path, "s1", [row("H4123A", cost="5.00"), row("H4123A", cost="6.00")])
    source = PartSource.objects.get()
    assert source.cost_cents == 600 and Part.objects.count() == 1
    assert sorted(NormalizedRow.objects.values_list("match_state", flat=True)) == ["matched", "matched"]


def test_a_queued_listing_keeps_one_review_item_pointing_at_its_newest_row(tmp_path):
    load(tmp_path, "s1", [row("H4123A", "")], name="a.csv")
    load(tmp_path, "s1", [row("H4123A", "", cost="9.00")], name="b.csv")
    item = ReviewItem.objects.get()
    assert item.norm_row.raw.cost_cents == 900
    assert NormalizedRow.objects.filter(match_state="review").count() == 2


def test_resolving_to_a_part_links_it_and_later_rows_flow_through_the_link(tmp_path):
    load(tmp_path, "s1", [row("H4123A", "Marlow")])
    load(tmp_path, "s2", [row("H4123A", "")], name="a.csv")
    item = ReviewItem.objects.get()
    resolve_review(item, part=Part.objects.get())
    item.refresh_from_db()
    assert item.status == "linked" and item.resolved_at is not None
    assert PartSource.objects.get(supplier__code="s2").linked_by == "review"
    load(tmp_path, "s2", [row("H4123A", "", cost="7.00")], name="b.csv")
    assert PartSource.objects.get(supplier__code="s2").cost_cents == 700
    assert ReviewItem.objects.count() == 1


def test_resolving_as_a_new_part_needs_a_brand_when_the_row_has_none(tmp_path):
    load(tmp_path, "s2", [row("H4123A", "")])
    item = ReviewItem.objects.get()
    with pytest.raises(ValueError, match="no brand"):
        resolve_review(item, new_part=True)
    resolve_review(ReviewItem.objects.select_related("norm_row__raw").get(), new_part=True, brand="MARLOW")
    assert parts() == [("MARLOW", "H4123A")] and ReviewItem.objects.get().status == "new_part"


def test_ignoring_a_listing_keeps_it_out_of_the_catalog(tmp_path):
    load(tmp_path, "s2", [row("H4123A", "")], name="a.csv")
    resolve_review(ReviewItem.objects.get(), ignore=True)
    load(tmp_path, "s2", [row("H4123A", "", cost="8.00")], name="b.csv")
    assert Part.objects.count() == 0 and PartSource.objects.count() == 0
    assert sorted(NormalizedRow.objects.values_list("match_state", flat=True)) == ["skipped", "skipped"]


def test_a_review_can_only_be_resolved_once_and_with_exactly_one_choice(tmp_path):
    load(tmp_path, "s1", [row("H4123A", "Marlow")])
    load(tmp_path, "s2", [row("H4123A", "")])
    item = ReviewItem.objects.get()
    with pytest.raises(ValueError, match="exactly one"):
        resolve_review(item)
    with pytest.raises(ValueError, match="exactly one"):
        resolve_review(item, part=Part.objects.get(), ignore=True)
    resolve_review(item, ignore=True)
    with pytest.raises(ValueError, match="already"):
        resolve_review(item, ignore=True)


def test_a_second_match_run_changes_nothing(tmp_path):
    load(tmp_path, "s1", [row("H4123A", gtin=gtin(1)), row("ZR4519", "Dunmore")])
    load(tmp_path, "s2", [row("H4123A", gtin=gtin(2)), row("H4123B", gtin=gtin(1)), row("ZR4519", "")])

    def state():
        return (
            sorted(Part.objects.values_list("pk", "brand", "number_norm", "gtin", "updated_at")),
            sorted(PartSource.objects.values_list("pk", "listing_key", "raw_part_id", "cost_cents", "last_seen")),
            sorted(ReviewItem.objects.values_list("pk", "reason", "norm_row_id", "status")),
            sorted(NormalizedRow.objects.values_list("pk", "match_state")),
        )

    before = state()
    run = match_pending()
    assert (run.rows_read, run.rows_written) == (0, 0)
    assert state() == before


def test_every_row_ends_in_exactly_one_state(tmp_path):
    load(tmp_path, "s1", [row("H4123A", gtin=gtin(1)), row("ZR4519", "")])
    load(tmp_path, "s2", [row("H4123A", gtin=gtin(2)), row("H4123B", gtin=gtin(1))])
    assert not NormalizedRow.objects.filter(match_state="pending").exists()
    linked = NormalizedRow.objects.filter(match_state="matched").count()
    queued = NormalizedRow.objects.filter(match_state="review").count()
    assert linked == PartSource.objects.count() and queued == ReviewItem.objects.count() == 3


def test_a_limit_stops_the_run_and_the_next_run_finishes_it(tmp_path):
    s = get_supplier("s1")
    path = write_csv(tmp_path / "f.csv", [row(f"AB{i:04d}") for i in range(50)])
    ingest(s, FileAdapter(path))
    normalize_pending()
    first = match_pending(batch_size=10, limit=20)
    assert first.rows_read == 20 and Part.objects.count() == 20
    rest = match_pending(batch_size=10)
    assert rest.rows_read == 30 and Part.objects.count() == 50
