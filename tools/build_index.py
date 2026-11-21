"""Build-time: turn per-test coverage + source ASTs into a compact selection index."""
import ast, json, os, pickle, re, sys, collections
REPO = sys.argv[1]; COV = sys.argv[2]; DUR = sys.argv[3]; OUT = sys.argv[4]
cm = json.load(open(COV))
ctx = cm["tests"]
def nodeid(c):
    parts = c.split(".")
    # tests.dialects.test_duckdb.TestDuckDB.test_x
    for i in range(len(parts)):
        if parts[i].startswith("test_") and i + 1 < len(parts) and parts[i+1][:1].isupper():
            return "/".join(parts[:i+1]) + ".py::" + "::".join(parts[i+1:])
    return c
tests = [nodeid(c) for c in ctx]
# durations
dur = collections.Counter()
for l in open(DUR):
    m = re.match(r"([\d.]+)s (setup|call|teardown)\s+(\S+)", l)
    if m: dur[m.group(3)] += float(m.group(1))
durs = [max(dur.get(t, 0.0), 0.003) for t in tests]

# import-time lines: not inside any function body
def runtime_lines(path):
    src = open(path).read(); tree = ast.parse(src)
    rt = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            body = n.body if not isinstance(n, ast.Lambda) else [n.body]
            for b in body:
                rt.update(range(b.lineno, (b.end_lineno or b.lineno) + 1))
    return rt
# importer contexts: tests that cover many import-time lines
imp_count = collections.Counter()
rtl = {}
for f, lines in cm["map"].items():
    p = os.path.join(REPO, f)
    rtl[f] = runtime_lines(p)
    for ln, ids in lines.items():
        if int(ln) not in rtl[f]:
            for i in ids: imp_count[i] += 1
importers = {i for i, c in imp_count.items() if c > 200}
print("importers", [tests[i] for i in importers][:10], file=sys.stderr)

def mask(ids):
    m = 0
    for i in ids: m |= 1 << i
    return m
linemap = {}   # file -> {line: mask}  runtime coverage only
for f, lines in cm["map"].items():
    d = {}
    for ln, ids in lines.items():
        ln = int(ln)
        if ln not in rtl[f]: continue
        d[ln] = mask(ids)
    linemap[f] = d

# function spans per source file (for insertions / uncovered lines inside a function)
def func_spans(path):
    tree = ast.parse(open(path).read()); spans = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spans.append((n.lineno, n.end_lineno, n.name))
    return spans
spans = {f: func_spans(os.path.join(REPO, f)) for f in cm["map"]}

# dialect usage: tests that run any function line in a dialect-specific file
DIALECT_DIRS = ("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/tokenizers/", "sqlglot/typing/")
dial_mask = collections.defaultdict(int)
for f, d in linemap.items():
    if f.startswith(DIALECT_DIRS):
        name = os.path.basename(f)[:-3]
        if name in ("__init__", "dialect"): continue
        for m in d.values(): dial_mask[name] |= m

# test sources (for token search) and test-file -> mask
test_src = {}
for t in tests:
    path, *rest = t.split("::")
test_files = sorted({t.split("::")[0] for t in tests})
tid = {t: i for i, t in enumerate(tests)}
for tf in test_files:
    src = open(os.path.join(REPO, tf)).read(); tree = ast.parse(src); lines = src.split("\n")
    for cls in tree.body:
        if isinstance(cls, ast.ClassDef):
            for fn in cls.body:
                if isinstance(fn, ast.FunctionDef):
                    k = f"{tf}::{cls.name}::{fn.name}"
                    if k in tid:
                        test_src[tid[k]] = "\n".join(lines[fn.lineno-1:fn.end_lineno])
# test function line spans (for diffs touching tests)
test_spans = collections.defaultdict(list)
for i, t in enumerate(tests):
    pass
for tf in test_files:
    tree = ast.parse(open(os.path.join(REPO, tf)).read())
    for cls in tree.body:
        if isinstance(cls, ast.ClassDef):
            for fn in cls.body:
                if isinstance(fn, ast.FunctionDef):
                    k = f"{tf}::{cls.name}::{fn.name}"
                    if k in tid: test_spans[tf].append((fn.lineno, fn.end_lineno, tid[k]))
# fixture files referenced by tests
pickle.dump(dict(tests=tests, durs=durs, linemap=linemap, spans=spans, dial_mask=dict(dial_mask),
                 test_src=test_src, test_spans=dict(test_spans)), open(OUT, "wb"), protocol=5)
print("ok", len(tests), file=sys.stderr)
