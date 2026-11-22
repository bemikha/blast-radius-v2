"""Entry point: read /data, print one JSON line per commit."""
import json, os, sys, time
t0 = time.process_time()
wall0 = time.monotonic()
DATA = os.environ.get("BR_DATA", "/data")
import select_tests as S

# --- in-container check (see check.py) ---------------------------------------------------
# A commit is checked only when the expected saving beats the expected CPU at server prices.
CHECK = os.environ.get("BR_CHECK", "1") == "1"
P_PASS = float(os.environ.get("BR_P_PASS", "0.25"))         # share of checked commits that pass
CPU_FACTOR = float(os.environ.get("BR_CPU_FACTOR", "2.5"))  # server CPU s per local CPU s
FAIL_SHARE = 0.6           # a failing check stops at the first failure: share of a full run
MAX_EST = float(os.environ.get("BR_MAX_EST", "0.6"))        # never check above this (local CPU s)
WALL_BUDGET = 45.0         # stop checking after this much wall time (3 min limit, 4.5 in final)
CPU_BUDGET = 12.0          # ... or after this much CPU of our own

# --- TPC-DS check for executor-only patches (see tpccheck.py) ------------------------------
TPC = os.environ.get("BR_TPC", "1") == "1"
TPC_P_PASS = float(os.environ.get("BR_TPC_P_PASS", "0.5"))
TPC_CPU_FACTOR = float(os.environ.get("BR_TPC_CPU_FACTOR", "1.7"))
TPC_FAIL_SHARE = 0.3       # a differing result stops the run early
TPC_WALL_BUDGET = 100.0    # never start a TPC-DS check after this much wall time
TPC_CPU_BUDGET = 45.0      # ... or once this much CPU is spent


def split(items, k):
    ws = [[] for _ in range(k)]; loads = [0] * k
    for ms, n in sorted(items, reverse=True):
        j = loads.index(min(loads)); ws[j].append(n); loads[j] += ms
    return ws, (max(loads) if items else 0)

def best_split(items):
    if not items: return []
    best = None
    for k in range(1, 9):
        ws, mx = split(items, k)
        cost = mx + 2000 * k
        if best is None or cost < best[0]: best = (cost, ws)
    return [w for w in best[1] if w]


def static_cost(workers, by_num):
    if not workers:
        return 0.0
    return max(sum(round(by_num[n]["seconds"] * 1000) for n in w) for w in workers) / 1000 + 2 * len(workers)


def own_cpu():
    import resource
    a = resource.getrusage(resource.RUSAGE_SELF); b = resource.getrusage(resource.RUSAGE_CHILDREN)
    return a.ru_utime + a.ru_stime + b.ru_utime + b.ru_stime


def plan_check(text, sel, workers, by_id, by_num):
    """-> (sources, nodeids, est) if this commit is worth checking, else None."""
    import check as C
    nodeids = [t for t in sel if t in by_id]  # tests the environment skips are not numbered
    if not nodeids or any(t.count("::") != 2 for t in nodeids):
        return None
    est = C.estimate_cpu(nodeids)
    if est > MAX_EST:
        return None
    cost = static_cost(workers, by_num)
    expected_cpu = CPU_FACTOR * est * (P_PASS + (1 - P_PASS) * FAIL_SHARE)
    if P_PASS * cost <= expected_cpu:
        return None
    sources = C.apply_patch(text)
    if not sources or any(not (p.startswith("sqlglot/") and p.endswith(".py")) for p in sources):
        return None
    return sources, nodeids, est


def maybe_drop_tpcds(cid, text, sel, workers, by_id, by_num):
    """-> sel without test_execute_tpcds if an in-container rerun proves it passes, else sel."""
    import check as C
    import tpccheck as T
    if T.TPCDS not in sel or not T.available():
        return sel
    if time.monotonic() - wall0 > TPC_WALL_BUDGET or own_cpu() > TPC_CPU_BUDGET:
        return sel
    sources = C.apply_patch(text)
    if not T.executor_only(sources):
        return sel
    indices, why = T.affected_queries(text, sources)
    est = T.estimate_cpu(indices)
    rest = [t for t in sel if t != T.TPCDS]
    after = best_split([(round(by_id[t]["seconds"] * 1000), by_id[t]["number"]) for t in rest if t in by_id])
    saving = static_cost(workers, by_num) - static_cost(after, by_num)
    expected_cpu = TPC_CPU_FACTOR * est * (TPC_P_PASS + (1 - TPC_P_PASS) * TPC_FAIL_SHARE)
    if TPC_P_PASS * saving <= expected_cpu:
        print(f"{cid}: tpcds skip ({why}, {len(indices)} queries, est={est:.1f} saving={saving:.1f})", file=sys.stderr)
        return sel
    c0 = own_cpu()
    verdict, detail = T.run(sources, indices, wall_timeout=max(10.0, 4 * est))
    print(f"{cid}: tpcds {verdict} ({why}, {len(indices)} queries) est={est:.1f} cpu={own_cpu() - c0:.1f} "
          f"saving={saving:.1f} {str(detail)[:60]}", file=sys.stderr)
    return rest if verdict == "pass" else sel


def run_checks(todo, answers, by_num):
    import check as C
    here = os.path.dirname(os.path.abspath(__file__))
    warm_mods = set(json.load(open(os.path.join(here, "warm_modules.json"))))
    # cold first: commits that patch a module the warm parent would already hold
    cold = [x for x in todo if {C.module_name(r) for r in x[1]} & warm_mods]
    warm = [x for x in todo if x not in cold]
    for phase, batch in (("cold", cold), ("warm", warm)):
        if phase == "warm" and batch:
            C.warm_up()
        for cid, sources, nodeids, est in batch:
            if time.monotonic() - wall0 > WALL_BUDGET or own_cpu() > CPU_BUDGET:
                print(f"{cid}: check budget spent", file=sys.stderr)
                continue
            # hard guard: a patched module must never be one the parent already loaded
            if {C.module_name(r) for r in sources} & set(sys.modules):
                print(f"{cid}: patched module already loaded, skip", file=sys.stderr)
                continue
            c0 = own_cpu()
            verdict, detail = C.run(sources, nodeids, wall_timeout=max(5.0, 8 * est),
                                    cpu_limit=max(2, int(4 * est) + 1))
            print(f"{cid}: {phase} {verdict} est={est:.2f} cpu={own_cpu() - c0:.2f} "
                  f"static={static_cost(answers[cid], by_num):.2f} {str(detail)[:80]}", file=sys.stderr)
            if verdict == "pass":
                answers[cid] = []


def main():
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    by_id = {t["id"]: t for t in tests}
    by_num = {t["number"]: t for t in tests}
    zero_ms = [t["number"] for t in tests if round(t["seconds"] * 1000) == 0]
    cdir = os.path.join(DATA, "commits")
    answers, todo = {}, []
    for fn in sorted(os.listdir(cdir)):
        if not fn.endswith(".patch"): continue
        cid = fn[:-6]
        text = open(os.path.join(cdir, fn), encoding="utf-8", errors="replace").read()
        try:
            sel = S.select(text)
        except Exception as e:  # never crash: fall back to everything
            print(f"{cid}: selector error {e!r}", file=sys.stderr)
            sel = [t["id"] for t in tests]
        items = [(round(by_id[t]["seconds"] * 1000), by_id[t]["number"]) for t in sel if t in by_id]
        workers = best_split(items)
        if workers and TPC:
            try:
                sel2 = maybe_drop_tpcds(cid, text, sel, workers, by_id, by_num)
                if sel2 is not sel:
                    sel = sel2
                    workers = best_split([(round(by_id[t]["seconds"] * 1000), by_id[t]["number"])
                                          for t in sel if t in by_id])
            except Exception as e:  # any doubt: keep the static selection
                print(f"{cid}: tpcds error {e!r}", file=sys.stderr)
        answers[cid] = workers
        if workers and CHECK:
            try:
                p = plan_check(text, sel, workers, by_id, by_num)
                if p:
                    todo.append((cid,) + p)
            except Exception as e:
                print(f"{cid}: plan error {e!r}", file=sys.stderr)
    if todo:
        try:
            run_checks(todo, answers, by_num)
        except Exception as e:  # any doubt: keep the static answers
            print(f"check error {e!r}", file=sys.stderr)

    out = []
    for cid in sorted(answers):
        workers = answers[cid]
        if workers:
            # tests recorded at 0 ms cost nothing once a worker exists: free insurance
            have = {n for w in workers for n in w}
            workers[-1].extend(n for n in zero_ms if n not in have)
        out.append(json.dumps({"commit": cid, "workers": workers}))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    print(f"cpu {time.process_time() - t0:.2f}s own+children {own_cpu():.2f}s", file=sys.stderr)

if __name__ == "__main__":
    main()
