import json, sys, os
sys.path.insert(0, "/home/claude/br/sol")
import select_tests as S
K = "/home/claude/br/kit/sample"
tests = json.load(open(f"{K}/tests.json"))["tests"]; num = {t["number"]: t for t in tests}
ans = json.load(open(f"{K}/answers.json"))["commits"]
tot = 0
for cid, a in sorted(ans.items()):
    sel = set(S.select(open(f"{K}/commits/{cid}.patch").read()))
    fails = [num[n]["id"] for n in a["failing"]]
    miss = [f for f in fails if f not in sel]
    cost = sum(num_t["seconds"] for num_t in tests if num_t["id"] in sel)
    tot += cost
    print(f"{cid} {a['kind']:8} fail={len(fails):2} sel={len(sel):4} sec={cost:7.2f} missed={[m.split('::',1)[1] for m in miss][:4]}")
print("total sel sec", round(tot, 1))
