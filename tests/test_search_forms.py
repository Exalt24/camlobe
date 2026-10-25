"""A search finds a part by any form a supplier would print its number in, and both backends answer alike.

Each test runs on the database backend and on Meilisearch (the `backend` fixture), against the same small world.
"""
import pytest
from django.test import Client

from catalog.search import index

from .helpers import get_supplier, row, run_all, write_csv

pytestmark = pytest.mark.django_db(transaction=True)

WORLD = [
    row("0766611428", "Kestrel", "Brake pad set front", "40.00"),  # Kestrel numbers start with a real zero
    row("DU-ZR7208", "Dunmore", "Spark plug iridium", "8.20"),  # the supplier glues its house prefix on
    row("MF-H4123A", "Marlow", "Oil filter spin on", "12.50", "5012345678900"),
    row("0000766611428", "Marlow", "Cabin filter padded export", "9.00"),  # a padded export of another brand's 766611428
    row("DUZR7208", "Tarn", "Thermostat housing", "21.00"),  # a Tarn number that really begins with DU
    row("K80123", "Halden", "Strut assembly front", "140.00"),
]


@pytest.fixture
def world(tmp_path):
    run_all(get_supplier("redline"), write_csv(tmp_path / "w.csv", WORLD))


@pytest.fixture(params=["db", "meili"])
def backend(request, world, settings):
    if request.param == "meili":
        request.getfixturevalue("meili")
        index.rebuild()
    settings.SEARCH_BACKEND = request.param
    return request.param


def ask(q, **params):
    body = Client().get("/api/parts/search/", {"q": q, "limit": 100, **params}).json()
    return sorted(f"{r['brand']}:{r['number']}" for r in body["results"]), body["count"]


def test_a_kestrel_number_is_found_with_its_leading_zero_and_no_brand_word(backend):
    found, _ = ask("0766611428")
    assert "KESTREL:0766611428" in found
    # the padded export of another brand has the same digits after its zeros: both answer, none is picked for the caller
    assert found == ["KESTREL:0766611428", "MARLOW:766611428"]
    assert ask("kestrel 0766611428")[0] == ["KESTREL:0766611428"]
    assert ask("kb-0766611428")[0] == ["KESTREL:0766611428"]  # Kestrel's own prefix, then its zero kept


def test_a_number_as_the_supplier_prints_it_finds_the_canonical_part(backend):
    assert ask("DU-ZR7208")[0] == ["DUNMORE:ZR7208", "TARN:DUZR7208"]  # the Dunmore form and the number as typed, both
    assert ask("du zr 7208")[0] == ["DUNMORE:ZR7208", "TARN:DUZR7208"]
    assert ask("ZR7208")[0] == ["DUNMORE:ZR7208"]
    assert ask("MF-H4123A")[0] == ["MARLOW:H4123A"]
    assert ask("mrl h4123a")[0] == ["MARLOW:H4123A"]


def test_a_form_that_belongs_to_one_brand_matches_only_that_brand(backend):
    assert ask("MF-ZR7208")[0] == []  # Marlow's prefix, and Marlow has no ZR7208: the Dunmore part is not a guess
    assert ask("KB-ZR7208")[0] == []
    assert ask("DU-ZR7209")[0] == []  # one character off is another part


def test_a_brand_in_the_query_or_the_parameter_narrows_every_form(backend):
    assert ask("dunmore du-zr7208")[0] == ["DUNMORE:ZR7208"]
    assert ask("DU-ZR7208", brand="tarn")[0] == ["TARN:DUZR7208"]
    assert ask("DU-ZR7208", brand="marlow")[0] == []
    assert ask("kestrel du-zr7208", brand="tarn")[0] == []  # two brands asked for at once


@pytest.mark.parametrize("q", ["---", "#", "...", " - ", "/", "()"])
def test_a_query_with_no_letter_or_digit_returns_nothing(backend, q):
    assert ask(q) == ([], 0)


def test_a_text_query_searches_brand_and_description_only_never_the_number(backend):
    assert ask("ZR") == ([], 0)  # the number ZR7208 is not text: it is found by a query with a digit
    assert ask("DU")[0] == ["DUNMORE:ZR7208"]  # only because DUNMORE is a brand word: the number is not searched
    assert ask("oil")[0] == ["MARLOW:H4123A"]
    assert ask("mar")[0] == ["MARLOW:766611428", "MARLOW:H4123A"]  # a word prefix of the brand
    assert ask("filter oil")[0] == ["MARLOW:H4123A"]  # every word must be there, in any order
    assert ask("brake zzz") == ([], 0)  # a word that is nowhere does not get dropped to find the rest


QUERIES = [
    "0766611428", "kestrel 0766611428", "kb-0766611428", "DU-ZR7208", "du zr 7208", "ZR7208", "zr72", "MF-H4123A", "h4123a",
    "marlow mf-h4123a", "mrl h4123a", "5012345678900", "5012-3456-78900", "5012345678917", "766611428", "0766", "7666",
    "---", "#", " - ", ".", "ZR", "zr", "DU", "oil", "oil filter", "filter oil", "mar", "marlow", "brake pad", "brake zzz",
    "strut", "str", "spark plug", "a", "x", "1", "-1", "9", "H4123C", "h4124a", "pad set front", "cabin filter", "tarn thermostat",
]


def test_both_backends_give_the_same_answer_to_the_same_queries(world, meili, settings):
    answers = {}
    for backend in ("db", "meili"):
        settings.SEARCH_BACKEND = backend
        if backend == "meili":
            index.rebuild()
        answers[backend] = {(q, b): ask(q, brand=b) for q in QUERIES for b in ("", "marlow")}
    different = {k: (answers["db"][k], answers["meili"][k]) for k in answers["db"] if answers["db"][k] != answers["meili"][k]}
    assert different == {}


def test_search_legs_are_one_reading_for_both_backends():
    from catalog.search.documents import search_legs  # imported here so the other tests also run against older code

    assert search_legs("---") == []
    kinds = {leg["kind"] for leg in search_legs("5012345678900")}
    assert kinds == {"gtin", "number"}  # a GTIN is also tried as a number: a part number can look like one
    assert [leg["value"] for leg in search_legs("zr oil")] == ["zr oil"] and search_legs("zr oil")[0]["kind"] == "text"
    assert search_legs("kestrel du-zr7208", "tarn") == []
