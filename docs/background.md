# Background runs

A feed run is long, and it must not die with a web request or a terminal. `catalog/tasks.py` wraps the `ingest_feed` command in a Celery task, so a worker runs exactly the code the command runs, with the same Postgres lock and the same checkpoint.

```bash
docker compose up -d --wait redis
celery -A config worker --pool=solo -l info        # on Linux the default pool is fine
celery -A config beat -l info                       # only needed for scheduled feeds
```

Run one feed from a Python shell: `ingest_feed_task.delay("redline", {"file": "demo-data/redline_catalog.csv"})`. The second argument holds the command's own options.

## What the task does with each outcome

| Outcome | What happens | Why |
|---|---|---|
| The run finishes | the task returns `done` | the run record in `/api/feed-runs/` holds the counts |
| Another run of the same source holds the Postgres lock | the task returns `skipped: already running` and does not retry | a second worker has nothing to add, and retrying would only queue behind the first |
| The database or the network drops (`OperationalError`, `InterfaceError`, `ConnectionError`, `TimeoutError`) | retried with exponential backoff and jitter, 30 s growing to 10 minutes, at most 5 times | the run resumes at its checkpoint, so a retry repeats no finished work |
| The feed itself is bad (a renamed column, a refused file) | the task fails at once, no retry | running the same bad file again changes nothing |
| The worker dies in the middle | the job goes back to Redis (`acks_late`, `reject_on_worker_lost`) and another worker picks it up | one long feed per worker slot (`prefetch_multiplier` 1), so a dead worker loses one job, not a queue of them |

Soft time limit 50 minutes, hard limit 60. The Redis address comes from `CAMLOBE_REDIS_URL` (default `redis://localhost:6381/0`).

## Schedules

Nothing is scheduled by default. Set `CAMLOBE_FEED_SCHEDULE` to JSON and beat runs each entry:

```json
{"redline-nightly": {"supplier": "redline", "source": {"file": "feeds/redline.csv"}, "crontab": "0 3 * * *"}}
```

## What the tests prove

`tests/test_tasks.py` has seven tests: a run stores the feed and a second run changes nothing, a lost lock steps aside, another command error fails without a retry, a dropped connection is retried until it works, the retry and acknowledgement settings are the ones above, the schedule comes from the environment, and a real worker on a real Redis picks a job up and writes the rows. Five changes to `catalog/tasks.py` (the lock check removed, the retry list emptied, retries set to zero, the command not called, every command error swallowed) each broke at least one of them.

## The feed runs page

`/runs/` is one HTML page that loads AG Grid Community at a pinned version from a CDN and reads `/api/feed-runs/`. `tests/test_grid_page.py` checks that the version is pinned, that every column reads a field the API gives, and that the page makes no database query. Four changes to `catalog/api/grid.py` (an unpinned version, a column on a field the API lacks, the wrong endpoint, a renamed count key) each broke a test.
