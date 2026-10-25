"""Meilisearch index work: a restartable rebuild, an incremental sync, and the queries.

Rebuild. Documents go into a staging index (`<name>_next`), a batch at a time, in Part id order. The
cursor is saved only after Meilisearch confirmed the batch, so a killed rebuild resumes at the last
confirmed batch, and a batch sent twice is harmless because a document with the same id replaces itself.
When every part is in, the staging index is swapped with the live one in one step, so searches never see
a half built index.

Sync. A part carries `search_version`, bumped by anything that changes what search shows, and
`indexed_version`, the version last confirmed in the live index. The sync sends the parts where the
second is behind the first, then records the version it sent, only if the part has not changed again
meanwhile. No clock and no cursor: a part changed during a send simply stays dirty for the next run.
The sync refuses to run during a rebuild (its writes would go to the old index and be marked done).
"""
from __future__ import annotations

import meilisearch
from django.conf import settings
from django.db import connection, transaction
from django.db.models import F

from ..models import Part, PipelineRun, SearchIndexState
from ..normalize import canonical_brand
from ..runs import add_error, advisory_lock, finish_run, start_run
from .documents import documents_for, interpret_query

TASK_TIMEOUT_MS = 60_000

SETTINGS = {
    "searchableAttributes": ["number", "brand", "description", "gtin"],
    "filterableAttributes": ["brand", "suppliers"],
    "sortableAttributes": ["lowest_cost_cents", "supplier_count"],
    # A part number one character off is another part, so typo matching is off for the whole index. The per
    # attribute switch (disableOnAttributes) was tried first: on Meilisearch 1.12.8 it made every part number
    # added after the setting unfindable, which the sync tests caught. The cost is no typo tolerance on words.
    "typoTolerance": {"enabled": False},
    "pagination": {"maxTotalHits": 5000},
}


class SearchUnavailable(Exception):
    pass


def client() -> meilisearch.Client:
    return meilisearch.Client(settings.MEILI_URL, settings.MEILI_KEY, timeout=15)


def live_uid() -> str:
    return settings.MEILI_INDEX


def staging_uid() -> str:
    return settings.MEILI_INDEX + "_next"


def _wait(cl: meilisearch.Client, task) -> None:
    uid = task.task_uid if hasattr(task, "task_uid") else task["taskUid"]
    done = cl.wait_for_task(uid, timeout_in_ms=TASK_TIMEOUT_MS, interval_in_ms=50)
    status = done.status if hasattr(done, "status") else done["status"]
    if status != "succeeded":
        error = getattr(done, "error", None)
        raise SearchUnavailable(f"Meilisearch task {uid} {status}: {error}")


def _ensure(cl: meilisearch.Client, uid: str) -> None:
    try:
        cl.get_index(uid)
    except meilisearch.errors.MeilisearchApiError:
        _wait(cl, cl.create_index(uid, {"primaryKey": "id"}))
    _wait(cl, cl.index(uid).update_settings(SETTINGS))


def _delete_if_exists(cl: meilisearch.Client, uid: str) -> None:
    try:
        cl.get_index(uid)
    except meilisearch.errors.MeilisearchApiError:
        return
    _wait(cl, cl.delete_index(uid))


def _state(uid: str) -> SearchIndexState:
    return SearchIndexState.objects.get_or_create(index_uid=uid)[0]


def _mark_indexed(docs: list[dict]) -> None:
    """indexed_version = the version that was sent, but only where the part has not changed since."""
    if not docs:
        return
    values = ",".join("(%s,%s)" for _ in docs)
    params = [x for d in docs for x in (d["id"], d["version"])]
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE catalog_part p SET indexed_version = v.version FROM (VALUES {values}) AS v(id, version) "
            "WHERE p.id = v.id AND p.search_version = v.version AND p.indexed_version < v.version",
            params,
        )


def rebuild(batch_size: int = 1000, restart: bool = False, stop_after_batches: int | None = None) -> PipelineRun:
    """Build the whole index into the staging index and swap it in. Resumes a build that was interrupted."""
    with advisory_lock("search-index"):
        run = start_run(PipelineRun.Kind.INDEX, source_ref="rebuild")
        try:
            cl = client()
            state = _state(live_uid())
            if restart or state.status != "building":
                _delete_if_exists(cl, staging_uid())
                state.status, state.rebuild_after_id, state.docs_sent = "building", 0, 0
                state.save()
            else:
                run.notes["resumed_after_part_id"] = state.rebuild_after_id
            _ensure(cl, staging_uid())
            staging = cl.index(staging_uid())
            batches = 0
            while True:
                ids = list(
                    Part.objects.filter(id__gt=state.rebuild_after_id).order_by("id").values_list("id", flat=True)[:batch_size]
                )
                if not ids:
                    break
                docs = documents_for(Part.objects.filter(id__in=ids))
                _wait(cl, staging.add_documents(docs))
                _mark_indexed(docs)  # versions sent to staging count once staging is swapped in
                with transaction.atomic():
                    state.rebuild_after_id = ids[-1]
                    state.docs_sent += len(docs)
                    state.save()
                    PipelineRun.objects.filter(pk=run.pk).update(
                        rows_read=run.rows_read + len(ids), rows_written=run.rows_written + len(docs)
                    )
                    run.rows_read += len(ids)
                    run.rows_written += len(docs)
                batches += 1
                if stop_after_batches is not None and batches >= stop_after_batches:
                    run.notes["stopped_early"] = True
                    return finish_run(run, PipelineRun.Status.PARTIAL)
            _ensure(cl, live_uid())
            _wait(cl, cl.swap_indexes([{"indexes": [live_uid(), staging_uid()]}]))
            _delete_if_exists(cl, staging_uid())
            state.status, state.rebuild_after_id = "ready", 0
            state.save()
            return finish_run(run, PipelineRun.Status.SUCCEEDED)
        except Exception as exc:
            add_error(run, "rebuild", f"{type(exc).__name__}: {exc}")
            return finish_run(run, PipelineRun.Status.FAILED)


def sync(batch_size: int = 1000) -> PipelineRun:
    """Send the parts changed since they were last indexed to the live index."""
    with advisory_lock("search-index"):
        run = start_run(PipelineRun.Kind.INDEX, source_ref="sync")
        try:
            state = _state(live_uid())
            if state.status == "building":
                raise SearchUnavailable("a rebuild is in progress: finish it (rebuild_search_index) before syncing")
            if state.status != "ready":
                raise SearchUnavailable("the index has not been built yet: run rebuild_search_index first")
            cl = client()
            live = cl.index(live_uid())
            while True:
                ids = list(
                    Part.objects.filter(indexed_version__lt=F("search_version")).order_by("id").values_list("id", flat=True)[:batch_size]
                )
                if not ids:
                    break
                docs = documents_for(Part.objects.filter(id__in=ids))
                _wait(cl, live.add_documents(docs))
                _mark_indexed(docs)
                run.rows_read += len(ids)
                run.rows_written += len(docs)
                PipelineRun.objects.filter(pk=run.pk).update(rows_read=run.rows_read, rows_written=run.rows_written)
                if Part.objects.filter(id__in=ids, indexed_version__lt=F("search_version")).exists():
                    # a part changed while it was being sent: it stays dirty for the next run, and this run must not spin
                    changed = set(Part.objects.filter(id__in=ids, indexed_version__lt=F("search_version")).values_list("id", flat=True))
                    if changed == set(ids):
                        break
            return finish_run(run, PipelineRun.Status.SUCCEEDED)
        except Exception as exc:
            add_error(run, "sync", f"{type(exc).__name__}: {exc}")
            return finish_run(run, PipelineRun.Status.FAILED)


def search(q: str, limit: int = 20, offset: int = 0, brand: str = "") -> dict:
    """Search the live index. Returns {"count": int, "results": [document, ...]}."""
    query = interpret_query(q)
    options = {"limit": limit, "offset": offset, "attributesToRetrieve": ["id", "brand", "number", "description", "gtin", "suppliers", "supplier_count", "lowest_cost_cents"]}
    # a brand reaches the filter string only as letters and digits, so it cannot carry filter syntax
    wanted = {canonical_brand(brand), query["brand"]} - {""}
    if len(wanted) > 1:
        return {"count": 0, "results": []}  # two different brands asked for at once
    if wanted:
        options["filter"] = f'brand = "{wanted.pop()}"'
    if query["kind"] in ("number", "gtin"):
        options["attributesToSearchOn"] = ["number"] if query["kind"] == "number" else ["gtin"]
    try:
        result = client().index(live_uid()).search(query["value"], options)
    except meilisearch.errors.MeilisearchError as exc:
        raise SearchUnavailable(str(exc)) from exc
    return {"count": result.get("estimatedTotalHits", len(result["hits"])), "results": result["hits"]}
