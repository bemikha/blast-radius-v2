import coverage, json, pickle, collections, ast, os
idx = pickle.load(open("sol/index.pkl", "rb")); tests = idx["tests"]; tid = {t: i for i, t in enumerate(tests)}
def nodeid(c):
    parts = c.split(".")
    for i in range(len(parts)):
        if parts[i].startswith("test_") and i + 1 < len(parts) and parts[i+1][:1].isupper():
            return "/".join(parts[:i+1]) + ".py::" + "::".join(parts[i+1:])
    return c
cov = coverage.Coverage(data_file="/home/claude/br/.coverage_rev"); cov.load(); d = cov.get_data()
root = "/home/claude/br/cov_repo/"
rev = {}
for f in d.measured_files():
    rel = f.replace(root, ""); fm = {}
    for ln, ctxs in d.contexts_by_lineno(f).items():
        m = 0
        for c in ctxs:
            if c and nodeid(c) in tid: m |= 1 << tid[nodeid(c)]
        if m: fm[ln] = m
    rev[rel] = fm
diff = collections.Counter(); ex = []
odep = {}
for f, fm in idx["linemap"].items():
    r = rev.get(f, {}); s = set()
    for ln in set(fm) | set(r):
        if ln in fm or ln in r:
            a, b = fm.get(ln, 0), r.get(ln, 0)
            if a != b and ln in fm or (ln not in fm and b):
                s.add(ln); diff[f] += 1
                if len(ex) < 25 and abs(bin(a).count("1") - bin(b).count("1")) >= 0: ex.append((f, ln, bin(a).count("1"), bin(b).count("1"), bin(a & b).count("1")))
    odep[f] = s
pickle.dump({"rev": rev, "odep": odep}, open("order.pkl", "wb"))
print(sum(diff.values()), diff.most_common(10))
for e in ex: print(e)
