import os, re, sys, glob, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import select_tests as S
T = json.load(open("/home/claude/br/kit/sample/tests.json"))["tests"]; SEC = {t["id"]: t["seconds"] for t in T}
def ids(txt):
    return {m.group(0) for m in re.finditer(r"tests/\S+?\.py::[A-Za-z0-9_]+::[A-Za-z0-9_]+", txt)}
n = miss = 0; cost = 0.0; ideal = 0.0
for g in sorted(glob.glob("/home/claude/br/gt2/*.txt")):
    name = os.path.basename(g)[:-4]; txt = open(g).read()
    if "APPLYFAIL" in txt: continue
    fails = ids(txt)
    sel = set(S.select(open(f"/home/claude/br/mut2/{name}.patch").read()))
    missed = fails - sel
    c = sum(SEC.get(t, 0) for t in sel) + (2 if sel else 0)
    i = sum(SEC.get(t, 0) for t in fails) + (2 if fails else 0)
    n += 1; miss += bool(missed); cost += c; ideal += i
    flag = "MISS" if missed else ""
    if missed or c - i > 3:
        print(f"{name:24} fail={len(fails):3} sel={len(sel):4} cost={c:6.1f} ideal={i:6.1f} {flag} {sorted(missed)[:3]}")
print(f"n={n} missed={miss} cost={cost:.1f} ideal={ideal:.1f}")
