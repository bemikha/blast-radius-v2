"""Build step: pin what test_execute_tpcds needs from sqlglot's executor at the pinned commit.

For every TPC-DS query the test executes, a fresh forked child (cold caches, so memoised work
is seen by every query that needs it) runs it the way the test's pool workers do and records:
  - a digest of the result table (columns and rows, exactly as the test reads them),
  - the CPU seconds it took here,
  - which lines of sqlglot/executor/*.py it executed.
Also recorded: the executor lines that run while the test module is imported, the parsed
TPC-DS table data (independent of sqlglot), and the query list.

usage: python tpc_build.py SQLGLOT_DIR OUT.pkl
"""
import hashlib
import json
import os
import pickle
import sys
import time

REPO, OUT = sys.argv[1], sys.argv[2]
EXEC_DIR = os.path.join(os.path.abspath(REPO), "sqlglot", "executor") + os.sep


def digest(table):
    if table is None:
        return None
    return hashlib.blake2b(repr((tuple(table.columns), table.rows)).encode("utf-8", "surrogatepass"),
                           digest_size=16).hexdigest()


def tracer(lines):
    def local(frame, event, arg):
        if event == "line":
            lines.add((frame.f_code.co_filename[len(EXEC_DIR):], frame.f_lineno))
        return local

    def glob(frame, event, arg):
        if frame.f_code.co_filename.startswith(EXEC_DIR):
            lines.add((frame.f_code.co_filename[len(EXEC_DIR):], frame.f_lineno))
            return local
        return None
    return glob


def in_child(fn):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            res = fn()
            data = pickle.dumps(("ok", res))
        except BaseException as e:  # noqa: BLE001
            data = pickle.dumps(("err", repr(e)))
        with os.fdopen(w, "wb") as f:
            f.write(data)
        os._exit(0)
    os.close(w)
    with os.fdopen(r, "rb") as f:
        data = f.read()
    os.waitpid(pid, 0)
    status, res = pickle.loads(data)
    if status != "ok":
        raise RuntimeError(res)
    return res


def run_parallel(jobs, procs):
    """Run (key1, key2, fn) jobs in forked children, at most `procs` at a time."""
    results, running, pending = {}, {}, list(jobs)
    while pending or running:
        while pending and len(running) < procs:
            i, mode, fn = pending.pop(0)
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(r)
                try:
                    data = pickle.dumps(("ok", fn()))
                except BaseException as e:  # noqa: BLE001
                    data = pickle.dumps(("err", repr(e)))
                with os.fdopen(w, "wb") as f:
                    f.write(data)
                os._exit(0)
            os.close(w)
            running[r] = [(i, mode), pid, b""]
        import select as sel
        ready, _, _ = sel.select(list(running), [], [])
        for r in ready:
            chunk = os.read(r, 1 << 20)
            if chunk:
                running[r][2] += chunk
                continue
            key, pid, data = running.pop(r)
            os.close(r)
            os.waitpid(pid, 0)
            status, res = pickle.loads(data)
            if status != "ok":
                raise RuntimeError(f"{key}: {res}")
            results[key] = res
    return results


def main():
    os.environ["SKIP_INTEGRATION"] = "0"
    os.chdir(REPO)
    sys.path.insert(0, os.path.abspath(REPO))
    import unittest  # noqa: F401  (before sqlglot, as under the test runner)
    import sqlglot  # noqa: F401

    def import_lines():
        lines = set()
        sys.settrace(tracer(lines))
        import tests.test_executor  # noqa: F401
        sys.settrace(None)
        return lines
    imp = in_child(import_lines)

    import tests.test_executor as te
    from tests.helpers import TPCDS_SCHEMA
    from sqlglot import parse_one
    te.TestExecutor.setUpClass()
    cls = te.TestExecutor
    raw = {name: (tuple(t.columns), t.rows) for name, t in cls.tpcds_tables.items()}
    sqls = [(args, sql) for args, sql, _ in cls.tpcds_sqls]
    te.initializer(TPCDS_SCHEMA, cls.tpcds_tables)
    cls.tearDownClass()  # close DuckDB (and its threads) before forking: only Python tables are used

    def make_jobs(i, args, sql):
        def one():
            expr = pickle.loads(pickle.dumps(parse_one(sql)))
            needed = sorted({t.name for t in sqlglot.find_tables(expr)})
            lines = set()
            names = set()
            import builtins
            real_compile = builtins.compile

            def recording_compile(*a, **k):
                # the executor compiles each generated Python expression and evals it with
                # ENV as its globals: record every global name the generated code can look up
                code = real_compile(*a, **k)

                def walk(co):
                    names.update(co.co_names)
                    for c in co.co_consts:
                        if hasattr(c, "co_code"):
                            walk(c)
                if hasattr(code, "co_code"):
                    walk(code)
                return code
            builtins.compile = recording_compile
            c0 = time.process_time()
            sys.settrace(tracer(lines))
            table = te.mp_execute(expr, args)
            sys.settrace(None)
            builtins.compile = real_compile
            traced = time.process_time() - c0
            # untraced timing in the same (now warm) process is not cold; time a second,
            # clean run in a fresh child instead
            return digest(pickle.loads(pickle.dumps(table))), needed, lines, names

        def timed():
            expr = pickle.loads(pickle.dumps(parse_one(sql)))
            c0 = time.process_time()
            table = te.mp_execute(expr, args)
            return time.process_time() - c0, digest(pickle.loads(pickle.dumps(table)))

        return one, timed

    jobs = []
    for i, (args, sql) in enumerate(sqls):
        if args.get("execute"):
            one, timed = make_jobs(i, args, sql)
            jobs += [(i, "trace", one), (i, "time", timed)]
    results = run_parallel(jobs, int(os.environ.get("TPC_BUILD_PROCS", "4")))
    queries = {}
    for i in sorted({j[0] for j in jobs}):
        d, needed, lines, names = results[(i, "trace")]
        cpu, d2 = results[(i, "time")]
        if d != d2:
            raise SystemExit(f"query {i}: result not deterministic")
        queries[i] = {"digest": d, "tables": needed, "lines": lines, "cpu": cpu, "names": names}
        print(f"q{i}: cpu={cpu:.2f} lines={len(lines)}", file=sys.stderr)

    # determinism across processes: a third run of a few queries must match
    for i in list(queries)[:3]:
        args, sql = sqls[i]
        d3 = in_child(lambda: digest(te.mp_execute(parse_one(sql), args)))
        if d3 != queries[i]["digest"]:
            raise SystemExit(f"query {i}: digest differs between runs")

    with open(OUT, "wb") as f:
        pickle.dump({"sqls": sqls, "raw": raw, "queries": queries, "import_lines": imp,
                     "schema": TPCDS_SCHEMA}, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"pinned {len(queries)} queries, total cpu {sum(q['cpu'] for q in queries.values()):.1f}s",
          file=sys.stderr)


if __name__ == "__main__":
    main()
