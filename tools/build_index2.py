"""Add tracer data + class-level AST structure to the index."""
import ast, os, pickle, sys, collections
REPO, TRACE, IDX = sys.argv[1], sys.argv[2], sys.argv[3]
idx = pickle.load(open(IDX, "rb")); tr = pickle.load(open(TRACE, "rb"))
tid = {t: i for i, t in enumerate(idx["tests"])}
def mask(ts):
    m = 0
    for t in ts:
        i = tid.get(t)
        if i is not None: m |= 1 << i
    return m
reads = {k: mask(v) for k, v in tr["reads"].items()}
keys = {k: mask(v) for k, v in tr["keys"].items()}
hier = tr["hier"]
sub = collections.defaultdict(set)
for d, mro in hier.items():
    for c in mro: sub[c].add(d)
# per class: union of all reads (class-usage mask)
cls_use = collections.defaultdict(int)
for (c, a), m in reads.items(): cls_use[c] |= m
for (c, a, k), m in keys.items(): cls_use[c] |= m

def keyname(node):
    if isinstance(node, ast.Attribute): return node.attr
    if isinstance(node, ast.Name): return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str): return node.value
    if isinstance(node, ast.Tuple): return "|".join(filter(None, (keyname(e) for e in node.elts)))
    return None

structure = {}
for root, _, fs in os.walk(os.path.join(REPO, "sqlglot")):
    for fn in fs:
        if not fn.endswith(".py"): continue
        p = os.path.join(root, fn); rel = os.path.relpath(p, REPO)
        mod = rel[:-3].replace("/", ".")
        if mod.endswith(".__init__"): mod = mod[:-9]
        tree = ast.parse(open(p).read())
        items = []   # (start, end, kind, info)
        def visit(node, prefix):
            for n in node.body:
                if isinstance(n, ast.ClassDef):
                    q = prefix + n.name
                    items.append((n.lineno, n.end_lineno, "class", f"{mod}.{q}"))
                    for b in n.body:
                        if isinstance(b, (ast.Assign, ast.AnnAssign)):
                            tgt = b.targets[0] if isinstance(b, ast.Assign) else b.target
                            if not isinstance(tgt, ast.Name) or b.value is None: continue
                            ents = []
                            v = b.value
                            if isinstance(v, ast.Dict):
                                for k, val in zip(v.keys, v.values):
                                    if k is None:
                                        continue
                                    ents.append((k.lineno, val.end_lineno, keyname(k), k.col_offset, val.end_col_offset))
                            elif isinstance(v, (ast.Set, ast.List, ast.Tuple)) or (isinstance(v, ast.Call) and v.args and isinstance(v.args[0], (ast.Set, ast.List, ast.Tuple, ast.Dict))):
                                elts = v.elts if not isinstance(v, ast.Call) else (v.args[0].elts if not isinstance(v.args[0], ast.Dict) else [])
                                if isinstance(v, ast.Call) and isinstance(v.args[0], ast.Dict):
                                    for k, val in zip(v.args[0].keys, v.args[0].values):
                                        if k is not None: ents.append((k.lineno, val.end_lineno, keyname(k), k.col_offset, val.end_col_offset))
                                for e in elts:
                                    if not isinstance(e, ast.Starred): ents.append((e.lineno, e.end_lineno, keyname(e), e.col_offset, e.end_col_offset))
                            items.append((b.lineno, b.end_lineno, "attr", (f"{mod}.{q}", tgt.id, ents)))
                        elif isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            items.append((b.lineno, b.end_lineno, "method", (f"{mod}.{q}", b.name)))
                    visit(n, q + ".")
        visit(tree, "")
        structure[rel] = items
from sqlglot import expressions as E  # base checkout on path
expr_keys = {k: v.__name__ for k, v in E.EXPR_CLASSES.items()} if hasattr(E, "EXPR_CLASSES") else {}
import enum, importlib, pkgutil, sqlglot
enums = set()
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if m.name.endswith("__main__"): continue
    _argv = sys.argv; sys.argv = ["x"]
    try: mod = importlib.import_module(m.name)
    except BaseException: continue
    finally: sys.argv = _argv
    for v in vars(mod).values():
        if isinstance(v, type) and issubclass(v, enum.Enum) and v.__module__.startswith("sqlglot"): enums.add(v.__name__)
expr_use = {k: mask(v) for k, v in tr.get("expr_use", {}).items()}
expr_classes = sorted(c for c, mro in hier.items() if "sqlglot.expressions.core.Expr" in mro)
idx.update(expr_use=expr_use, expr_classes=expr_classes, enums=sorted(enums), reads=reads, keys=keys, sub={k: sorted(v) for k, v in sub.items()}, cls_use=dict(cls_use),
           structure=structure, expr_keys=expr_keys)
pickle.dump(idx, open(IDX, "wb"), protocol=5)
print("ok", len(reads), len(keys), len(structure), len(expr_keys))
