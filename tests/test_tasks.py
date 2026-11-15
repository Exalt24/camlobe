"""Background feed runs: the Celery task is the ingest command run by a worker, so what it must prove is the hand-off.

Four things matter and each has a test in both directions: a run goes through and a second run changes nothing, a worker
that loses the Postgres lock steps aside instead of failing or retrying, a dropped connection is retried and a bad file is
not, and a real worker on a real Redis picks the job up. The last one skips itself when Redis is not running."""
import os
import socket
import subprocess
import sys
import time

import pytest
from django.conf import settings as django_settings
from django.db import OperationalError, connection

from catalog import tasks
from catalog.models import PartSource, PipelineRun, RawPart
from catalog.tasks import LOCKED, TRANSIENT, ingest_feed_task

from .helpers import row, write_csv

pytestmark = pytest.mark.django_db(transaction=True)


def test_a_task_run_stores_the_feed_and_a_second_run_changes_nothing(tmp_path):
    path = write_csv(tmp_path / "f.csv", [row("H1001A"), row("H1002A")])
    assert ingest_feed_task.apply(args=["s1", {"file": str(path)}]).get() == "done"
    stored = RawPart.objects.count()
    assert stored == 2
    assert ingest_feed_task.apply(args=["s1", {"file": str(path)}]).get() == "done"
    assert RawPart.objects.count() == stored
    assert PipelineRun.objects.filter(status="succeeded").count() >= 1


def test_a_worker_that_loses_the_lock_steps_aside(tmp_path, monkeypatch):
    from django.core.management.base import CommandError

    def locked(*a, **k):
        raise CommandError(LOCKED)

    monkeypatch.setattr(tasks, "call_command", locked)
    result = ingest_feed_task.apply(args=["s1", {"file": "x"}])
    assert result.get() == "skipped: already running"
    assert result.successful()


def test_another_command_error_is_a_failure_and_is_not_retried(tmp_path, monkeypatch):
    from django.core.management.base import CommandError

    calls = []

    def bad(*a, **k):
        calls.append(1)
        raise CommandError("run 7 ended failed")

    monkeypatch.setattr(tasks, "call_command", bad)
    result = ingest_feed_task.apply(args=["s1", {"file": "x"}])
    assert result.failed() and len(calls) == 1


def test_a_dropped_connection_is_retried_with_backoff(monkeypatch):
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) < 3:
            raise OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(tasks, "call_command", flaky)
    monkeypatch.setattr(ingest_feed_task, "retry_backoff", 0)
    result = ingest_feed_task.apply(args=["s1", {"file": "x"}])
    assert result.successful() and len(calls) == 3


def test_the_retry_settings_are_the_ones_the_docs_state():
    assert OperationalError in TRANSIENT and ConnectionError in TRANSIENT
    assert ingest_feed_task.max_retries == 5
    assert django_settings.CELERY_TASK_ACKS_LATE and django_settings.CELERY_WORKER_PREFETCH_MULTIPLIER == 1


def test_the_schedule_comes_from_the_environment_and_nothing_is_scheduled_by_default(settings):
    from celery import Celery

    from config.celery import schedule_feeds

    app = Celery("t", set_as_current=False)   # a second current app would steal the shared task's broker for every later test
    app.conf.beat_schedule = {}
    settings.CAMLOBE_FEED_SCHEDULE = {}
    schedule_feeds(app)
    assert app.conf.beat_schedule == {}
    settings.CAMLOBE_FEED_SCHEDULE = {"acme-nightly": {"supplier": "acme", "source": {"file": "feeds/acme.csv"}, "crontab": "0 3 * * *"}}
    schedule_feeds(app)
    assert list(app.conf.beat_schedule) == ["acme-nightly"]


def redis_up() -> bool:
    try:
        host, port = django_settings.CELERY_BROKER_URL.split("//")[1].split("/")[0].split(":")
        socket.create_connection((host, int(port)), timeout=2).close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not redis_up(), reason="Redis is not running: docker compose up -d redis")
def test_a_real_worker_on_a_real_redis_runs_the_job(tmp_path):
    path = write_csv(tmp_path / "f.csv", [row("H1001A")])
    from config.celery import app

    app.control.purge()   # a job left in the queue by an earlier run must not be this test's answer
    # the worker is its own process: it has to read the TEST database, not the development one
    env = {**os.environ, "CAMLOBE_DB_NAME": connection.settings_dict["NAME"]}
    worker = subprocess.Popen(
        [sys.executable, "-m", "celery", "-A", "config", "worker", "--pool=solo", "--concurrency=1", "-l", "warning"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
    )
    try:
        deadline = time.time() + 30
        result = None
        last = ""
        while time.time() < deadline and result is None:
            try:
                result = ingest_feed_task.delay("s1", {"file": str(path)}).get(timeout=20)
            except Exception as exc:
                last = repr(exc)
                time.sleep(1)
        assert result == "done", last
        assert RawPart.objects.count() == 1
    finally:
        worker.terminate()
        worker.wait(timeout=15)
