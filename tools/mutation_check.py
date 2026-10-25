# LIFESPAN: permanent
"""Mutation control: break each load-bearing guard and prove the suite goes red.

A green suite is not evidence. Each mutation below removes or weakens ONE guard (a check, a constraint,
a retry rule, a control total) and runs the tests that are supposed to protect it. A mutant that leaves
those tests green is a guard nobody tests, and it is reported as SURVIVED.

THIS EDITS SOURCE FILES IN PLACE. Each mutation is written into the real file, the tests run, and the
original bytes are written back (also on Ctrl-C and SIGTERM). A killed process (power loss, kill -9) can
leave a weakened line behind, so run it in a COPY of the repository, never in the working tree you edit,
and check `git diff` if a run was interrupted.

The tests need the compose Postgres on port 5434 (and Meilisearch on 7701 for the search tests):
    docker compose up -d

    python tools/mutation_check.py --list                    names and the tests aimed at each guard
    python tools/mutation_check.py --check-targets           only confirm that every guard finds its text
    python tools/mutation_check.py --baseline                run the aimed tests once unmutated, all must pass
    python tools/mutation_check.py --only edi,paged          only the guards in those target groups
    python tools/mutation_check.py --match cost,header       only guards whose name holds a word

Two kinds of guard. `M(...)` replaces one piece of text by another (a hand-matched pair: it goes stale when the line is
reworded, and --check-targets then says so). `D(...)` DERIVES its edit: it names a function, a kind of change and a few
words of the line to aim at, and tools/mutate_ast.py works the change out of the code's syntax tree (a negated condition,
a swapped comparison, a deleted call). Reword the line and the aim still finds it; remove it and --check-targets says so.
mutate_ast.py is found through $MUTATE_AST, or an `Operations/tools/mutate_ast.py` above this folder, and a derived guard
without it is a usage error, never a silent pass.
    python tools/mutation_check.py                           every guard

An argument it does not know is refused with exit 2 and nothing is edited. Exit 0 when every mutant was
killed, 1 when one survived, 2 on a usage error or a guard whose text cannot be found.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import signal
import subprocess
import sys
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT_SECONDS = 180  # a mutant that hangs (an endless loop) counts as killed and says so


def venv_python() -> Path:
    rel = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    candidate = ROOT.joinpath(".venv", *rel)
    return candidate if candidate.exists() else Path(sys.executable)


PY = Path(os.environ.get("MUTATION_PYTHON", "")) if os.environ.get("MUTATION_PYTHON") else venv_python()

TARGETS = {
    "normalize": ROOT / "catalog" / "normalize.py",
    "mapping": ROOT / "catalog" / "ingest" / "mapping.py",
    "adapters": ROOT / "catalog" / "ingest" / "adapters.py",
    "runner": ROOT / "catalog" / "ingest" / "runner.py",
    "runs": ROOT / "catalog" / "runs.py",
    "paged": ROOT / "catalog" / "ingest" / "paged.py",
    "rest": ROOT / "catalog" / "ingest" / "rest.py",
    "soap": ROOT / "catalog" / "ingest" / "soap.py",
    "sftp": ROOT / "catalog" / "ingest" / "sftp.py",
    "batch": ROOT / "catalog" / "matching_batch.py",
    "normstep": ROOT / "catalog" / "normalize_step.py",
    "m0001": ROOT / "catalog" / "migrations" / "0001_initial.py",
    "m0002": ROOT / "catalog" / "migrations" / "0002_rawpart_immutable.py",
    "api": ROOT / "catalog" / "api" / "views.py",
    "docs": ROOT / "catalog" / "search" / "documents.py",
    "index": ROOT / "catalog" / "search" / "index.py",
    "fitment": ROOT / "catalog" / "fitment.py",
    "plans": ROOT / "catalog" / "plans.py",
    "x12": ROOT / "catalog" / "edi" / "x12.py",
    "orders": ROOT / "catalog" / "edi" / "orders.py",
    "snapshot": ROOT / "catalog" / "snapshot.py",
    "scrape": ROOT / "catalog" / "ingest" / "scrape.py",
    "redact": ROOT / "catalog" / "ingest" / "redact.py",
    "transport": ROOT / "catalog" / "ingest" / "transport.py",
    "pagination": ROOT / "catalog" / "api" / "pagination.py",
}


@dataclass(frozen=True)
class Mutant:
    name: str
    target: str
    old: str
    new: str
    tests: tuple[str, ...]
    count: int = 1  # how many times `old` must occur; all are replaced
    timeout: int = TIMEOUT_SECONDS  # a guard whose removal loops forever gets a short one
    function: str = ""  # a DERIVED guard: the function to mutate inside, the kind of change, and words of the line to aim at
    kind: str = ""
    anchor: str = ""


def M(name, target, old, new, *tests, count=1, timeout=TIMEOUT_SECONDS):
    return Mutant(name, target, old, new, tests, count, timeout)


def D(name, target, function, kind, anchor, *tests, timeout=TIMEOUT_SECONDS):
    """A guard whose edit is derived from the syntax tree. `kind` is the start of what mutate_ast calls the change
    ("negate the condition", "delete the call statement", "return value", "remove `not`", "Eq", "Lt" ...); `anchor` is text
    of the line it must sit on. Exactly one site of that kind on such a line inside `function` must exist."""
    return Mutant(name, target, "", "", tests, 1, timeout, function, kind, anchor)


_mutate_ast = None


def mutate_ast():
    global _mutate_ast
    if _mutate_ast is not None:
        return _mutate_ast
    candidates = [os.environ.get("MUTATE_AST", "")]
    candidates += [str(parent / "Operations" / "tools" / "mutate_ast.py") for parent in ROOT.parents]
    candidates += ["C:/Projects/Professional/Operations/tools/mutate_ast.py"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            spec = importlib.util.spec_from_file_location("mutate_ast", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _mutate_ast = module
            return module
    print("a derived guard needs mutate_ast.py: set MUTATE_AST to its path", file=sys.stderr)
    sys.exit(2)


def derive(m: Mutant, text: str) -> tuple[str | None, str]:
    """(the mutated source, or None, and why not). The site is found by function, kind and the words on its line."""
    ma = mutate_ast()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tree = ast.parse(text)
    lines = text.split(chr(10))
    names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if m.function not in names:
        return None, f"no function {m.function}"
    found = [
        (index, line, desc)
        for index, line, desc in ma.sites(tree, text, only_functions=[m.function])
        if desc.startswith(m.kind) and m.anchor in lines[line - 1]
    ]
    if len(found) != 1:
        return None, f"{len(found)} sites of kind {m.kind!r} on a line holding {m.anchor!r} in {m.function}"
    return ast.unparse(ma.apply(tree, found[0][0])), ""


NORM = "tests/test_normalize.py"
MAP = "tests/test_mapping.py"
FILEAD = "tests/test_file_adapter.py"
INGEST = "tests/test_ingest.py"
PAGED = "tests/test_paged.py"
REMOTE = "tests/test_remote_adapters.py"
MATCH = "tests/test_matching.py"
NSTEP = "tests/test_normalize_step.py"
SCHEMA = "tests/test_schema_constraints.py"
API = "tests/test_api.py"
SEARCH = "tests/test_search_index.py"
FIT = "tests/test_fitment.py"
PLANS = "tests/test_plans.py"
EDI = "tests/test_edi.py"
ORD = "tests/test_edi_orders.py"
TRUTH = "tests/test_pipeline_truth.py"
FORMS = "tests/test_search_forms.py"
DELIST = "tests/test_delisting.py"
SCRAPE = "tests/test_scrape.py"
SECRETS = "tests/test_secrets_not_stored.py"
PAGES = "tests/test_scrape_pages.py"

MUTANTS: list[Mutant] = [
    # ---- part number, brand and GTIN normalisation ------------------------------------------------
    M("normalize: no unicode fold", "normalize", 'text = _fold(raw or "")', 'text = raw or ""', NORM),
    M("normalize: no upper casing", "normalize", "    text = text.upper()\n", "    text = text\n", NORM),
    M("normalize: non ascii letters are guessed at, not refused", "normalize", 'if any(c.isalpha() and not ("A" <= c <= "Z") for c in text):', "if False:", NORM),
    M("normalize: separators are kept", "normalize", 'stripped = re.sub(r"[^A-Z0-9]", "", text)', "stripped = text", NORM),
    M("normalize: a prefix is stripped even with no digit left", "normalize", "any(c.isdigit() for c in rest)", "True", NORM),
    M("normalize: a prefix is stripped even when under 3 characters remain", "normalize", "len(rest) >= MIN_LEN", "len(rest) >= 1", NORM),
    M("normalize: brands with a real leading zero lose it", "normalize", "brand in ZERO_SIGNIFICANT or ", "False or ", NORM),
    M("normalize: zero padding stripped from short numbers too", "normalize", "len(value) < ZEROPAD_MIN_DIGITS", "len(value) < 1", NORM),
    M("normalize: zeros stripped below 4 digits", "normalize", "if len(stripped) < ZEROPAD_KEEP_DIGITS:", "if False:", NORM),
    M("normalize: prefix and zero rules run once, not to a fixed point", "normalize", "        if not changed:\n            break\n", "        break\n", NORM),
    M("normalize: no minimum length", "normalize", "if len(value) < MIN_LEN:", "if False:", NORM),
    M("normalize: no maximum length", "normalize", "if len(value) > MAX_LEN:", "if False:", NORM),
    M("normalize: gtin check digit ignored", "normalize", "or not _gtin_check_ok(digits)", "", NORM),
    M("normalize: gtin length ignored", "normalize", "len(digits) not in (8, 12, 13, 14)", "False", NORM),
    M("normalize: gtin not padded to 14", "normalize", 'return digits.zfill(14), ""', 'return digits, ""', NORM),
    M("normalize: brand aliases ignored", "normalize", "return _ALIAS_LOOKUP.get(key, key)[:80]", "return key[:80]", NORM),
    # ---- header mapping, cost, keys ------------------------------------------------------------------
    M("mapping: a decimal comma is read as thousands", "mapping", 'if not re.fullmatch(r"\\d{1,3}(,\\d{3})+(\\.\\d+)?", value):', "if False:", MAP, INGEST),
    M("mapping: a half cent is truncated", "mapping", "if cents != cents.to_integral_value():", "if False:", MAP),
    M("mapping: a negative cost is accepted", "mapping", 'if not re.fullmatch(r"\\d+(\\.\\d+)?", value):', 'if not re.fullmatch(r"-?\\d+(\\.\\d+)?", value):', MAP),
    M("mapping: no ceiling on a cost", "mapping", "if cents > 10_000_000 * 100:", "if False:", MAP),
    M("mapping: a blank part number is accepted", "mapping", '    if not part_number:\n        raise RowRejected("blank_part_number")', '    if False:\n        raise RowRejected("blank_part_number")', MAP, INGEST),
    M("mapping: a missing required column is not drift", "mapping", "missing = [f for f in REQUIRED if f not in found]", "missing = []", MAP, INGEST),
    M("mapping: two columns for one field is not drift", "mapping", "if doubled:", "if False:", MAP),
    M("mapping: header names are matched case and punctuation sensitive", "mapping", 'return re.sub(r"[^a-z0-9]", "", header.lower())', "return header", MAP),
    M("mapping: the row position is left out of the idempotency key", "mapping", r'\x1f{position}\x1f{content}', r'\x1f{content}', MAP),
    # ---- file adapter --------------------------------------------------------------------------------
    M("adapters: a latin line is decoded as utf-8 with replacement", "adapters", 'return raw.decode("cp1252", errors="replace"), True', 'return raw.decode("utf-8", errors="replace"), True', FILEAD),
    M("adapters: a split row is padded instead of rejected", "adapters", "if len(cells) != len(header):", "if False:", FILEAD),
    M("adapters: an undecodable byte does not reject the row", "adapters", 'elif any("', 'elif False and any("', FILEAD),
    M("adapters: the byte order mark is kept", "adapters", 'if data.startswith(b"\\xef\\xbb\\xbf"):', "if False:", FILEAD),
    M("adapters: the delimiter is always a comma", "adapters", "delimiter = detect_delimiter(lines[0])", 'delimiter = ","', FILEAD),
    M("adapters: a resume point is ignored", "adapters", "if position <= start_after:", "if False:", FILEAD, INGEST),
    M("adapters: xml doctype is allowed", "adapters", "forbid_dtd=True", "forbid_dtd=False", FILEAD),
    M("adapters: every nested xml element is a record", "adapters", "if depth != 1:", "if False:", FILEAD),
    M("adapters: the fingerprint ignores the content", "adapters", "digest.update(chunk)", "pass", FILEAD),
    # ---- the ingest runner -----------------------------------------------------------------------------
    M("runner: the checkpoint never advances", "runner", "FeedCheckpoint.objects.filter(pk=checkpoint.pk).update(position=advance_to, last_run=run)", "pass", INGEST),
    M("runner: a complete source is read again", "runner", "elif checkpoint.completed:", "elif False:", INGEST),
    M("runner: changed content does not restart the checkpoint", "runner", "if restart or checkpoint.fingerprint != fingerprint or (checkpoint.completed and not fingerprint):", "if False:", INGEST),
    M("runner: an unchanged row is stored again", "runner", " or latest.get((row.part_number, row.brand)) == chash", "", INGEST),
    M("runner: the unchanged check ignores the brand", "runner",
      '.order_by("part_number_raw", "brand_raw", "-id")\n            .distinct("part_number_raw", "brand_raw")',
      '.order_by("part_number_raw", "-id")\n            .distinct("part_number_raw")', INGEST),
    M("runner: a seen idempotency key is stored again", "runner", "if key in seen_keys or ", "if False or ", INGEST),
    M("runner: rows before a gap do not move the checkpoint", "runner", "flush()  # what was read before the gap is whole, so it moves the checkpoint", "pass", REMOTE),
    M("runner: the checkpoint moves past a gap", "runner", "advance = contiguous and not batch.had_gap", "advance = True", INGEST, REMOTE),
    M("runner: a gap does not make the run partial", "runner", "    if gap_seen:\n        return finish_run(run, PipelineRun.Status.PARTIAL)", "    if False:\n        return finish_run(run, PipelineRun.Status.PARTIAL)", INGEST, REMOTE),
    M("runner: counts are not saved with the batch", "runner", "PipelineRun.objects.filter(pk=run.pk).update(**counts)", "pass", INGEST),
    M("runner: a batch is not one transaction", "runner", "    with transaction.atomic():\n        seen_keys", "    if True:\n        seen_keys", INGEST),
    M("runs: a run left running by a dead process is not closed", "runs", "stale.update(", "stale.none().update(", INGEST),
    M("runs: the advisory lock is not enforced", "runs", "if not cursor.fetchone()[0]:", "if False:", INGEST),
    # ---- paged sources -----------------------------------------------------------------------------------
    M("paged: backoff does not double", "paged", "self.backoff_base * 2**attempt)", "self.backoff_base)", PAGED, REMOTE),
    M("paged: backoff has no cap", "paged", "delay = min(self.backoff_cap, self.backoff_base * 2**attempt)", "delay = self.backoff_base * 2**attempt", PAGED),
    M("paged: Retry-After is ignored", "paged", "delay = min(self.backoff_cap, max(delay, exc.retry_after))", "delay = delay", PAGED, REMOTE),
    M("paged: one retry too many", "paged", "                if attempt == self.max_retries:\n                    break\n", "                if False:\n                    break\n", PAGED),
    M("paged: no rate limit", "paged", "            if wait > 0:", "            if False:", PAGED),
    M("paged: endless failed pages are not fatal", "paged", "                if failed_in_a_row >= MAX_CONSECUTIVE_FAILED_PAGES:\n                    raise FatalError(f\"{failed_in_a_row} pages in a row failed; the endpoint looks down: {exc}\")", "                if False:\n                    raise FatalError(f\"{failed_in_a_row} pages in a row failed; the endpoint looks down: {exc}\")", PAGED),
    M("paged: a good page does not reset the failure run", "paged", "            failed_in_a_row = 0  # a page that is new, whatever fetch or repeat came before it\n", "", PAGED),
    M("paged: a capped page size is not noticed", "paged", "if short and total is not None and offset + len(items) < total:", "if False:", PAGED),
    M("paged: a resume starts at the first page", "paged", "offset = (start_after // self.page_size) * self.page_size", "offset = 0", PAGED),
    M("paged: rows before the resume point are yielded again", "paged", "                if position > start_after:", "                if True:", PAGED),
    M("rest: a 401 is treated as one bad page", "rest", "if status in (401, 403):", "if False:", REMOTE),
    M("rest: a 5xx is not retried", "rest", "if status == 429 or status >= 500:", "if status == 429:", REMOTE),
    M("rest: credentials in the url are accepted", "rest", 'if "?" in url or', "if False or", REMOTE),
    M("soap: a 401 is treated as one bad page", "soap", "if exc.status_code in (401, 403):", "if False:", REMOTE),
    M("soap: a transient fault is not retried", "soap", "if any(code.endswith(t) or code == t for t in TRANSIENT_FAULTS):", "if False:", REMOTE),
    M("sftp: the host key is not checked", "sftp", "if got != self.expected_key:", "if False:", REMOTE),
    M("sftp: a short transfer is accepted", "sftp", "if size != before.st_size or after.st_size != before.st_size or after.st_mtime != before.st_mtime:", "if False:", REMOTE),
    M("sftp: a host key mismatch is retried", "sftp", "except (HostKeyMismatch, paramiko.AuthenticationException):", "except paramiko.AuthenticationException:", REMOTE),
    M("sftp: a missing file is retried", "sftp", "            except FileNotFoundError as exc:\n                raise SftpError(f\"no such file on the server: {self.remote_path}\") from exc\n", "", REMOTE),
    M("sftp: the user name is put in the stored source", "sftp", 'self.source_ref = f"sftp://{host}:{port}{remote_path}"', 'self.source_ref = f"sftp://{username}@{host}:{port}{remote_path}"', REMOTE),
    # ---- the match rule ----------------------------------------------------------------------------------
    M("match: a row without a brand is guessed", "batch", "        if not row.brand:\n", "        if False:\n", MATCH, TRUTH),
    M("match: a GTIN disagreement is ignored", "batch", "if part is not None and row.gtin and part.gtin and row.gtin != part.gtin:", "if False:", MATCH),
    M("match: a GTIN held by another part is ignored", "batch", "        elif holders:", "        elif False:", MATCH),
    M("match: the brand is not part of the match", "batch", "part = by_brand_number.get((row.brand, row.number_norm))", "part = (by_number.get(row.number_norm) or [None])[0]", MATCH),
    M("match: a missing GTIN is not filled in", "batch", "if row.gtin and not part.gtin:", "if False:", MATCH),
    M("match: the older of two versions in a batch wins", "batch", "if row.raw_id > earlier.row.raw_id:", "if False:", MATCH),
    M("match: a changed listing does not mark the part for search", "batch", 'update(updated_at=now, search_version=F("search_version") + 1)', 'update(updated_at=now, search_version=F("search_version"))', SEARCH, count=2),
    M("match: a zero padded variant of a part is created as a second part", "batch", "        elif _zero_variants(by_brand_core, row):\n", "        elif False:\n", MATCH),
    M("match: parts already stored are not consulted for zero variants", "batch", "zero_kin = list(Part.objects.filter(number_core__in=cores)) if cores else []", "zero_kin = []", MATCH),
    M("match: a part made in this batch is not seen as a zero variant", "batch", "                by_brand_core[(part.brand, _core(part.number_norm))].append(part)\n", "                pass\n", MATCH),
    M("match: a queued listing keeps its first row", "batch", "if row.pk > review.norm_row_id:", "if False:", MATCH),
    M("match: a review can be resolved twice", "batch", "if item.status != ReviewItem.Status.PENDING:", "if False:", MATCH),
    M("match: a review accepts several choices", "batch", "if sum(bool(x) for x in (part, new_part, ignore)) != 1:", "if False:", MATCH),
    M("match: a new part can be made with no brand", "batch", "if not part_brand:", "if False:", MATCH),
    M("normalize step: a rejected row stays pending", "normstep", "match_state=NormalizedRow.MatchState.SKIPPED,", "match_state=NormalizedRow.MatchState.PENDING,", NSTEP),
    # ---- schema --------------------------------------------------------------------------------------------
    M("schema: raw rows can be changed or deleted", "m0002", "RAISE EXCEPTION 'catalog_rawpart rows are append only: % refused', TG_OP\n        USING ERRCODE = 'integrity_constraint_violation';", "RETURN COALESCE(NEW, OLD);", INGEST),
    M("schema: an idempotency key can repeat", "m0001", "('idempotency_key', models.CharField(max_length=64, unique=True))", "('idempotency_key', models.CharField(max_length=64))", INGEST),
    M("schema: a brand and number can repeat", "m0001", "fields=('brand', 'number_norm'), name='one_part_per_brand_number'", "fields=('id', 'brand', 'number_norm'), name='one_part_per_brand_number'", SCHEMA),
    M("schema: a listing can link twice", "m0001", "fields=('supplier', 'listing_key'), name='one_listing_per_supplier'", "fields=('id', 'supplier', 'listing_key'), name='one_listing_per_supplier'", SCHEMA),
    M("schema: a listing can be queued twice", "m0001", "fields=('supplier', 'listing_key'), name='one_review_per_listing'", "fields=('id', 'supplier', 'listing_key'), name='one_review_per_listing'", SCHEMA),
    M("schema: a source can have two checkpoints", "m0001", "fields=('supplier', 'source_ref'), name='one_checkpoint_per_source'", "fields=('id', 'supplier', 'source_ref'), name='one_checkpoint_per_source'", SCHEMA),
    M("schema: a fitment row can repeat", "m0001", "fields=('part', 'vehicle', 'position'), name='one_fitment_row'", "fields=('id', 'part', 'vehicle', 'position'), name='one_fitment_row'", SCHEMA, FIT),
    M("schema: a vehicle can repeat", "m0001", "fields=('year', 'make', 'model', 'submodel', 'engine'), name='one_vehicle_row'", "fields=('id', 'year', 'make', 'model', 'submodel', 'engine'), name='one_vehicle_row'", SCHEMA, FIT),
    # ---- API, search, fitment ---------------------------------------------------------------------------------
    M("api: limit is not range checked", "api", "if not low <= value <= high:", "if False:", API),
    M("api: a search with no text is accepted", "api", "        if not q:\n", "        if False:\n", API),
    M("api: a very long search is accepted", "api", "if len(q) > 100:", "if False:", API),
    M("api: fitment without a make is accepted", "api", "        if not make:\n", "        if False:\n", API),
    M("api: fitment ignores the year", "api", "qs = Fitment.objects.filter(vehicle__year=year, vehicle__make=make, vehicle__model=model)", "qs = Fitment.objects.filter(vehicle__make=make, vehicle__model=model)", API),
    M("api: a down search server is a 500", "api", "status=503", "status=500", API),
    M("api: a bad run filter is accepted", "api", "                if value not in valid:", "                if False:", API),
    M("api: a bad review status is accepted", "api", "if status not in ReviewItem.Status.values:", "if False:", API),
    M("search: a GTIN is searched as a part number", "docs", "    if gtin:\n", "    if False:\n", SEARCH),
    M("search: a brand reaches the filter unsanitised", "docs", "    wanted = canonical_brand(brand)\n", "    wanted = brand\n", SEARCH),
    M("search: a part changed during a send is marked as indexed at its new version", "index", "SET indexed_version = v.version FROM", "SET indexed_version = p.search_version FROM", SEARCH),
    M("search: the rebuild cursor is never saved", "index", "state.rebuild_after_id = ids[-1]", "pass", SEARCH, timeout=60),
    M("search: the sync runs during a rebuild", "index", 'if state.status == "building":', "if False:", SEARCH),
    M("search: typo tolerance is on", "index", '"typoTolerance": {"enabled": False},', '"typoTolerance": {"enabled": True},', SEARCH, API),
    M("fitment: vehicle text is not canonical", "fitment", 'return " ".join((text or "").split()).upper()', 'return (text or "")', FIT, API),
    M("fitment: any year is accepted", "fitment", "if not MIN_YEAR <= year <= MAX_YEAR:", "if False:", FIT),
    M("fitment: rows already loaded are counted as new", "fitment", "if k not in have]", "]", FIT),
    # ---- query plans ----------------------------------------------------------------------------------------
    M("plans: a Seq Scan on a big table is allowed", "plans", 'if kind == "Seq Scan" and _estimated_rows(relation) > BIG_TABLE_ROWS:', "if False:", PLANS),
    M("plans: a missing index is not reported", "plans", "if check.want_index and check.want_index not in indexes", "if False and check.want_index and check.want_index not in indexes", PLANS),
    M("plans: the time budget is ignored", "plans", "if ms > check.budget_ms:", "if False:", PLANS),
    # ---- X12 ------------------------------------------------------------------------------------------------------
    M("x12: a short ISA is accepted", "x12", "if len(text) < ISA_LENGTH:", "if False:", EDI),
    M("x12: repeated separators are accepted", "x12", " or len({es, ss, term}) != 3", "", EDI),
    M("x12: an unterminated last segment is accepted", "x12", r'if pieces[-1].strip("\r\n \t") != "":', "if False:", EDI),
    M("x12: a bad segment tag is accepted", "x12", 'if not re.fullmatch(r"[A-Z0-9]{2,3}", tag):', "if False:", EDI),
    M("x12: ISA fixed widths are not checked", "x12", "if len(isa.elements) != 16 or any(len(e) != w for e, w in zip(isa.elements, ISA_WIDTHS)):", "if False:", EDI),
    M("x12: any ISA version is accepted", "x12", 'if isa.el(12) != "00501":', "if False:", EDI),
    M("x12: a non numeric ISA control number is accepted", "x12", 'if not re.fullmatch(r"\\d{9}", isa.el(13)):', "if False:", EDI),
    M("x12: any usage indicator is accepted", "x12", 'if isa.el(15) not in ("T", "P"):', "if False:", EDI),
    M("x12: the IEA group count is not checked", "x12", "if iea.el(1) != str(groups):", "if False:", EDI),
    M("x12: the IEA control number is not checked", "x12", "if iea.el(2) != isa.el(13):", "if False:", EDI),
    M("x12: segments after the IEA are accepted", "x12", "if i != len(segments) - 1:", "if False:", EDI),
    M("x12: the GE set count is not checked", "x12", "if ge.el(1) != str(sets):", "if False:", EDI),
    M("x12: the GE control number is not checked", "x12", "if ge.el(2) != gs.el(6):", "if False:", EDI),
    M("x12: a set inside the wrong group is accepted", "x12", "if SET_TO_GROUP[set_id] != gs.el(1):", "if False:", EDI),
    M("x12: the SE segment count is not checked", "x12", "if se.el(1) != str(count):", "if False:", EDI),
    M("x12: the SE control number is not checked", "x12", "if se.el(2) != st.el(2):", "if False:", EDI),
    M("x12: the CTT line count is not checked", "x12", 'if whole(ctt[0], 1, "number of line items", 0) != expected:', "if False:", EDI),
    M("x12: a CTT in the middle is accepted", "x12", "if ctt[0] is not body[-1]:", "if False:", EDI),
    M("x12: duplicate line numbers are accepted", "x12", "if len({ln.line_id for ln in lines}) != len(lines):", "if False:", EDI),
    M("x12: an invoice total that does not add up is accepted", "x12", "if total != expected:", "if False:", EDI),
    M("x12: a price with half a cent is rounded", "x12", "if cents != cents.to_integral_value():", "if False:", EDI),
    M("x12: a date that does not exist is accepted", "x12", "return dt.date(int(value[:4]), int(value[4:6]), int(value[6:]))", "return dt.date(int(value[:4]), 1, 1)", EDI),
    M("x12: HL ids may skip", "x12", "if hid != str(hl_total):", "if False:", EDI),
    M("x12: an item may sit under a shipment", "x12", "if levels[parent] != expected_parent:", "if False:", EDI),
    M("x12: an HL parent may not exist", "x12", "if parent not in levels:", "if False:", EDI),
    M("x12: a quantity outside its range is accepted", "x12", "if not minimum <= number <= maximum:", "if False:", EDI),
    M("x12: a code outside its table is accepted", "x12", "if value not in allowed:", "if False:", EDI),
    M("x12: an element outside its length is accepted", "x12", "if not lo <= len(value) <= hi:", "if False:", EDI),
    M("x12: an accepted line may carry quantity zero", "x12", 'if status in ("IA", "IB", "IQ") and not qty:', "if False:", EDI),
    M("x12: a PO1 with no ACK at the end is accepted", "x12", "    if acks_for_current == 0:\n", "    if False:\n", EDI),
    M("x12: a bad shipment time is accepted", "x12", 'if not re.fullmatch(r"\\d{4}(\\d{2})?", need(bsn, 4, "shipment time")):', "if False:", EDI),
    M("x12: an invoice total with a decimal point is accepted", "x12", 'if not re.fullmatch(r"\\d{1,12}", value):', "if False:", EDI),
    M("x12: a second TDS is accepted", "x12", "            if total is not None:\n                raise", "            if False:\n                raise", EDI),
    M("x12: non basic characters in an element are accepted", "x12", 'if not re.fullmatch(r"[ -~]+", value):', "if False:", EDI),
    # ---- order reconciliation -------------------------------------------------------------------------------------
    M("orders: an overshipped line is not reported", "orders", "if qty > by_line[line_id].qty:", "if False:", ORD),
    M("orders: a price change on the invoice is not reported", "orders", "if inv.unit_price_cents != line.unit_price_cents and line.line_id not in priced:", "if False:", ORD),
    M("orders: a line with no cost can be ordered", "orders", "if source.cost_cents is None:", "if False:", ORD),
    M("orders: a rejected line is not reported", "orders", 'if a.status == "IR":', "if False:", ORD),
    # ---- search legs (documents.py, index.py), both backends ---------------------------------------------------
    M("forms: nothing but punctuation is not recognised as empty", "docs", "if not _WORD.search(q):", "if False:", SEARCH),
    M("forms: a number is not tried as typed", "docs", '    add("", typed)\n', "", FORMS),
    M("forms: a brand's form matches every brand", "docs", 'q = Q(brand=leg["brand"]) if leg["brand"] else Q()', "q = Q()", FORMS),
    M("forms: a leg for another brand than the parameter is kept", "docs", "        if wanted and leg_brand and leg_brand != wanted:\n            return\n", "        if False:\n            return\n", FORMS),
    M("forms: a text word may be any part of a word", "docs", 'pattern = rf"(^|[^[:alnum:]]){re.escape(word)}"', 'pattern = re.escape(word)', FORMS),
    M("forms: a GTIN is not looked up as a GTIN", "docs", '        add("gtin", query["value"], "")\n', "", FORMS, API),
    M("forms: no brand's own rules are tried", "docs", "    for known in _known_brands():\n        add(known, normalize_part_number(text, known).value)\n", "", FORMS),
    M("forms: Meilisearch drops words from the end of a text query", "index", '"matchingStrategy": "all",', '"matchingStrategy": "last",', FORMS),
    M("forms: Meilisearch searches the number for a text query", "index", '"text": ["brand", "description"]', '"text": ["number", "brand", "description"]', FORMS),
    # ---- delisting: models, runner, snapshot, consumers ---------------------------------------------------------------
    M("snapshot: a supplier that is not a snapshot is applied", "snapshot", "    if not supplier.full_snapshot:\n        return {}\n", "    if False:\n        return {}\n", DELIST),
    M("snapshot: nothing is relisted", "snapshot", "back = [(pk, part_id) for pk, key, part_id in gone if key in keys]", "back = []", DELIST),
    M("snapshot: a missing listing is not delisted", "snapshot", "missing = [(pk, part_id) for pk, key, part_id in active if key not in keys]", "missing = []", DELIST),
    M("snapshot: a pass with an unreadable row still delists", "snapshot", "        if missing and unreadable:\n", "        if False:\n", DELIST),
    M("snapshot: an empty pass delists everything", "snapshot", "        elif missing and not keys:\n", "        elif False:\n", DELIST),
    M("snapshot: a loss of more than half is not refused", "snapshot", "and len(missing) > MASS_DELIST_FRACTION * len(active)", "and False", DELIST),
    M("snapshot: exactly half is refused too", "snapshot", "and len(missing) > MASS_DELIST_FRACTION * len(active)", "and len(missing) >= MASS_DELIST_FRACTION * len(active)", DELIST),
    M("snapshot: the operator flag does not lift the refusal", "snapshot", "            and not allow_mass_delist\n", "", DELIST),
    M("snapshot: a delisting does not reach the search index", "snapshot", 'update(updated_at=now, search_version=F("search_version") + 1)', "update(updated_at=now)", DELIST),
    M("snapshot: the oldest pass is the snapshot", "snapshot", '.order_by("-completed_at", "-id")', '.order_by("completed_at", "id")', DELIST),
    M("runner: a snapshot pass keeps nothing it read", "runner", "if supplier.full_snapshot and seen:", "if False:", DELIST),
    M("runner: an unreadable row is not marked", "runner", '            if reason in UNREADABLE:\n                seen.add(("", ""))', '            if False:\n                seen.add(("", ""))', DELIST),
    M("runner: a row rejected for its cost is not counted as listed", "runner", "            elif (identity := row_identity(rec.fields, mapping)) is not None:", "            elif False:", DELIST),
    D("runner: a new pass keeps what the last pass read", "runner", "_ingest_locked", "delete the call statement", "staging=True).delete()", DELIST),
    M("runner: a NUL in a row fails the whole batch", "runner", "if not reason and any(NUL in value for value in rec.fields.values()):", "if False:", INGEST),
    M("docs: a delisted listing still sets the lowest cost", "docs", 'lowest_cost=Min("sources__cost_cents", filter=Q(sources__delisted_at__isnull=True)),', 'lowest_cost=Min("sources__cost_cents"),', DELIST),
    M("docs: a delisted listing still counts as a supplier", "docs", "live = Q(sources__isnull=False, sources__delisted_at__isnull=True)", "live = Q(sources__isnull=False)", DELIST),
    M("api: a delisted listing is shown as a source", "api", "queryset=PartSource.objects.filter(delisted_at__isnull=True)", "queryset=PartSource.objects.all()", DELIST),
    M("orders: a delisted listing can be ordered", "orders", 'source = listings.filter(delisted_at__isnull=True).order_by("-last_seen").first()', 'source = listings.order_by("-last_seen").first()', DELIST),
    M("pagination: a list may be asked for every row", "pagination", "max_limit = MAX_LIMIT", "max_limit = None", API),
    # ---- paged: a repeated page, no total ----------------------------------------------------------------------------
    M("paged: a repeated page is read as a new one", "paged", "if identity and identity in seen_pages:", "if False:", PAGED, REMOTE),
    M("paged: a repeat with no total does not stop the run", "paged", "                if total is None:\n                    raise FatalError(f\"{message};", "                if False:\n                    raise FatalError(f\"{message};", PAGED),
    M("paged: pages repeated for good do not stop the run", "paged", 'raise FatalError(f"{failed_in_a_row} pages in a row failed; the endpoint looks down: {message}")', "pass", PAGED),
    M("paged: a short page with no total ends the run", "paged", "if not items or (short and total is not None):", "if not items or short:", PAGED, REMOTE),
    M("paged: with no total the next page starts a page on", "paged", "offset += self.page_size if total is not None else len(items)", "offset += self.page_size", PAGED, REMOTE),
    M("paged: a one item page can be a repeat", "paged", "MIN_ITEMS_TO_CALL_A_REPEAT = 2", "MIN_ITEMS_TO_CALL_A_REPEAT = 1", PAGED),
    # ---- feed shapes: namespaces, attributes, UTF-16, doubled optional columns ----------------------------------------------------
    M("adapters: a namespace stays in a field name", "adapters", 'return name.rsplit("}", 1)[-1]', "return name", FILEAD),
    M("adapters: a record's own attributes are ignored", "adapters", "fields: dict[str, str] = {local_name(a): v for a, v in element.attrib.items()}", "fields: dict[str, str] = {}", FILEAD),
    M("adapters: a byte order mark does not say UTF-16", "adapters", 'if data.startswith((b"\\xff\\xfe", b"\\xfe\\xff")):', "if False:", FILEAD),
    M("adapters: UTF-16 without a mark is not found by its NULs", "adapters", "if odd >= 0.8 * pairs and even <= 0.1 * pairs:", "if False:", FILEAD),
    M("adapters: a stray NUL is taken for UTF-16", "adapters", "0.8 * pairs", "0.0 * pairs", FILEAD, count=2),
    M("mapping: two columns for a required field are accepted", "mapping", "if len(h) > 1 and f in REQUIRED}", "if len(h) > 1 and False}", MAP, INGEST),
    M("mapping: the first non empty GTIN beats the first valid one", "mapping", "            if normalize_gtin(value)[0]:", "            if False:", MAP, INGEST),
    M("mapping: doubled columns are read in alphabetical order", "mapping", "else list(dict.fromkeys(headers))", "else sorted(headers)", MAP),
    # ---- X12: the writer, the 856 hierarchy, the terminator, allocation ----------------------------------------------------------------
    M("x12: the writer does not read its document back", "x12", "    parse(text)\n    return text", "    return text", EDI, ORD),
    M("x12: accents are not dropped from a ship to name", "x12", 'decomposed = unicodedata.normalize("NFKD", value)', "decomposed = value", EDI, ORD),
    M("x12: a newline is not accepted as a terminator", "x12", 'or es in " \\r\\n" or term == " ":', 'or es in " \\r\\n" or term in " \\r\\n":', EDI),
    M("x12: a space can end a segment", "x12", ' or term == " ":', ":", EDI),
    M("x12: an 856 item goes to the last order", "x12", "item_order = order_slot[parent]", "item_order = len(orders) - 1", EDI),
    M("x12: an 856 item under an order with no PRF is accepted", "x12", "                if parent not in order_slot:\n", "                if False:\n", EDI),
    M("x12: an order level with no PRF passes", "x12", 'if any(level == "O" and hid not in order_slot for hid, level in levels.items()):', "if False:", EDI),
    M("x12: a second PRF under one order is accepted", "x12", "            if list(levels)[-1] in order_slot:\n", "            if False:\n", EDI),
    M("orders: a part on two lines is shipped on the first only", "orders", "give = min(left, max(line.qty - taken.get(line.line_id, 0), 0))", "give = left", ORD),
    M("orders: what no line has room for vanishes", "orders", "    if left:\n        out.append((lines[-1], left))", "    if False:\n        out.append((lines[-1], left))", ORD),
    # ---- the scraping adapter -----------------------------------------------------------------------------------------------------------
    M("scrape: the login form needs no CSRF field", "scrape", "if self.cfg.csrf_field and not hidden.get(self.cfg.csrf_field):", "if False:", SCRAPE),
    M("scrape: the hidden fields are not posted", "scrape", "data = {**hidden, self.cfg.username_field: self._username, self.cfg.password_field: self._password}", "data = {self.cfg.username_field: self._username, self.cfg.password_field: self._password}", SCRAPE),
    M("scrape: a refused login is taken for a success", "scrape", "        if refused:\n            raise _Stop(\"E_LOGIN_FAILED\"", "        if False:\n            raise _Stop(\"E_LOGIN_FAILED\"", SCRAPE),
    M("scrape: the session cookie is not looked for", "scrape", "if not refused and self.cfg.session_cookie and not any(", "if False and not any(", SCRAPE),
    M("scrape: a login that sends us back to the form is a success", "scrape", 'refused = self._is_login(urljoin(action, posted.headers.get("Location", "")))', "refused = False", SCRAPE),
    M("scrape: any number of re-logins", "scrape", "if self._relogins >= 1:", "if self._relogins >= 99:", SCRAPE),
    M("scrape: the first login may repeat for ever", "scrape", "            if self._initial_login_done:\n", "            if False:\n", SCRAPE, timeout=60),
    M("scrape: an expired page is read as data", "scrape", '        if soup.select_one("input[type=password]") is not None:\n            raise _SessionExpired()', '        if False:\n            raise _SessionExpired()', SCRAPE),
    M("scrape: a 401 is only an error", "scrape", "if response.status_code == 401 and not login_page:", "if False:", SCRAPE),
    M("scrape: the login page is not checked for a challenge", "scrape", "        self._refuse_a_challenge(soup)\n        password_input =", "        password_input =", SCRAPE),
    M("scrape: the answer to the login post is not checked for a challenge", "scrape", "            self._refuse_a_challenge(after)\n", "", SCRAPE),
    M("scrape: robots.txt is not obeyed", "scrape", "if self._robots is not None and not self._robots.can_fetch(self.cfg.user_agent, url):", "if False:", SCRAPE),
    M("scrape: a robots.txt that fails is read as allowed", "scrape", '            if stop.code == "E_SERVER_ERROR":', "            if False:", SCRAPE),
    M("scrape: a 403 is not a block", "scrape", "                if status == 403:\n", "                if False:\n", SCRAPE),
    M("scrape: a Retry-After above the cap is waited", "scrape", "if asked is not None and asked > cfg.backoff_cap:", "if False:", SCRAPE),
    M("scrape: Retry-After is ignored", "scrape", "delay = max(delay, asked or 0.0)", "delay = delay", SCRAPE),
    M("scrape: no pause between requests", "scrape", "if pause and self._last_request is not None:", "if False:", SCRAPE),
    M("scrape: the crawl delay is ignored", "scrape", "pause = max(self.cfg.delay_seconds, self._crawl_delay)", "pause = self.cfg.delay_seconds", SCRAPE),
    M("scrape: a challenge page is parsed as data", "scrape", "if marker.lower() in lowered:", "if False:", SCRAPE),
    M("scrape: the product table is scanned for challenge markers", "scrape", "outside = str(soup).replace(str(table), \"\", 1) if table is not None else str(soup)", "outside = str(soup)", SCRAPE),
    M("scrape: a page with no table is read", "scrape", '        if table is None:\n            raise _Stop("E_UNEXPECTED_PAGE"', '        if False:\n            raise _Stop("E_UNEXPECTED_PAGE"', SCRAPE),
    M("scrape: a redirect to another site is followed", "scrape", '                if _origin(target) != _origin(self.cfg.start_url):\n                    raise _Stop("E_OFFSITE", "the site redirected', '                if False:\n                    raise _Stop("E_OFFSITE", "the site redirected', SCRAPE),
    M("scrape: a next link to another site is followed", "scrape", "            if url and _origin(url) != _origin(self.cfg.start_url):", "            if False:", SCRAPE),
    M("scrape: a next link that loops is followed for ever", "scrape", "            if url in seen:\n", "            if False:\n", SCRAPE, timeout=60),
    D("scrape: there is no page limit", "scrape", "_walk", "negate the condition", "if pages >= self.cfg.max_pages", SCRAPE),
    M("scrape: a dead proxy is asked again every time", "scrape", "                self._dead.add(index)  # this proxy refused or dropped the connection: the next one carries the request\n", "", SCRAPE),
    M("scrape: a proxy that fails ends the request", "scrape", '                self._stats["proxy_failovers"] += 1\n                continue\n', '                raise _Transient("E_PROXY_UNAVAILABLE", "x")\n', SCRAPE),
    M("scrape: when every proxy fails the request goes direct", "scrape", '        raise _Transient("E_PROXY_UNAVAILABLE", "every proxy refused the connection")', "        return self.session.request(method, url, **kwargs)", SCRAPE),
    M("scrape: the session is not saved", "scrape", "            self._jar.save(ignore_discard=True, ignore_expires=True)", "            pass", SCRAPE),
    M("scrape: the saved session is not loaded", "scrape", "            self._jar.load(ignore_discard=True, ignore_expires=False)", "            pass", SCRAPE),
    M("scrape: the user agent is the library's", "scrape", '        self.session.headers["User-Agent"] = config.user_agent', "        pass", SCRAPE),
    M("scrape: a proxy with a password is allowed in the config", "scrape", '            if "@" in urlsplit(url).netloc:', "            if False:", SCRAPE),
    M("scrape: a missing credential variable is accepted", "scrape", "                if not env.get(name):\n                    raise ScrapeConfigError(", "                if False:\n                    raise ScrapeConfigError(", SCRAPE),
    # ---- a secret is stored nowhere (derived guards: the edit comes from mutate_ast, not from a text pair) ---------------------------------
    D("redact: a secret found in a message is left in it", "redact", "text", "negate the condition", "if secret in value", SECRETS),
    D("redact: a log record keeps the secret it carries", "redact", "factory", "negate the condition", "clean != message", SECRETS),
    D("redact: the identity of a source shows a secret query value", "redact", "identity_url", "negate the condition", "secret_name(unquote_plus(name))", SECRETS),
    D("scrape: the credentials of a proxy are not among the secrets", "scrape", "__init__", "delete the call statement", "userinfo_secrets", SECRETS),
    D("scrape: a proxy port above 65535 is accepted", "scrape", "proxy_problem", "LtE -> Gt (operator 2", "65535", SECRETS),
    D("scrape: a proxy that is not a URL is tried instead of refused", "scrape", "records", "negate the condition", "if self._proxy_problem", SECRETS),
    # ---- the walk ends as a success only when the site said it ended ----------------------------------------------------------------------
    D("scrape: a page cut inside its table is read", "scrape", "_walk", "negate the condition", "if not page.table_closed", PAGES),
    D("scrape: an empty page is the end of the data", "scrape", "_walk", "negate the condition", "if not rows and not self.cfg.allow_empty", PAGES),
    D("scrape: an empty first page is only a gap", "scrape", "_walk", "Eq", "first = pages == 1", PAGES),
    D("scrape: a page that never closes its html ends the walk", "scrape", "_walk", "negate the condition", "if not page.html_closed", PAGES),
    D("scrape: a body shorter than its announced length is read", "scrape", "_read_page", "negate the condition", "announced.isdigit()", PAGES),
    D("scrape: an unclosed table is counted as closed", "scrape", "_scan", "LtE", "open_tables <= 0", PAGES),
    D("scrape: the closing of a table is not noticed", "scrape", "handle_endtag", "negate the condition", 'tag == "table"', PAGES),
    # ---- a resume never skips by position over a source whose order can change -------------------------------------------------------------
    D("runner: a source that cannot be resumed by position is resumed by position", "runner", "_ingest_locked", "remove `not`", "restart = not getattr", PAGES),
    # ---- a page is decoded as it declares itself, never as ISO-8859-1 by default ------------------------------------------------------------
    D("scrape: a byte order mark is not looked for", "scrape", "decode_page", "negate the condition", "if data.startswith(bom)", PAGES),
    D("scrape: a page that declares nothing is not read as UTF-8", "scrape", "decode_page", "delete the call statement", 'labels.append(("utf-8"', PAGES),
    D("scrape: the charset of the Content-Type is ignored", "scrape", "decode_page", "negate the condition", "if header:", PAGES),
    D("scrape: the charset of a meta tag is ignored", "scrape", "decode_page", "negate the condition", "if meta:", PAGES),
    D("scrape: a meta tag may declare UTF-16", "scrape", "_codec", "negate the condition", "if from_meta and", PAGES),
    D("scrape: a latin-1 page with an undefined windows-1252 byte is refused", "scrape", "decode_page", "negate the condition", "if codec in _LATIN_ONE:", PAGES),
    # ---- a next link that is not a page is the end of the walk ------------------------------------------------------------------------------
    D("scrape: an empty, # or javascript: next link is followed", "scrape", "_next_url", "negate the condition", "if not href", PAGES),
    D("scrape: a next link to the page itself is followed", "scrape", "_next_url", "negate the condition", 'return "" if target in itself', PAGES),
    D("scrape: a next link marked disabled is followed", "scrape", "_says_disabled", "and <-> or", 'any("disabled"', PAGES),
    # ---- the same class in the REST adapter: a JSON answer is read as bytes ------------------------------------------------------------------
    D("rest: the answer is not read as JSON bytes", "rest", "_json_body", "return value", "json.loads", REMOTE),
    # ---- second output round (derived from the syntax tree) ----------------------------------------------------------------------------------
    # F1: a row is unchanged only when it matches the NEWEST stored version; its key says "this row of THIS pass landed once"
    D("mapping: a key does not tell one pass from the next", "mapping", "idempotency_key", "negate the condition", "if pass_id:", INGEST, PAGES),
    # F2: a pass that has not finished never replaces the latest completed pass
    D("runner: a finished pass does not promote what it read", "runner", "_ingest_locked", "delete the call statement", ".update(staging=False)", DELIST),
    D("snapshot: the match step reads a pass that has not finished", "snapshot", "seen_listing_keys", "False -> True", "staging=False", DELIST),
    # F3: every error requests raises while a page is fetched or read is a coded outcome (transient: retried, then a gap)
    D("scrape: a transient error ends the run instead of being retried", "scrape", "_refused", "negate the condition", "if outcome.transient", PAGES),
    D("rest: a configuration error is retried as if it were transient", "rest", "fetch_page", "negate the condition", "if not outcome.transient", REMOTE),
    D("soap: a configuration error is retried as if it were transient", "soap", "fetch_page", "negate the condition", "if not outcome.transient", REMOTE),
    D("transport: a configuration error is not told from a transient one", "transport", "classify", "negate the condition", "isinstance(exc, CONFIG_ERRORS)", PAGES, REMOTE),
    D("transport: a body cut after the headers is not E_PAGE_TRUNCATED", "transport", "classify", "negate the condition", "rx.ChunkedEncodingError", PAGES),
    D("transport: a body that does not decode is not E_PAGE_ENCODING", "transport", "classify", "negate the condition", "rx.ContentDecodingError", PAGES),
    # F4: a link is resolved against the address the page was served from, or its base element
    D("scrape: a redirect's landing address is not told from the one asked for", "scrape", "_walk", "negate the condition", "if served != url", PAGES),
    D("scrape: a page read under its second address is read again", "scrape", "_walk", "delete the call statement", "seen.add(served)", PAGES),
    D("scrape: a next link back to the page served is followed", "scrape", "_next_url", "In -> NotIn", 'return "" if target in itself', PAGES),
    D("scrape: a base element is not the base of a link", "scrape", "_next_url", "negate the condition", "if base_tag is not None", PAGES),
]


# ------------------------------------------------------------------------------------------------------------
_originals: dict[Path, bytes] = {}


def restore_all(*_args) -> None:
    for path, data in _originals.items():
        path.write_bytes(data)
    _originals.clear()


def install_handlers() -> None:
    def handler(signum, frame):  # noqa: ARG001
        restore_all()
        sys.exit(130)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def run_tests(tests: tuple[str, ...], timeout: int = TIMEOUT_SECONDS) -> tuple[int, str]:
    cmd = [str(PY), "-m", "pytest", *tests, "-x", "-q", "-p", "no:cacheprovider"]
    try:
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "hang: no result inside the time limit"
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-1:] or [""]
    return proc.returncode, tail[0]


def select(args) -> list[Mutant]:
    chosen = MUTANTS
    if args.only:
        keys = set(args.only.split(","))
        unknown = keys - set(TARGETS)
        if unknown:
            print(f"unknown target group(s): {', '.join(sorted(unknown))}; known: {', '.join(TARGETS)}", file=sys.stderr)
            sys.exit(2)
        chosen = [m for m in chosen if m.target in keys]
    if args.match:
        words = [w.lower() for w in args.match.split(",")]
        chosen = [m for m in chosen if any(w in m.name.lower() for w in words)]
    return chosen


def check_targets(mutants: list[Mutant]) -> bool:
    ok = True
    for m in mutants:
        text = TARGETS[m.target].read_text(encoding="utf-8").replace("\r\n", "\n")
        if m.function:
            mutated, why = derive(m, text)
            if mutated is None:
                ok = False
                print(f"TARGET NOT FOUND ({why}): {m.name}  [{m.target}]", file=sys.stderr)
            continue
        found = text.count(m.old)
        if found != m.count:
            ok = False
            print(f"TARGET NOT FOUND ({found} of {m.count}): {m.name}  [{m.target}]  {m.old[:70]!r}", file=sys.stderr)
        if m.old == m.new:
            ok = False
            print(f"NO-OP MUTATION: {m.name}", file=sys.stderr)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--check-targets", action="store_true")
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--only")
    parser.add_argument("--match")
    args = parser.parse_args()
    mutants = select(args)
    if not mutants:
        print("no guard matches", file=sys.stderr)
        return 2
    if args.list:
        for m in mutants:
            print(f"{m.target:10} {m.name}  <- {', '.join(m.tests)}")
        print(f"{len(mutants)} guards")
        return 0
    if not check_targets(mutants):
        return 2
    if args.check_targets:
        print(f"all {len(mutants)} guards found their text")
        return 0
    if args.baseline:
        suites = sorted({t for m in mutants for t in m.tests})
        rc, tail = run_tests(tuple(suites))
        print(f"baseline over {len(suites)} test files: {'PASS' if rc == 0 else 'FAIL'}: {tail}")
        return 0 if rc == 0 else 1

    install_handlers()
    killed, survived = 0, []
    started = time.time()
    for n, m in enumerate(mutants, start=1):
        path = TARGETS[m.target]
        original = path.read_bytes()
        _originals[path] = original
        try:
            # a file with CRLF endings is matched as LF text, as check_targets reads it; otherwise a multi line
            # guard would never be edited and its mutant would be reported as a survivor
            as_lf = original.decode("utf-8").replace("\r\n", "\n")
            if m.function:
                mutated, why = derive(m, as_lf)
                if mutated is None:
                    raise SystemExit(f"the derived mutation for {m.name!r} found no site: {why}")
            else:
                mutated = as_lf.replace(m.old, m.new)
            if mutated == as_lf:
                raise SystemExit(f"the mutation for {m.name!r} changed nothing; refusing to report it as a result")
            path.write_text(mutated, encoding="utf-8", newline="")
            rc, tail = run_tests(m.tests, m.timeout)
        finally:
            path.write_bytes(original)
            _originals.pop(path, None)
        verdict = "killed" if rc != 0 else "SURVIVED"
        if rc != 0:
            killed += 1
        else:
            survived.append(m)
        print(f"[{n}/{len(mutants)}] {verdict:8} {m.name}  ({tail[:70]})", flush=True)
    print(f"\nkilled {killed} of {len(mutants)} in {time.time() - started:.0f} s")
    for m in survived:
        print(f"  SURVIVED: {m.name}  [{m.target}]  tests: {', '.join(m.tests)}")
    return 0 if not survived else 1


if __name__ == "__main__":
    sys.exit(main())
