"""Check test_execute_tpcds in-container for patches that change only sqlglot/executor.

test_execute_tpcds parses each TPC-DS query (parser: unchanged by such a patch), runs it through
the executor in a pool worker, and compares the result table with DuckDB's answer for the same
SQL (transpiled by sqlglot: also unchanged). It passes at the pinned commit. So if the patched
executor returns, for every query the test executes, a result table identical to the pinned
one, the comparison sees the same tables and the test passes as it does at the pinned commit.

Pinned digests, CPU times and per-query executor line coverage come from tpc_build.py.
Narrowing: when every hunk lies inside a function body and the module structure is otherwise
identical, only queries that executed a changed function need rerunning (coverage was recorded
per query from cold caches). A changed function that runs at import time disables narrowing.

Every doubt keeps the test: unparsable patch, structure change we cannot place, a result that
differs, an exception, a query using more than its CPU allowance, a missing pin file.
"""
import ast
import hashlib
import json
import os
import pickle
import resource
import select
import signal
import sys
import time

BASE = os.environ.get("BR_SQLGLOT", "/opt/sqlglot")
HERE = os.path.dirname(os.path.abspath(__file__))
PIN = os.environ.get("BR_TPC_PIN", os.path.join(HERE, "tpc_pinned.pkl"))
TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
EXEC_PREFIX = "sqlglot/executor/"

_pin = None


def pin():
    global _pin
    if _pin is None:
        with open(PIN, "rb") as f:
            _pin = pickle.load(f)
    return _pin


def available():
    return os.path.exists(PIN)


def executor_only(sources):
    return bool(sources) and all(p.startswith(EXEC_PREFIX) and p.endswith(".py") for p in sources)


def _changed_old_lines(patch_text):
    """{path: set(old line numbers touched)}; a pure insertion marks the old lines around it."""
    out, cur = {}, None
    old = new = 0
    import re
    for line in patch_text.split("\n"):
        if line.startswith("+++ "):
            p = line[4:].strip().split("\t")[0]
            cur = p[2:] if p.startswith("b/") else p
            out.setdefault(cur, set())
            continue
        if line.startswith("--- ") or line.startswith("diff --git"):
            continue
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m:
            old, new = int(m.group(1)), int(m.group(2))
            continue
        if cur is None:
            continue
        if line.startswith("-"):
            out[cur].add(old)
            old += 1
        elif line.startswith("+"):
            out[cur].update((old - 1, old))  # insertion between old-1 and old
        elif line.startswith(" ") or line == "":
            old += 1
            new += 1
    return out


def _skeleton(src):
    """The module with every function body emptied: equal skeletons = body-only edits."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.body = [ast.Pass()]
    return ast.dump(tree, include_attributes=False)


def _function_spans(src):
    """[(start, end, body_start)] for every function, innermost last when nested."""
    spans = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spans.append((node.lineno, node.end_lineno, node.body[0].lineno))
    return spans


def affected_queries(patch_text, sources):
    """-> (sorted query indices to rerun, why). Falls back to every executed query."""
    queries = pin()["queries"]
    everything = sorted(queries)
    changed = _changed_old_lines(patch_text)
    touched = {}  # executor-relative path -> set of (start, end) function spans
    for path, new_src in sources.items():
        try:
            with open(os.path.join(BASE, path), encoding="utf-8") as f:
                old_src = f.read()
            if _skeleton(old_src) != _skeleton(new_src):
                return everything, "structure changed"
        except (OSError, SyntaxError, ValueError):
            return everything, "cannot parse"
        spans = _function_spans(old_src)
        rel = path[len(EXEC_PREFIX):]
        touched[rel] = set()
        for ln in changed.get(path, ()):
            inner = [s for s in spans if s[2] <= ln <= s[1]]
            if not inner:
                return everything, f"line {ln} outside a function body"
            s = max(inner, key=lambda s: s[0])  # innermost
            touched[rel].add(s)  # (def line, end line, first body line)
        if not touched[rel]:
            return everything, "no changed lines found"

    # Queries share worker processes in the real test, so an edit that may write module state
    # (a module-level variable, `global`, setattr, ...) could reach queries that never call the
    # changed function: no narrowing then.
    import re
    added = [l[1:] for l in patch_text.split("\n") if l.startswith("+") and not l.startswith("+++")]
    if any(re.search(r"\bglobal\b|\bnonlocal\b|setattr\(|__dict__|sys\.modules|globals\(|builtins", l) for l in added):
        return everything, "edit may write shared state"
    for path in sources:
        try:
            with open(os.path.join(BASE, path), encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except (OSError, SyntaxError, ValueError):
            return everything, "cannot parse"
        names = set()
        for node in tree.body:
            targets = node.targets if isinstance(node, ast.Assign) else \
                [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else []
            for t in targets:
                names.update(n.id for n in ast.walk(t) if isinstance(n, ast.Name))
        if names and any(re.search(r"\b(%s)\b" % "|".join(map(re.escape, names)), l) for l in added):
            return everything, "edit mentions a module-level variable"

    def hits(lines, body_only=False):
        for rel, ln in lines:
            for a, b, body in touched.get(rel, ()):
                if (body if body_only else a) <= ln <= b:
                    return True
        return False

    # at import a `def` line only creates the function; its body running there is what counts
    if hits(pin()["import_lines"], body_only=True):
        return everything, "changed function runs at import"
    return sorted(i for i, q in queries.items() if hits(q["lines"])), "narrowed"


def estimate_cpu(indices):
    q = pin()["queries"]
    return 0.4 + sum(q[i]["cpu"] for i in indices)


class _TooSlow(BaseException):
    pass


def _child(sources, indices, wfd):
    res = {"error": "no result"}
    try:
        import check as C
        total = estimate_cpu(indices)
        resource.setrlimit(resource.RLIMIT_CPU, (int(3 * total) + 3, int(3 * total) + 4))
        os.environ["SKIP_INTEGRATION"] = "0"
        os.chdir(BASE)
        if BASE not in sys.path:
            sys.path.insert(0, BASE)
        sys.meta_path.insert(0, C._PatchedFinder(sources, BASE))
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        import unittest  # noqa: F401
        import tests.test_executor as te  # imports the patched executor
        from sqlglot import find_tables, parse_one
        from sqlglot.executor.table import Table
        P = pin()
        q = P["queries"]
        need = sorted({t for i in indices for t in q[i]["tables"]})
        tables = {name: Table(columns=P["raw"][name][0], rows=P["raw"][name][1]) for name in need}
        if "sqlglot/executor/table.py" in sources:  # the pool pickles the tables to its workers
            tables = pickle.loads(pickle.dumps(tables))
        te.initializer(P["schema"], tables)

        def on_prof(signum, frame):
            raise _TooSlow()
        signal.signal(signal.SIGPROF, on_prof)
        for i in indices:
            args, sql = P["sqls"][i]
            expr = pickle.loads(pickle.dumps(parse_one(sql)))
            if not {t.name for t in find_tables(expr)} <= set(tables):
                res = {"error": f"q{i} needs another table"}
                raise _TooSlow()
            cap = 3 * q[i]["cpu"] + 0.5
            signal.setitimer(signal.ITIMER_PROF, cap)
            try:
                table = te.mp_execute(expr, args)
            finally:
                signal.setitimer(signal.ITIMER_PROF, 0)
            table = pickle.loads(pickle.dumps(table))
            d = None if table is None else hashlib.blake2b(
                repr((tuple(table.columns), table.rows)).encode("utf-8", "surrogatepass"),
                digest_size=16).hexdigest()
            if d != q[i]["digest"]:
                res = {"fail": f"q{i} result differs"}
                break
        else:
            res = {"pass": True}
    except _TooSlow:
        if res.get("error") == "no result":
            res = {"error": "query over its CPU allowance"}
    except BaseException as e:  # noqa: BLE001  exceptions in the executor fail the test
        res = {"fail": "exception " + repr(e)[:80]}
    finally:
        try:
            os.write(wfd, json.dumps(res).encode())
        except Exception:
            pass
        os._exit(0)


def run(sources, indices, wall_timeout):
    """-> ("pass" | "fail" | None, detail)"""
    sys.stdout.flush(); sys.stderr.flush()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        _child(sources, indices, w)
    os.close(w)
    buf, deadline, timed_out = b"", time.monotonic() + wall_timeout, False
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            timed_out = True
            break
        ready, _, _ = select.select([r], [], [], left)
        if not ready:
            continue
        chunk = os.read(r, 1 << 16)
        if not chunk:
            break
        buf += chunk
    os.close(r)
    if timed_out:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
    if timed_out:
        return None, "timeout"
    try:
        res = json.loads(buf.decode() or "{}")
    except ValueError:
        return None, "bad child output"
    if "fail" in res:
        return "fail", res["fail"]
    if res.get("pass") is True:
        return "pass", ""
    return None, res.get("error", "no result (cpu limit?)")
