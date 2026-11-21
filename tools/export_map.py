import coverage, json, sys
cov = coverage.Coverage(data_file="/home/claude/br/.coverage"); cov.load()
d = cov.get_data()
root = "/home/claude/br/sqlglot/"
ctx_ids = {}; out = {}
for f in d.measured_files():
    rel = f.replace(root, "")
    m = d.contexts_by_lineno(f)
    fm = {}
    for ln, ctxs in m.items():
        ids = []
        for c in ctxs:
            if not c: continue
            if c not in ctx_ids: ctx_ids[c] = len(ctx_ids)
            ids.append(ctx_ids[c])
        if ids: fm[ln] = sorted(ids)
    out[rel] = fm
json.dump({"tests": sorted(ctx_ids, key=ctx_ids.get), "map": out}, open("covmap.json","w"))
print(len(ctx_ids), len(out)); print(list(ctx_ids)[:5])
