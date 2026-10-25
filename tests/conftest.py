import uuid

import meilisearch
import pytest
from django.conf import settings as django_settings


def meili_up() -> bool:
    try:
        meilisearch.Client(django_settings.MEILI_URL, django_settings.MEILI_KEY, timeout=3).health()
        return True
    except Exception:
        return False


@pytest.fixture
def meili(settings):
    """A private pair of indexes on the local Meilisearch for one test, removed afterwards."""
    if not meili_up():
        pytest.skip("Meilisearch is not running: docker compose up -d search")
    settings.MEILI_INDEX = f"test_{uuid.uuid4().hex[:10]}"
    settings.SEARCH_BACKEND = "meili"
    client = meilisearch.Client(settings.MEILI_URL, settings.MEILI_KEY, timeout=15)
    yield client
    for uid in (settings.MEILI_INDEX, settings.MEILI_INDEX + "_next"):
        try:
            client.wait_for_task(client.delete_index(uid).task_uid, timeout_in_ms=30000)
        except Exception:
            pass


@pytest.fixture
def db_search(settings):
    settings.SEARCH_BACKEND = "db"
