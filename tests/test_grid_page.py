"""The feed runs page: it is a thin client of the REST endpoint, so what can break is the contract between the two."""
import re

import pytest
from django.urls import reverse

from catalog.api.grid import AG_GRID_VERSION
from catalog.api.serializers import FeedRunSerializer

pytestmark = pytest.mark.django_db


def test_the_page_loads_a_pinned_grid_and_reads_the_runs_endpoint(client):
    body = client.get(reverse("feed-runs-page")).content.decode()
    assert f"ag-grid-community@{AG_GRID_VERSION}/" in body
    assert re.fullmatch(r"\d+\.\d+\.\d+", AG_GRID_VERSION)   # a range would let the CDN change the page under us
    assert 'fetch("/api/feed-runs/?limit=100")' in body


def test_every_column_reads_a_field_the_api_gives():
    from catalog.api.grid import PAGE

    fields = set(FeedRunSerializer.Meta.fields)
    used = set(re.findall(r'field:"(\w+)"', PAGE))
    assert used and used <= fields, used - fields
    counts = set(re.findall(r"p\.data\.counts\.(\w+)", PAGE))
    assert counts == {"read", "written", "unchanged", "rejected"}


def test_the_page_is_fast_to_serve_and_holds_no_run_data(client, django_assert_num_queries):
    with django_assert_num_queries(0):
        assert client.get(reverse("feed-runs-page")).status_code == 200
