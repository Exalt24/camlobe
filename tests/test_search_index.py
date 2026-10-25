"""Meilisearch: a rebuild that resumes, an incremental sync with no clock, and the way part numbers are searched."""
from unittest import mock

import pytest
from django.conf import settings
from django.db.models import F

from catalog.ingest.adapters import FileAdapter
from catalog.ingest.runner import ingest
from catalog.matching import match_pending
from catalog.models import Part, PipelineRun, SearchIndexState
from catalog.normalize_step import normalize_pending
from catalog.search import index
from catalog.search.documents import documents_for, interpret_query

from .helpers import get_supplier, row, run_all, write_csv

pytestmark = pytest.mark.django_db(transaction=True)

BRAND_FOR = {0: "Marlow", 1: "Dunmore", 2: "Halden"}


def seed(tmp_path, n=55, name="a.csv", supplier="s1"):
    rows = []
    for i in range(n):
        brand = BRAND_FOR[i % 3]
        number = {"Marlow": f"H{4100 + i}A", "Dunmore": f"ZR{4100 + i}", "Halden": f"K80{100 + i}"}[brand]
        rows.append(row(number, brand, f"{['Oil filter', 'Spark plug', 'Strut assembly'][i % 3]} number {i}", f"{i + 1}.00"))
    run_all(get_supplier(supplier), write_csv(tmp_path / name, rows))


def staging_exists(client):
    import meilisearch

    try:
        client.get_index(index.staging_uid())
        return True
    except meilisearch.errors.MeilisearchApiError:
        return False


def test_query_interpretation():
    def main(q):
        got = interpret_query(q)
        return {k: got[k] for k in ("kind", "value", "brand")}

    assert main("h-4123 a") == {"kind": "number", "value": "H4123A", "brand": ""}
    assert main("marlow h4123a") == {"kind": "number", "value": "H4123A", "brand": "MARLOW"}
    assert main("5012345678900") == {"kind": "gtin", "value": "05012345678900", "brand": ""}
    assert main("oil filter") == {"kind": "text", "value": "oil filter", "brand": ""}
    assert interpret_query("  ")["kind"] == "empty"  # nothing to search for: an empty answer, not the whole catalog
    assert interpret_query("a1")["value"] == "A1"  # too short to be a number: still searched as typed, not dropped
    assert ("", "H4123A") in interpret_query("h-4123 a")["forms"]


def test_a_rebuild_puts_every_part_in_the_live_index_and_removes_the_staging_one(tmp_path, meili):
    seed(tmp_path)
    run = index.rebuild(batch_size=10)
    assert run.status == PipelineRun.Status.SUCCEEDED and run.rows_written == 55
    assert meili.index(settings.MEILI_INDEX).get_stats().number_of_documents == 55
    assert not staging_exists(meili)
    state = SearchIndexState.objects.get()
    assert state.status == "ready" and state.docs_sent == 55
    assert not Part.objects.filter(indexed_version__lt=F("search_version")).exists()


def test_a_killed_rebuild_resumes_at_the_last_confirmed_batch(tmp_path, meili):
    seed(tmp_path)
    real_wait = index._wait
    calls = {"n": 0}

    def dying_wait(cl, task):
        calls["n"] += 1
        # calls: staging settings, create, then add, add, add ... die on the third add
        if calls["n"] == 7:
            raise ConnectionError("the process was killed")
        return real_wait(cl, task)

    with mock.patch.object(index, "_wait", side_effect=dying_wait):
        failed = index.rebuild(batch_size=10)
    assert failed.status == PipelineRun.Status.FAILED
    state = SearchIndexState.objects.get()
    assert state.status == "building" and 0 < state.rebuild_after_id < Part.objects.order_by("id").last().id
    resumed = index.rebuild(batch_size=10)
    assert resumed.status == PipelineRun.Status.SUCCEEDED
    assert resumed.notes["resumed_after_part_id"] == state.rebuild_after_id
    assert meili.index(settings.MEILI_INDEX).get_stats().number_of_documents == 55
    assert resumed.rows_written < 55  # it did not start over


def test_a_batch_sent_twice_does_not_duplicate_documents(tmp_path, meili):
    seed(tmp_path, n=20)
    index.rebuild(batch_size=7, stop_after_batches=1)
    SearchIndexState.objects.update(rebuild_after_id=0)  # pretend the cursor was lost after the batch was confirmed
    index.rebuild(batch_size=7)
    assert meili.index(settings.MEILI_INDEX).get_stats().number_of_documents == 20


def test_restart_throws_the_partial_build_away(tmp_path, meili):
    seed(tmp_path, n=20)
    index.rebuild(batch_size=5, stop_after_batches=2)
    run = index.rebuild(batch_size=5, restart=True)
    assert "resumed_after_part_id" not in run.notes and run.rows_written == 20


def test_searches_see_the_old_index_until_the_new_one_is_swapped_in(tmp_path, meili):
    seed(tmp_path, n=12)
    index.rebuild(batch_size=5)
    part = Part.objects.get(number_norm="H4100A")
    Part.objects.filter(pk=part.pk).update(description="Brand new description", search_version=F("search_version") + 1)
    index.rebuild(batch_size=5, restart=True, stop_after_batches=1)  # a rebuild is half way
    assert index.search("H4100A")["results"][0]["description"] != "Brand new description"
    index.rebuild(batch_size=5)  # resumes and swaps
    assert index.search("H4100A")["results"][0]["description"] == "Brand new description"


def test_the_sync_sends_only_what_changed_and_a_second_sync_sends_nothing(tmp_path, meili):
    seed(tmp_path, n=30)
    index.rebuild(batch_size=10)
    assert index.sync().rows_written == 0
    seed(tmp_path, n=40, name="b.csv")  # 10 new parts and 30 untouched listings with the same price
    assert Part.objects.filter(indexed_version__lt=F("search_version")).count() == 10
    run = index.sync(batch_size=4)
    assert run.status == PipelineRun.Status.SUCCEEDED and run.rows_written == 10
    assert meili.index(settings.MEILI_INDEX).get_stats().number_of_documents == 40
    assert index.sync().rows_written == 0


def test_a_price_change_on_a_listing_reaches_the_index(tmp_path, meili):
    seed(tmp_path, n=6)
    index.rebuild()
    before = index.search("H4100A")["results"][0]
    assert before["lowest_cost_cents"] == 100
    run_all(get_supplier("s1"), write_csv(tmp_path / "price.csv", [row("H4100A", "Marlow", "Oil filter number 0", "0.75")]))
    assert index.sync().rows_written == 1
    assert index.search("H4100A")["results"][0]["lowest_cost_cents"] == 75


def test_a_second_supplier_shows_in_the_suppliers_list(tmp_path, meili):
    seed(tmp_path, n=6)
    index.rebuild()
    run_all(get_supplier("s2"), write_csv(tmp_path / "s2.csv", [row("H4100A", "MARLOW", "Oil filter", "3.00")]))
    index.sync()
    hit = index.search("H4100A")["results"][0]
    assert hit["suppliers"] == ["s1", "s2"] and hit["supplier_count"] == 2 and hit["lowest_cost_cents"] == 100


def test_a_part_that_changes_while_it_is_being_sent_stays_dirty(tmp_path, meili):
    seed(tmp_path, n=6)
    index.rebuild()
    run_all(get_supplier("s1"), write_csv(tmp_path / "p.csv", [row("H4100A", "Marlow", "Oil filter number 0", "0.50")]))
    victim = Part.objects.get(number_norm="H4100A")
    real_wait = index._wait

    def change_during_send(cl, task):
        real_wait(cl, task)
        Part.objects.filter(pk=victim.pk).update(search_version=F("search_version") + 1)  # edited mid send

    with mock.patch.object(index, "_wait", side_effect=change_during_send):
        index.sync()
    victim.refresh_from_db()
    assert victim.indexed_version < victim.search_version
    assert index.sync().rows_written == 1
    victim.refresh_from_db()
    assert victim.indexed_version == victim.search_version


def test_the_sync_refuses_before_a_build_and_during_one(tmp_path, meili):
    seed(tmp_path, n=6)
    run = index.sync()
    assert run.status == PipelineRun.Status.FAILED and "not been built" in run.errors[0]["message"]
    index.rebuild(batch_size=2, stop_after_batches=1)
    run = index.sync()
    assert run.status == PipelineRun.Status.FAILED and "rebuild is in progress" in run.errors[0]["message"]


def test_part_numbers_are_found_by_messy_input_and_never_fuzzily(tmp_path, meili):
    seed(tmp_path, n=30)
    index.rebuild()
    ids = lambda q, **kw: sorted(h["number"] for h in index.search(q, **kw)["results"])
    assert ids("h-4100 a") == ["H4100A"]
    assert ids("h41") == sorted(f"H{4100 + i}A" for i in range(0, 30, 3))  # a prefix of the number
    assert ids("H4101A") == []  # i is a Dunmore part, so no such Marlow number
    assert ids("H4106A") == ["H4106A"]
    assert ids("H4107A") == []  # one character off H4106A: another part, never a typo of this one
    assert ids("H4100B") == []


def test_brand_filter_and_a_brand_in_the_query(tmp_path, meili):
    seed(tmp_path, n=30)
    index.rebuild()
    assert {h["brand"] for h in index.search("spark plug", brand="dunmore")["results"]} == {"DUNMORE"}
    assert index.search("spark plug", brand="marlow")["results"] == []
    assert [h["number"] for h in index.search("marlow h4100a")["results"]] == ["H4100A"]
    assert index.search("dunmore h4100a")["results"] == []  # the number is a Marlow one
    assert index.search("h4100a", brand="halden")["results"] == []


def test_a_brand_cannot_carry_filter_syntax(tmp_path, meili):
    seed(tmp_path, n=9)
    index.rebuild()
    assert index.search("oil", brand='x" OR brand != "x')["results"] == []


def test_there_is_no_typo_tolerance_for_numbers_or_words(tmp_path, meili):
    """A stated limit: typo matching is off for the whole index, because a part number one character off is another part."""
    seed(tmp_path, n=12)
    index.rebuild()
    assert index.search("H4130A")["results"] == []
    assert index.search("asembly")["results"] == []
    assert index.search("strut assem")["results"], "a prefix of the last word still works"


def test_a_part_added_after_the_build_is_found_by_its_number(tmp_path, meili):
    seed(tmp_path, n=12)
    index.rebuild()
    run_all(get_supplier("s2"), write_csv(tmp_path / "n.csv", [row("H9999A", "Marlow", "Oil filter late", "2.00")]))
    index.sync()
    assert [h["number"] for h in index.search("H9999A")["results"]] == ["H9999A"]
    assert "H9999A" in [h["number"] for h in index.search("h99")["results"]]


def test_gtin_search(tmp_path, meili):
    run_all(get_supplier(), write_csv(tmp_path / "g.csv", [row("H4100A", "Marlow", "Oil filter", "1.00", "5012345678900")]))
    index.rebuild()
    assert [h["number"] for h in index.search("5012345678900")["results"]] == ["H4100A"]
    assert [h["number"] for h in index.search("5012-3456-78900")["results"]] == ["H4100A"]
    assert index.search("5012345678917")["results"] == []


def test_documents_carry_what_the_api_shows(tmp_path):
    seed(tmp_path, n=3)
    docs = documents_for(Part.objects.all())
    assert len(docs) == 3
    d = docs[0]
    assert set(d) == {"id", "brand", "number", "description", "gtin", "suppliers", "supplier_count", "lowest_cost_cents", "version"}
    assert d["suppliers"] == ["s1"] and d["lowest_cost_cents"] == 100


def test_an_unreachable_search_server_is_reported_not_hidden(settings, tmp_path):
    settings.MEILI_URL = "http://127.0.0.1:9"
    seed(tmp_path, n=3)
    run = index.rebuild()
    assert run.status == PipelineRun.Status.FAILED
    with pytest.raises(Exception):
        index.search("H4100A")


def test_the_index_is_rebuildable_from_the_database_alone(tmp_path, meili):
    seed(tmp_path, n=15)
    index.rebuild()
    first = sorted((h["id"], h["number"]) for h in index.search("oil", limit=100)["results"])
    meili.delete_index(settings.MEILI_INDEX)
    SearchIndexState.objects.all().delete()
    index.rebuild()
    assert sorted((h["id"], h["number"]) for h in index.search("oil", limit=100)["results"]) == first
