"""Shared pieces of every restartable command: the advisory lock and the run record."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from django.db import connection
from django.utils import timezone

from .models import PipelineRun

log = logging.getLogger("catalog")
MAX_ERRORS = 50


class AlreadyRunning(Exception):
    """Another process holds the lock for this unit of work."""


@contextmanager
def advisory_lock(name: str) -> Iterator[None]:
    """A session level Postgres lock. Released on exit, and by the server if the process is killed."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", [name])
        if not cursor.fetchone()[0]:
            raise AlreadyRunning(name)
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", [name])


def start_run(kind: str, supplier=None, source_ref: str = "") -> PipelineRun:
    """Open a run row. Any earlier run of the same work still marked running died without finishing
    (we hold the lock, so nobody else is running it): it is closed as failed first."""
    stale = PipelineRun.objects.filter(kind=kind, supplier=supplier, source_ref=source_ref, status=PipelineRun.Status.RUNNING)
    stale.update(
        status=PipelineRun.Status.FAILED,
        finished_at=timezone.now(),
        errors=[{"where": "start", "message": "abandoned: the process died before this run finished"}],
    )
    return PipelineRun.objects.create(kind=kind, supplier=supplier, source_ref=source_ref)


def add_error(run: PipelineRun, where: str, message: str) -> None:
    if len(run.errors) < MAX_ERRORS:
        run.errors.append({"where": where, "message": message[:500]})
    elif len(run.errors) == MAX_ERRORS:
        run.errors.append({"where": "...", "message": "further errors not recorded"})


def finish_run(run: PipelineRun, status: str) -> PipelineRun:
    run.status = status
    run.finished_at = timezone.now()
    run.save()
    log.info(
        "%s run %s %s: read=%s written=%s unchanged=%s rejected=%s",
        run.kind, run.pk, status, run.rows_read, run.rows_written, run.rows_unchanged, run.rows_rejected,
    )
    return run
