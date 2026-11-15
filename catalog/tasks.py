"""Background feed runs. The task is the ingest_feed command run by a worker: same code, same lock, same checkpoint."""
from __future__ import annotations

import logging

from celery import shared_task
from django.core.management import CommandError, call_command
from django.db import InterfaceError, OperationalError

log = logging.getLogger("catalog")
# What is worth running again: the database or the network dropped. A bad file or a bad mapping is not.
LOCKED = "another ingest of this source is running"   # the message ingest_feed gives when it loses the Postgres lock
TRANSIENT = (OperationalError, InterfaceError, ConnectionError, TimeoutError)


@shared_task(
    bind=True,
    name="catalog.ingest_feed",
    autoretry_for=TRANSIENT,
    retry_backoff=30,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def ingest_feed_task(self, supplier: str, source: dict) -> str:
    """Run one supplier feed. `source` holds the command's own options, for example {"file": "feeds/acme.csv"}.

    Another run of the same supplier holding the Postgres lock is not an error and not a retry: this one steps aside.
    The run resumes at its checkpoint, so a retry after a dropped connection repeats no finished work."""
    try:
        call_command("ingest_feed", supplier, **source)
    except CommandError as exc:
        if str(exc).startswith(LOCKED):
            log.info("ingest %s skipped: another run holds the lock", supplier)
            return "skipped: already running"
        raise
    return "done"
