"""Final pass: module-level assignments, fixture-file map, multi-line string spans."""
import ast, os, pickle, sys
REPO, TRACE, IDX = sys.argv[1], sys.argv[2], sys.argv[3]
idx = pickle.load(open(IDX, "rb")); tr = pickle.load(open(TRACE, "rb"))
tid = {t: i for i, t in enumerate(idx["tests"])}
ma, strs, imp, stmts, docspans = {}, {}, {}, {}, {}
for root, _, fs in os.walk(os.path.join(REPO, "sqlglot")):
    for fn in fs:
        if not fn.endswith(".py"): continue
        p = os.path.join(root, fn); rel = os.path.relpath(p, REPO)
        tree = ast.parse(open(p).read())
        ma[rel] = [(n.lineno, n.end_lineno, (n.targets[0] if isinstance(n, ast.Assign) else n.target).id)
                   for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))
                   and isinstance((n.targets[0] if isinstance(n, ast.Assign) else n.target), ast.Name)]
        docs = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body:
                b0 = n.body[0]
                if isinstance(b0, ast.Expr) and isinstance(b0.value, ast.Constant) and isinstance(b0.value.value, str):
                    docs.add((b0.value.lineno, b0.value.end_lineno))
        strs[rel] = [(n.lineno, n.end_lineno) for n in ast.walk(tree)
                     if isinstance(n, (ast.Constant, ast.JoinedStr)) and n.end_lineno > n.lineno
                     and (isinstance(n, ast.JoinedStr) or isinstance(n.value, str))
                     and (n.lineno, n.end_lineno) not in docs]
        docspans[rel] = sorted(docs)
        hs = []
        for n in ast.walk(tree):
            if isinstance(n, ast.stmt):
                body = getattr(n, "body", None)
                if isinstance(body, list) and body and not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    end = body[0].lineno - 1
                    # headers: if/while/for/with conditions
                    if end > n.lineno: hs.append((n.lineno, end))
                elif not isinstance(body, list) and n.end_lineno > n.lineno:
                    hs.append((n.lineno, n.end_lineno))
        stmts[rel] = hs
        out = []
        for n in ast.walk(tree):
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    out.append((getattr(a, "lineno", n.lineno), getattr(a, "end_lineno", n.end_lineno),
                                (a.asname or a.name).split(".")[0], n.lineno, n.end_lineno))
        imp[rel] = out
files = {}
for f, ts in tr["files"].items():
    m = 0
    for t in ts:
        if t.startswith("CLASS:"):
            pre = t[6:] + "::"
            for x, i in tid.items():
                if x.startswith(pre): m |= 1 << i
        elif t in tid: m |= 1 << tid[t]
    files[f] = m
covsrc = {}
for f, d in idx["linemap"].items():
    lines = open(os.path.join(REPO, f)).read().split("\n")
    covsrc[f] = {ln: lines[ln - 1] for ln in d if ln - 1 < len(lines)}
idx.update(docspans=docspans, covsrc=covsrc, stmts=stmts, module_assigns=ma, strspans=strs, imports=imp, files=files)
pickle.dump(idx, open(IDX, "wb"), protocol=5)
import gzip
open(IDX + ".gz", "wb").write(gzip.compress(pickle.dumps(idx, protocol=5), 9))
print("ok", len(files))
