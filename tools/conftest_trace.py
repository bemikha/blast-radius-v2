"""pytest plugin: record per-test class-attribute reads and table key lookups in sqlglot."""
import os, sys, pickle, enum, atexit, inspect, collections
import sqlglot, sqlglot.dialects
from sqlglot import generator as _gen
from sqlglot.dialects.dialect import Dialect

CUR = [None]
READS = collections.defaultdict(set)   # (cls_qualname, attr) -> {test}
KEYS = collections.defaultdict(set)    # (owner_qualname, attr, key) -> {test}

def qn(c): return f"{c.__module__}.{c.__qualname__}"
def norm(k):
    if isinstance(k, type): return k.__name__
    if isinstance(k, enum.Enum): return k.name
    if isinstance(k, str): return k
    if isinstance(k, tuple): return "|".join(norm(x) for x in k)
    return None

def mk(base):
    class T(base):
        __slots__ = () if base is not dict else ()
        def __contains__(self, k):
            t = CUR[0]
            if t is not None:
                n = norm(k)
                if n is not None: KEYS[(self._o, self._a, n)].add(t)
            return base.__contains__(self, k)
    return T
class TDict(dict):
    def _rec(self, k):
        t = CUR[0]
        if t is not None:
            n = norm(k)
            if n is not None: KEYS[(self._o, self._a, n)].add(t)
    def __getitem__(self, k): self._rec(k); return dict.__getitem__(self, k)
    def get(self, k, d=None): self._rec(k); return dict.get(self, k, d)
    def __contains__(self, k): self._rec(k); return dict.__contains__(self, k)
class TSet(set):
    def __contains__(self, k):
        t = CUR[0]
        if t is not None:
            n = norm(k)
            if n is not None: KEYS[(self._o, self._a, n)].add(t)
        return set.__contains__(self, k)
class TFrozen(frozenset):
    def __contains__(self, k):
        t = CUR[0]
        if t is not None:
            n = norm(k)
            if n is not None: KEYS[(self._o, self._a, n)].add(t)
        return frozenset.__contains__(self, k)

def wrap(v, owner, attr):
    try:
        if type(v) is dict: w = TDict(v)
        elif type(v) is set: w = TSet(v)
        elif type(v) is frozenset: w = TFrozen(v)
        else: return v
        w._o = owner; w._a = attr
        return w
    except Exception:
        return v

class Rec:
    __slots__ = ("v", "a")
    def __init__(self, v, a): self.v = v; self.a = a
    def __get__(self, obj, objtype=None):
        t = CUR[0]
        if t is not None and objtype is not None:
            READS[(qn(objtype), self.a)].add(t)
        return self.v

# import every module so all classes exist
import pkgutil, importlib
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if m.name.endswith("__main__"): continue
    try: importlib.import_module(m.name)
    except Exception: pass
for name in list(getattr(Dialect, "classes", {}) or {}):
    pass
try:
    from sqlglot.dialects import DIALECTS
    for d in DIALECTS:
        try: Dialect.get_or_raise(d.lower())
        except Exception: pass
except Exception: pass

def all_classes():
    seen = set()
    for mod in list(sys.modules.values()):
        if not getattr(mod, "__name__", "").startswith("sqlglot"): continue
        for v in list(vars(mod).values()):
            if isinstance(v, type) and v.__module__.startswith("sqlglot") and id(v) not in seen:
                seen.add(id(v)); yield v
                for v2 in vars(v).values():
                    if isinstance(v2, type) and v2.__module__.startswith("sqlglot") and id(v2) not in seen:
                        seen.add(id(v2)); yield v2
CLASSES = list(all_classes())
SKIP_BASES = (enum.Enum,)
HIER = {}
for c in CLASSES:
    try: HIER[qn(c)] = [qn(b) for b in c.__mro__ if b.__module__.startswith("sqlglot")]
    except Exception: pass
    if issubclass(c, SKIP_BASES): continue
    try:
        from sqlglot import expressions as _e
        if issubclass(c, _e.Expr): continue
    except Exception: pass
    for a, v in list(vars(c).items()):
        if not a[:1].isupper() or not a.replace("_", "").isupper() and not a.isupper(): 
            if not (a.isupper() or (a[:1].isupper() and a.upper() == a)): continue
        if callable(v) and not isinstance(v, (dict, set, frozenset)): continue
        if isinstance(v, (type, staticmethod, classmethod, property)): continue
        try: setattr(c, a, Rec(wrap(v, qn(c), a), a))
        except Exception: pass

# expression classes instantiated per test (their class-level settings: arg_types, names...)
EXPR_USE = collections.defaultdict(set)
from sqlglot.expressions import core as _core
def _wrap_init(cls):
    orig = cls.__dict__["__init__"]
    def __init__(self, *a, **k):
        t = CUR[0]
        if t is not None:
            EXPR_USE[qn(type(self))].add(t)
        return orig(self, *a, **k)
    cls.__init__ = __init__
for c in CLASSES:
    try:
        if issubclass(c, _core.Expr) and "__init__" in c.__dict__:
            _wrap_init(c)
    except Exception:
        pass

# generator dispatch: record (generator class, expression class)
_orig_build = _gen._build_dispatch
def _bd(cls):
    d = TDict(_orig_build(cls)); d._o = qn(cls); d._a = "__dispatch__"; return d
_gen._build_dispatch = _bd
_gen._DISPATCH_CACHE.clear()

import builtins, io
FILES = collections.defaultdict(set)   # relpath -> {test or 'CLASS:'+prefix}
PHASE = [None]
_open = builtins.open
_REPO = os.getcwd() + os.sep
def _rec_path(f):
    try:
        f = os.path.abspath(os.fspath(f))
    except Exception:
        return
    if f.startswith(_REPO) and "/tests/" in f[len(_REPO)-1:]:
        rel = f[len(_REPO):]
        if CUR[0] is not None:
            FILES[rel].add(CUR[0] if PHASE[0] == "call" else "CLASS:" + CUR[0].rsplit("::", 1)[0])
def _o(f, *a, **k):
    _rec_path(f); return _open(f, *a, **k)
builtins.open = _o; io.open = _o
import pathlib
_rt = pathlib.Path.read_text; _rb = pathlib.Path.read_bytes; _po = pathlib.Path.open
def _prt(self, *a, **k): _rec_path(self); return _rt(self, *a, **k)
def _prb(self, *a, **k): _rec_path(self); return _rb(self, *a, **k)
def _ppo(self, *a, **k): _rec_path(self); return _po(self, *a, **k)
pathlib.Path.read_text = _prt; pathlib.Path.read_bytes = _prb; pathlib.Path.open = _ppo

import pytest
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    CUR[0] = item.nodeid; PHASE[0] = "setup"
    yield
    CUR[0] = None; PHASE[0] = None
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    CUR[0] = item.nodeid; PHASE[0] = "call"
    yield
    CUR[0] = None; PHASE[0] = None

def pytest_sessionfinish(session):
    out = os.environ.get("TRACE_OUT", "/home/claude/br/trace/trace.pkl")
    if hasattr(session.config, "workerinput"):
        out += "." + session.config.workerinput["workerid"]
    pickle.dump(dict(reads={k: sorted(v) for k, v in READS.items()},
                     keys={k: sorted(v) for k, v in KEYS.items()}, hier=HIER, files={k: sorted(v) for k, v in FILES.items()}, expr_use={k: sorted(v) for k, v in EXPR_USE.items()}), open(out, "wb"))
