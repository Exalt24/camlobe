"""The REST endpoints: search, part detail, feed run status, fitment, the review queue."""
import pytest
from django.test import Client

from catalog.fitment import load_fitment
from catalog.models import Fitment, Part, PipelineRun
from catalog.search import index

from .helpers import get_supplier, row, run_all, write_csv
from .test_fitment import HEADER

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def world(tmp_path, db_search):
    """Two suppliers, three parts, a queued row, a rejected row and a little fitment."""
    run_all(
        get_supplier("redline"),
        write_csv(
            tmp_path / "r.csv",
            [
                row("MF-H4123A", "Marlow", "Oil filter", "12.50", "5012345678900"),
                row("ZR 4519", "Dunmore", "Spark plug iridium", "8.20"),
                row("K80123", "Halden", "Strut assembly front", "140.00"),
                row("H4123B", "", "Oil filter no brand", "9.00"),
                row("", "Marlow", "Blank number", "1.00"),
            ],
        ),
    )
    run_all(
        get_supplier("delta"),
        write_csv(tmp_path / "d.csv", [row("h4123a", "MARLOW FILTRATION", "Oil filter", "11.00", "5012345678900")]),
    )
    fit = tmp_path / "fit.csv"
    write_csv(
        fit,
        [
            ["Marlow", "H4123A", "2019", "Altair", "T200", "", "2.0L I4", "", ""],
            ["Marlow", "H4123A", "2019", "Altair", "T200", "Sport", "3.5L V6", "", "sport trim"],
            ["Marlow", "H4123A", "2020", "Altair", "T200", "", "2.0L I4", "", ""],
            ["Halden", "K80123", "2019", "Altair", "T200", "", "2.0L I4", "front", ""],
            ["Halden", "K80123", "2019", "Brandt", "Kombi", "", "1.8L I4", "front", ""],
        ],
        header=HEADER,
    )
    load_fitment(fit)
    return Client()


def get(client, url, **params):
    return client.get(url, params)


def numbers(response):
    return [r["number"] for r in response.json()["results"]]


# ---- search ----------------------------------------------------------------------------------------


def test_search_by_a_messy_part_number(world):
    r = get(world, "/api/parts/search/", q="h-4123 a")
    assert r.status_code == 200
    body = r.json()
    assert numbers(r) == ["H4123A"] and body["count"] == 1
    hit = body["results"][0]
    assert hit["suppliers"] == ["delta", "redline"] and hit["supplier_count"] == 2
    assert hit["lowest_cost_cents"] == 1100 and isinstance(hit["lowest_cost_cents"], int)


def test_search_by_prefix_text_gtin_and_brand(world):
    assert numbers(get(world, "/api/parts/search/", q="zr45")) == ["ZR4519"]
    assert numbers(get(world, "/api/parts/search/", q="strut front")) == ["K80123"]
    assert numbers(get(world, "/api/parts/search/", q="5012-3456-78900")) == ["H4123A"]
    assert numbers(get(world, "/api/parts/search/", q="filter", brand="marlow")) == ["H4123A"]
    assert numbers(get(world, "/api/parts/search/", q="filter", brand="dunmore")) == []


def test_search_never_matches_a_number_that_is_one_character_off(world):
    assert numbers(get(world, "/api/parts/search/", q="H4123C")) == []
    assert numbers(get(world, "/api/parts/search/", q="H4124A")) == []


def test_search_pagination(world):
    # "s" starts a word of two parts (Spark plug, Strut assembly): a text word is a word prefix, not a substring
    r = get(world, "/api/parts/search/", q="s", limit=1, offset=0)
    assert r.json()["count"] == 2 and len(r.json()["results"]) == 1
    second = get(world, "/api/parts/search/", q="s", limit=1, offset=1)
    assert len(second.json()["results"]) == 1 and not set(numbers(r)) & set(numbers(second))
    assert get(world, "/api/parts/search/", q="s", limit=1, offset=2).json()["results"] == []


@pytest.mark.parametrize(
    "params,field",
    [
        ({}, "q"),
        ({"q": "  "}, "q"),
        ({"q": "x" * 101}, "q"),
        ({"q": "a", "limit": "0"}, "limit"),
        ({"q": "a", "limit": "101"}, "limit"),
        ({"q": "a", "limit": "ten"}, "limit"),
        ({"q": "a", "offset": "-1"}, "offset"),
    ],
)
def test_search_refuses_bad_input_with_the_field_named(world, params, field):
    r = get(world, "/api/parts/search/", **params)
    assert r.status_code == 400 and field in r.json()


def test_the_database_backend_and_meilisearch_answer_alike(world, meili, settings):
    queries = [("h4123a", ""), ("zr4", ""), ("marlow h4123a", ""), ("5012345678900", ""), ("spark plug", ""), ("filter", "marlow"), ("H4123C", "")]
    settings.SEARCH_BACKEND = "db"
    from_db = {q + "|" + b: sorted(numbers(get(world, "/api/parts/search/", q=q, brand=b))) for q, b in queries}
    index.rebuild()
    settings.SEARCH_BACKEND = "meili"
    from_meili = {q + "|" + b: sorted(numbers(get(world, "/api/parts/search/", q=q, brand=b))) for q, b in queries}
    assert from_db == from_meili
    assert from_db["h4123a|"] == ["H4123A"] and from_db["H4123C|"] == []


def test_a_down_search_server_is_a_503_not_a_500(world, settings):
    settings.SEARCH_BACKEND = "meili"
    settings.MEILI_URL = "http://127.0.0.1:9"
    r = get(world, "/api/parts/search/", q="h4123a")
    assert r.status_code == 503 and "unavailable" in r.json()["detail"]


# ---- part detail -----------------------------------------------------------------------------------


def test_part_detail_has_sources_and_fitment(world):
    part = Part.objects.get(number_norm="H4123A")
    r = get(world, f"/api/parts/{part.pk}/")
    assert r.status_code == 200
    body = r.json()
    assert (body["brand"], body["number"], body["gtin"]) == ("MARLOW", "H4123A", "05012345678900")
    sources = {s["supplier"]: s for s in body["sources"]}
    assert set(sources) == {"delta", "redline"}
    assert sources["redline"]["cost_cents"] == 1250 and sources["redline"]["currency"] == "USD"
    assert sources["redline"]["supplier_part_number"] == "MF-H4123A"
    assert sorted((f["year"], f["engine"]) for f in body["fitment"]) == [(2019, "2.0L I4"), (2019, "3.5L V6"), (2020, "2.0L I4")]


def test_part_detail_404_and_a_constant_number_of_queries(world, django_assert_max_num_queries):
    assert get(world, "/api/parts/999999/").status_code == 404
    part = Part.objects.get(number_norm="H4123A")
    with django_assert_max_num_queries(4):
        get(world, f"/api/parts/{part.pk}/")


# ---- feed runs -------------------------------------------------------------------------------------


def test_feed_run_list_and_detail(world):
    runs = get(world, "/api/feed-runs/", kind="ingest").json()
    assert runs["count"] == 2
    newest = runs["results"][0]
    assert newest["supplier"] == "delta" and newest["status"] == "succeeded"
    assert newest["counts"] == {"read": 1, "written": 1, "unchanged": 0, "rejected": 0}
    redline = next(r for r in runs["results"] if r["supplier"] == "redline")
    detail = get(world, f"/api/feed-runs/{redline['id']}/").json()
    assert detail["counts"]["rejected"] == 1 and detail["rejected_rows"] == {"blank_part_number": 1}
    assert redline["rejected_rows"] == {}  # the list does not run the extra query


def test_feed_run_filters_and_errors(world):
    assert get(world, "/api/feed-runs/", kind="match").json()["count"] == 2
    assert get(world, "/api/feed-runs/", supplier="delta", kind="ingest").json()["count"] == 1
    assert get(world, "/api/feed-runs/", kind="nonsense").status_code == 400
    assert get(world, "/api/feed-runs/", status="nonsense").status_code == 400
    assert get(world, "/api/feed-runs/424242/").status_code == 404


def test_a_failed_run_shows_its_error(tmp_path, db_search):
    from catalog.ingest.adapters import FileAdapter
    from catalog.ingest.runner import ingest

    path = write_csv(tmp_path / "bad.csv", [row("H4123A")], header=["PartNo", "Producer", "Description", "Cost", "UPC"])
    ingest(get_supplier(), FileAdapter(path))
    body = get(Client(), "/api/feed-runs/", status="failed").json()
    assert body["count"] == 1 and "no column maps to brand" in body["results"][0]["errors"][0]["message"]


# ---- fitment ---------------------------------------------------------------------------------------


def test_fitment_for_a_vehicle(world):
    r = get(world, "/api/fitment/", year=2019, make="altair", model="t200")
    assert r.status_code == 200
    rows = r.json()["results"]
    assert sorted((x["number"], x["engine"]) for x in rows) == [("H4123A", "2.0L I4"), ("H4123A", "3.5L V6"), ("K80123", "2.0L I4")]
    assert [x for x in rows if x["number"] == "K80123"][0]["position"] == "front"


def test_fitment_does_not_leak_other_vehicles(world):
    assert {x["number"] for x in get(world, "/api/fitment/", year=2020, make="Altair", model="T200").json()["results"]} == {"H4123A"}
    assert {x["number"] for x in get(world, "/api/fitment/", year=2019, make="Brandt", model="Kombi").json()["results"]} == {"K80123"}
    assert get(world, "/api/fitment/", year=2018, make="Altair", model="T200").json()["results"] == []
    assert get(world, "/api/fitment/", year=2019, make="Altair", model="T300").json()["results"] == []


def test_fitment_narrowed_by_submodel_and_engine(world):
    sport = get(world, "/api/fitment/", year=2019, make="Altair", model="T200", submodel="sport").json()["results"]
    assert [x["note"] for x in sport] == ["sport trim"]
    v6 = get(world, "/api/fitment/", year=2019, make="Altair", model="T200", engine="3.5l v6").json()["results"]
    assert [x["engine"] for x in v6] == ["3.5L V6"]


@pytest.mark.parametrize(
    "params,field",
    [({"make": "A", "model": "B"}, "year"), ({"year": "abc", "make": "A", "model": "B"}, "year"), ({"year": 2019, "model": "B"}, "make"), ({"year": 2019, "make": "A"}, "model")],
)
def test_fitment_needs_year_make_and_model(world, params, field):
    r = get(world, "/api/fitment/", **params)
    assert r.status_code == 400 and field in r.json()


# ---- review queue and read only --------------------------------------------------------------------


def test_the_review_queue_lists_what_the_rules_would_not_decide(world):
    r = get(world, "/api/review/")
    items = r.json()["results"]
    assert [(i["reason"], i["supplier"], i["supplier_part_number"]) for i in items] == [("no_brand", "redline", "H4123B")]
    assert get(world, "/api/review/", status="linked").json()["results"] == []
    assert get(world, "/api/review/", status="nope").status_code == 400


@pytest.mark.parametrize("url", ["/api/parts/search/?q=a", "/api/parts/1/", "/api/feed-runs/", "/api/fitment/", "/api/review/"])
def test_nothing_can_be_written_through_the_api(world, url):
    for method in ("post", "put", "patch", "delete"):
        assert getattr(world, method)(url).status_code == 405
    assert Fitment.objects.count() == 5 and PipelineRun.objects.filter(kind="ingest").count() == 2
