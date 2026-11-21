"""Merge a second coverage run (reverse order, other hash seed) and mark order-dependent lines."""
import pickle, sys, gzip
IDX, ORDER, ODEP = sys.argv[1], sys.argv[2], sys.argv[3]
idx = pickle.load(open(IDX, "rb")); rev = pickle.load(open(ORDER, "rb"))["rev"]; odep = pickle.load(open(ODEP, "rb"))
lm = idx["linemap"]
for f, fm in rev.items():
    d = lm.setdefault(f, {})
    for ln, m in fm.items():
        if ln in d or any(a < ln <= b for a, b, _ in idx["spans"].get(f, ())):
            d[ln] = d.get(ln, 0) | m
idx["odep"] = {f: sorted(s) for f, s in odep.items() if s}
pickle.dump(idx, open(IDX, "wb"), protocol=5)
open(IDX + ".gz", "wb").write(gzip.compress(pickle.dumps(idx, protocol=5), 9))
print("ok", sum(len(s) for s in idx["odep"].values()))
