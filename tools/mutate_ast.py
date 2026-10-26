# LIFESPAN: permanent
"""Derived mutation testing: the mutants come from the CODE'S OWN SYNTAX TREE, never from hand-written `old text -> new text` pairs (2026-10-12).

Why: a hand-written mutant table (`src.replace("MAX = 6 ", "MAX = 3 ", 1)`) is tied to exact source text. The first time someone rewords a comment the pattern matches twice or not at all, the table
goes stale, and a stale entry either fails the whole suite for a reason unrelated to the code (it did, the same hour it was written) or silently stops testing anything. This tool derives its
mutants from the AST, so a refactor, a renamed constant or a new function changes the mutant set instead of breaking it, and a NEW branch is mutated without anyone remembering to add a row.

What it mutates (one change per mutant): comparison operators (== != < <= > >= is is-not in not-in, EACH operator of a chained comparison such as `0 <= x < 10`), `and`/`or`, a removed `not`, a negated
`if`/`while`/`assert`/ternary condition, True/False, an integer constant (+1, and 0<->1, including an index such as `args[1]`), a non-empty string constant (emptied; skipped when it is a docstring, an f-string
part, a dict or subscript KEY or a regex flag), `return <value>` -> `return None`, a deleted call statement (replaced by `pass`), and the arithmetic and bit operators `+ - * / // % ** << >> & | ^` (swapped in
pairs). Docstrings and `# no-mutate` lines are left alone. (Round 5, 2026-10-13: chained comparisons, `%`, `/`, `**`, shifts, bit operators and integer subscripts were never mutated, and a form feed inside a string
shifted which lines were skipped.) A run that SAMPLES the sites (the default --max 40) says so: its score is an estimate, `--max 0` runs them all.

How a mutant is judged: the target's directory (its *.py files and its tests/ folder) is COPIED to a temp folder, ONE mutant is written into the copy, and the test command runs there. The original
files are never touched, so a hook that is live in another session is never mutated. Exit code != 0 (or a timeout) = KILLED; exit 0 = SURVIVED = a line the tests do not actually pin.

Usage: python mutate_ast.py TARGET.py --test REL_TEST.py [--max 40] [--seed 1] [--jobs 4] [--timeout 120] [--min-score 0.0] [--list]
  --test    path of the test script RELATIVE to the target's directory (tests/test_x.py or test_x.py), run with the same python
  --max     mutants to run (a seeded random sample of all the sites; 0 = all); --list prints the sites and exits
  --min-score  exit 1 if killed/(killed+survived) is below this (default 0: report only)
"""
import argparse
import ast
import concurrent.futures as cf
import copy
import glob
import os
import random
import re
import shutil
import subprocess
import time
import sys
import tempfile
import warnings

CMP_SWAP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.LtE: ast.Gt, ast.Gt: ast.LtE, ast.GtE: ast.Lt, ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In}
ARITH_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.FloorDiv, ast.FloorDiv: ast.Mult, ast.Div: ast.Mult, ast.Mod: ast.Mult, ast.Pow: ast.Mult,
              ast.LShift: ast.RShift, ast.RShift: ast.LShift, ast.BitAnd: ast.BitOr, ast.BitOr: ast.BitAnd, ast.BitXor: ast.BitAnd}
SUB = 64                       # a site index is `walk_position * SUB + k`: k picks one operator of a chained comparison (`a < b <= c`), 0 for every other site
LINE_BREAK = re.compile(r"\r\n|\r|\n")      # the breaks the PARSER counts: str.splitlines() also breaks on a form feed or U+2028 inside a string, which shifted every later line number


def _skip_lines(src):
    """Lines carrying a `# no-mutate` COMMENT (the words inside a string or docstring do not count)."""
    return {i + 1 for i, ln in enumerate(LINE_BREAK.split(src)) if re.search(r"#[^\n]*no-mutate", ln)}


def _docstring_nodes(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant):
            out.add(id(n.body[0].value))
    return out


def _fstring_parts(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr):
            for v in ast.walk(n):
                out.add(id(v))
    return out


def _dict_keys(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Dict):
            for k in n.keys:
                if k is not None:
                    out.add(id(k))
        if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str):
            out.add(id(n.slice))                             # a STRING subscript is a key (`d["name"]` emptied is a KeyError, not a finding); an integer index (`args[1]`) is mutated
    return out


def _output_only(tree):
    """ids of the nodes inside a `print(...)` statement: what a program SAYS is not what it DOES, so deleting a print or emptying its text is reported by the tests that read the output, not counted
    here (it would drown the real survivors). Pass --include-output to mutate them too."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) and n.value.func.id == "print":
            for v in ast.walk(n):
                out.add(id(v))
    return out


def _inside_functions(tree, names):
    """ids of every node inside a function (or method) whose NAME is in `names`: lets a mutation run aim at the code a test was written for, found by name, never by line number."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names:
            for v in ast.walk(n):
                out.add(id(v))
    return out


def sites(tree, src, include_output=False, only_functions=None):
    """-> [(index, lineno, description)] in a stable walk order. The index is the position of the node in `ast.walk`, so the same tree always yields the same numbering."""
    skip = _skip_lines(src)
    doc, fparts, keys = _docstring_nodes(tree), _fstring_parts(tree), _dict_keys(tree)
    said = set() if include_output else _output_only(tree)
    only = _inside_functions(tree, set(only_functions)) if only_functions else None
    out = []
    for i, n in enumerate(ast.walk(tree)):
        ln = getattr(n, "lineno", 0)
        if ln in skip or id(n) in said or (only is not None and id(n) not in only):
            continue
        at = i * SUB
        if isinstance(n, ast.Compare):
            for k, op in list(enumerate(n.ops))[:SUB]:
                if type(op) in CMP_SWAP:
                    out.append((at + k, ln, "%s -> %s%s" % (type(op).__name__, CMP_SWAP[type(op)].__name__, "" if len(n.ops) == 1 else " (operator %d of a chain)" % (k + 1))))
        elif isinstance(n, ast.BoolOp):
            out.append((at, ln, "and <-> or"))
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            out.append((at, ln, "remove `not`"))
        elif isinstance(n, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            out.append((at, ln, "negate the condition"))
        elif isinstance(n, ast.Constant) and not isinstance(n.value, str) and isinstance(n.value, bool):
            out.append((at, ln, "%s -> %s" % (n.value, not n.value)))
        elif isinstance(n, ast.Constant) and isinstance(n.value, int) and not isinstance(n.value, bool) and id(n) not in keys:
            out.append((at, ln, "%d -> %d" % (n.value, 1 if n.value == 0 else n.value + 1)))
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value and id(n) not in doc and id(n) not in fparts and id(n) not in keys and len(n.value) > 1:
            out.append((at, ln, "string %r -> ''" % n.value[:24]))
        elif isinstance(n, ast.Return) and n.value is not None and not (isinstance(n.value, ast.Constant) and not n.value.value):
            out.append((at, ln, "return value -> None"))       # `return 0`, `return False`, `return ""` -> None is the same answer to every caller that tests truthiness or exits: not a mutant worth reading
        elif isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
            out.append((at, ln, "delete the call statement"))
        elif isinstance(n, ast.BinOp) and type(n.op) in ARITH_SWAP:
            out.append((at, ln, "%s -> %s" % (type(n.op).__name__, ARITH_SWAP[type(n.op)].__name__)))
        elif isinstance(n, ast.AugAssign) and type(n.op) in ARITH_SWAP:
            out.append((at, ln, "%s= -> %s=" % (type(n.op).__name__, ARITH_SWAP[type(n.op)].__name__)))
    return out


def apply(tree, index):
    """-> a mutated COPY of the tree, with the node at walk position `index // SUB` changed (for a chained comparison, its operator number `index % SUB`)."""
    t = copy.deepcopy(tree)
    position, sub = divmod(index, SUB)
    for i, n in enumerate(ast.walk(t)):
        if i != position:
            continue
        if isinstance(n, ast.Compare):
            ops = list(n.ops)
            ops[sub] = CMP_SWAP[type(ops[sub])]()
            n.ops = ops
        elif isinstance(n, ast.BoolOp):
            n.op = ast.Or() if isinstance(n.op, ast.And) else ast.And()
        elif isinstance(n, ast.UnaryOp):
            return _replace(t, n, n.operand)
        elif isinstance(n, (ast.If, ast.While, ast.IfExp)):
            n.test = ast.UnaryOp(op=ast.Not(), operand=n.test)
        elif isinstance(n, ast.Assert):
            n.test = ast.UnaryOp(op=ast.Not(), operand=n.test)
        elif isinstance(n, ast.Constant):
            v = n.value
            n.value = (not v) if isinstance(v, bool) else (1 if v == 0 else v + 1) if isinstance(v, int) else ""
        elif isinstance(n, ast.Return):
            n.value = ast.Constant(value=None)
        elif isinstance(n, ast.Expr):
            return _replace(t, n, ast.Pass())
        elif isinstance(n, (ast.BinOp, ast.AugAssign)):
            n.op = ARITH_SWAP[type(n.op)]()
        break
    return ast.fix_missing_locations(t)


def _replace(tree, old, new):
    for parent in ast.walk(tree):
        for field, val in ast.iter_fields(parent):
            if val is old:
                setattr(parent, field, new)
                return ast.fix_missing_locations(tree)
            if isinstance(val, list):
                for j, item in enumerate(val):
                    if item is old:
                        val[j] = new
                        return ast.fix_missing_locations(tree)
    return tree


DATA_EXT = (".py", ".json", ".txt", ".md", ".ps1", ".cjs", ".js", ".mjs", ".csv", ".jsonl", ".cfg", ".toml", ".yml", ".yaml", ".ini")
MAX_DATA_BYTES = 2_000_000


def copy_workdir(src_dir, d):
    """Copy the target's folder (its files of the usual source and data kinds up to 2 MB, plus its tests/ folder) into `d`. A file that vanishes mid-copy (another session's throwaway mutant, a temp file) is skipped,
    and so are other mutation runs' own files. NOTE: only the COPY is mutated, but the code under test still sees the REAL home and REAL absolute paths: a test that reaches outside its folder is not isolated."""
    for sub, dest in (("", d), ("tests", os.path.join(d, "tests"))):
        folder = os.path.join(src_dir, sub) if sub else src_dir
        if not os.path.isdir(folder):
            continue
        os.makedirs(dest, exist_ok=True)
        for name in os.listdir(folder):
            f = os.path.join(folder, name)
            if name.startswith(("_mut_ast_", "_mut_base_", "_mut_out_")) or not name.lower().endswith(DATA_EXT):      # this tool's OWN temp artifacts only: a test helper such as hooks/tests/_mutants.py MUST be copied (four hook tests import it)
                continue
            try:
                if os.path.isfile(f) and os.path.getsize(f) <= MAX_DATA_BYTES:
                    shutil.copy(f, dest)
            except OSError:
                continue
    if os.path.isdir(os.path.join(d, "tests")) and not os.listdir(os.path.join(d, "tests")):
        os.rmdir(os.path.join(d, "tests"))


def kill_tree(proc):
    """We started this process, so killing it and everything under it is ours to do (a grandchild holding a pipe made `subprocess.run(timeout=)` hang for 25 s on Windows)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, creationflags=0x08000000)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


class _Job:
    """A Windows job object that kills every process in it when it is closed (KILL_ON_JOB_CLOSE). A test that starts a child and then EXITS leaves the child alive: taskkill /T finds nothing once the parent is gone,
    so a deadline-only kill leaked one process per mutant. Inert (and harmless) off Windows, where the process group does this, and when the job cannot be made."""

    def __init__(self):
        self.h = None
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            class Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

            class Io(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

            class Ext(ctypes.Structure):
                _fields_ = [("Basic", Basic), ("Io", Io), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            job = k32.CreateJobObjectW(None, None)
            info = Ext()
            info.Basic.LimitFlags = 0x2000                       # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if job and k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                self.h, self._k32 = job, k32
            elif job:
                k32.CloseHandle(job)                             # a job that could not be configured is not kept (the handle used to leak)
        except Exception:                                        # noqa: BLE001
            self.h = None

    def add(self, proc):
        """True when the process is in the job. False (not created, assignment refused, any error) tells the caller to fall back to a tree kill."""
        if not self.h:
            return False
        try:
            return bool(self._k32.AssignProcessToJobObject(self.h, int(proc._handle)))        # noqa: SLF001  the handle Popen already holds
        except Exception:                                        # noqa: BLE001
            return False

    def close(self):
        if self.h:
            try:
                self._k32.CloseHandle(self.h)                    # kills everything still in the job
            except Exception:                                    # noqa: BLE001
                pass
            self.h = None


def run_capped(cmd, cwd, env, timeout):
    """-> (returncode or None on timeout, output tail). Output goes to a temp FILE, never a pipe, and the whole process tree is killed at the deadline AND when the test exits (a child it left behind is killed too)."""
    env = dict(env if env is not None else os.environ)
    env.setdefault("HOOK_TRACE_OFF", "1")                    # a mutant run never writes the production liveness log (it would show up as the hook firing)
    fd, out_path = tempfile.mkstemp(prefix="_mut_out_", suffix=".txt")
    job = _Job()
    try:
        with os.fdopen(fd, "wb") as out:
            kw = {"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": True}
            proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, **kw)
            in_job = job.add(proc)
            try:
                proc.wait(timeout=timeout)
                rc = proc.returncode
                if os.name == "nt" and not in_job:
                    kill_tree(proc)                              # no job (could not be made or assigned): the best effort left is a tree kill, which finds a child only while its parent is still alive
                if os.name != "nt":
                    try:
                        os.killpg(proc.pid, 9)                   # the group the test started: a child it left running dies with it
                    except OSError:
                        pass
            except subprocess.TimeoutExpired:
                kill_tree(proc)
                rc = None
        with open(out_path, "rb") as f:
            tail = f.read()[-1500:].decode("utf-8", "replace")
        return rc, tail
    finally:
        job.close()                                          # kills a child the test left behind before its files are removed
        for attempt in range(8):                             # the kill is asynchronous: a just-killed child can hold the output file for a moment
            try:
                os.unlink(out_path)
                break
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(0.05 * (attempt + 1))


def _rmtree(d):
    for attempt in range(8):                                 # a test's child that is still shutting down can hold a file for a moment on Windows
        shutil.rmtree(d, ignore_errors=True)
        if not os.path.exists(d):
            return
        time.sleep(0.05 * (attempt + 1))


def run_one(job):
    target, tree, idx, lineno, desc, test_rel, timeout = job
    d = tempfile.mkdtemp(prefix="_mut_ast_")
    try:
        copy_workdir(os.path.dirname(os.path.abspath(target)), d)
        mutated = ast.unparse(apply(tree, idx))
        try:
            compile(mutated, target, "exec")
        except (SyntaxError, ValueError):
            return (lineno, desc, "invalid")                 # a mutant that does not even compile is not a kill: the test never ran against it
        with open(os.path.join(d, os.path.basename(target)), "w", encoding="utf-8") as fh:
            fh.write(mutated)
        t = os.path.join(d, test_rel)
        rc, _tail = run_capped([sys.executable, t], os.path.dirname(t), dict(os.environ, MUTATE_AST_COPY=d), timeout)
        return (lineno, desc, "killed" if rc != 0 else "survived")       # None (a timeout) is not 0: a mutant that hangs the tests is caught
    finally:
        _rmtree(d)


def baseline_passes(target, test_rel, timeout, tree=None, parallel=1):
    """The test must PASS on the unmodified code, in TWO forms and `parallel` times at once. (1) The ORIGINAL source: a test that already fails 'kills' every mutant and reports a perfect score over nothing.
    (2) The source round-tripped through ast.unparse with NO mutation (comments gone, line numbers changed): a test that only greps a comment, or reads line numbers, would otherwise 'kill' every mutant for a
    reason unrelated to the mutation. (3) `parallel` copies run at the same moment, because the mutants will: a test that binds a fixed port or holds a lock passes alone and fails against itself."""
    if os.path.isabs(test_rel) or test_rel.startswith(("\\", "/")) or os.path.splitdrive(test_rel)[0]:
        return False, "--test must be RELATIVE to the target's folder (an absolute, drive-less rooted or drive-qualified path would run the ORIGINAL files, never the mutant)"
    forms = [("the original source", None)]
    if tree is not None:
        forms.append(("the source round-tripped through ast.unparse (no mutation)", ast.unparse(tree)))
    jobs = []
    for label, text in forms:
        for _ in range(max(1, parallel) if text is None else 1):
            jobs.append((label, text))

    def one(job):
        label, text = job
        d = tempfile.mkdtemp(prefix="_mut_base_")
        try:
            copy_workdir(os.path.dirname(os.path.abspath(target)), d)
            if text is not None:
                with open(os.path.join(d, os.path.basename(target)), "w", encoding="utf-8") as fh:
                    fh.write(text)
            t = os.path.join(d, test_rel)
            if not os.path.isfile(t):
                return label, False, "the test %s is not in the copied folder (only source and data files up to 2 MB beside the target, and its tests/ folder, are copied)" % test_rel
            rc, tail = run_capped([sys.executable, t], os.path.dirname(t), dict(os.environ, MUTATE_AST_COPY=d), timeout)
            return label, rc == 0, ("timed out after %d s" % timeout) if rc is None else "\n".join(tail.strip().splitlines()[-6:])
        finally:
            _rmtree(d)
    originals = [j for j in jobs if j[1] is None]
    controls = [j for j in jobs if j[1] is not None]
    for stage in (originals, controls):                      # the original copies run TOGETHER (that is how mutants will run); the unparsed control runs after, on its own
        if not stage:
            continue
        with cf.ThreadPoolExecutor(max_workers=len(stage)) as ex:
            for label, ok, tail in ex.map(one, stage):
                if not ok:
                    return False, "[%s] %s" % (label, tail)
    return True, ""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("target")
    ap.add_argument("--test", required=True)
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--min-score", type=float, default=0.0)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--include-output", action="store_true", help="also mutate print() statements and their text")
    ap.add_argument("--only-functions", default="", help="comma-separated function names: mutate only inside them (aim a fast test at the code it was written for)")
    a = ap.parse_args(argv)
    if a.max < 0 or a.jobs < 1 or a.timeout < 1:
        print("mutation: --max must be 0 (all) or more, --jobs and --timeout at least 1")
        return 2
    src = open(a.target, encoding="utf-8-sig").read()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")                      # the target may hold `"\s"` literals that warn on parse
        tree = ast.parse(src)
    only = [f.strip() for f in a.only_functions.split(",") if f.strip()]
    if only:
        names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        missing = [f for f in only if f not in names]
        if missing:
            print("mutation: --only-functions names no function in the target: %s (a misspelt name would otherwise run a smaller set and report a clean score)" % ", ".join(missing))
            return 2
    allsites = sites(tree, src, a.include_output, only or None)
    if only and not allsites:
        print("mutation: --only-functions %s matched no mutable code" % ", ".join(only))
        return 2
    if a.list:
        for _i, ln, desc in allsites:
            print("%5d  %s" % (ln, desc))
        print("%d site(s)" % len(allsites))
        return 0
    ok, tail = baseline_passes(a.target, a.test, a.timeout, tree, parallel=a.jobs)
    if not ok:
        print("mutation: the test FAILS on the UNMUTATED code, so every mutant would count as killed and the score would mean nothing. Fix the test first.\n%s" % tail)
        return 3
    chosen = allsites if not a.max or a.max >= len(allsites) else random.Random(a.seed).sample(allsites, a.max)
    if len(chosen) < len(allsites):
        print("mutation: SAMPLED %d of %d sites (seed %d): the score is an ESTIMATE; `--max 0` runs every site" % (len(chosen), len(allsites), a.seed))
    jobs = [(a.target, tree, i, ln, desc, a.test, a.timeout) for i, ln, desc in sorted(chosen, key=lambda s: s[1])]
    killed = survived = invalid = 0
    survivors = []
    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        for ln, desc, verdict in ex.map(run_one, jobs):
            if verdict == "killed":
                killed += 1
            elif verdict == "invalid":
                invalid += 1
            else:
                survived += 1
                survivors.append((ln, desc))
    for ln, desc in sorted(survivors):
        print("SURVIVED  %s:%d  %s" % (os.path.basename(a.target), ln, desc))
    total = killed + survived
    if not total:
        print("mutation: 0 mutant(s) could be judged (%d site(s), %d invalid): there is nothing to score, so no score is claimed (exit 3). Pick a target with logic, or widen --only-functions / --max."
              % (len(allsites), invalid))
        return 3
    score = killed / total
    print("mutation: %d mutant(s) run of %d site(s), %d killed, %d survived%s, score %.2f (equivalent mutants, such as a changed log string, can survive; read each)"
          % (total, len(allsites), killed, survived, (", %d invalid (did not compile, not counted)" % invalid) if invalid else "", score))
    return 1 if score < a.min_score else 0


if __name__ == "__main__":
    sys.exit(main())
