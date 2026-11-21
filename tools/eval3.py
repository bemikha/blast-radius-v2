import os, re, sys, glob, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import select_tests as S
from main import best_split
T = json.load(open("/home/claude/br/kit/sample/tests.json"))["tests"]; byid = {t["id"]: t for t in T}; num = {t["number"]: t for t in T}
def ids(txt):
    out = set()
    for m in re.finditer(r"(?:FAILED|ERROR|SUBFAILED)", txt):
        mm = re.search(r"tests/\S+?\.py::[A-Za-z0-9_]+::[A-Za-z0-9_]+", txt[m.end():])
        if mm: out.add(mm.group(0))
    return out
def cost(sel):
    ws = best_split([(round(byid[t]["seconds"] * 1000), byid[t]["number"]) for t in sel if t in byid])
    return 0 if not ws else max(sum(round(num[n]["seconds"] * 1000) for n in w) for w in ws) / 1000 + 2 * len(ws)
n = miss = 0; c_tot = i_tot = 0
for g in sorted(glob.glob("/home/claude/br/gt3/*.txt")):
    name = os.path.basename(g)[:-4]; txt = open(g).read()
    if "APPLYFAIL" in txt: continue
    fails = ids(txt); sel = set(S.select(open(f"/home/claude/br/mut2/{name}.patch").read()))
    missed = fails - sel; c = cost(sel); i = cost(fails)
    n += 1; miss += bool(missed); c_tot += c; i_tot += i
    if missed or c - i > 2.5:
        print(f"{name:20} fail={len(fails):4} sel={len(sel):4} cost={c:6.1f} ideal={i:6.1f} {'MISS ' + str(sorted(missed)[:3]) if missed else ''}")
print(f"n={n} missed={miss} cost={c_tot:.1f} ideal={i_tot:.1f}")
