"""Run a commit's selected tests inside the container, without pytest, to prove it harmless.

sqlglot's tests are plain unittest.TestCase classes (no conftest, no pytest config), so each
selected test is run straight from its class in a forked child that sees the patched sources.
The child stops at the first failure: a failing commit only needs to be recognised, and its
static selection is kept. Only an all-pass verdict changes the answer (the commit runs nothing).

Any doubt (patch does not apply, touches non-Python files, a test is skipped or missing,
crash, CPU or wall limit) returns None and the caller keeps its static selection.

Warm parent: once the commits that touch core modules are done, the parent imports unittest,
sqlglot and the shared test helpers, and later children are forked from it. A child is only
forked warm if none of the patched modules is already loaded, so every patched module is still
imported fresh, from the patched source.
"""
import importlib
import importlib.abc
import importlib.util
import json
import os
import re
import resource
import select
import signal
import sys
import time

BASE = os.environ.get("BR_SQLGLOT", "/opt/sqlglot")


def apply_patch(patch_text, base=BASE):
    """-> {relpath: new_text} for every file in the patch, or None if any hunk fails."""
    out, hunks, order = {}, {}, []
    cur = None
    for line in patch_text.split("\n"):
        if line.startswith("diff --git"):
            cur = None
            continue
        if line.startswith("--- "):
            continue
        if line.startswith("+++ "):
            p = line[4:].strip().split("\t")[0]
            if p == "/dev/null":
                return None
            cur = p[2:] if p.startswith("b/") else p
            hunks.setdefault(cur, [])
            order.append(cur)
            continue
        m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if m and cur is not None:
            hunks[cur].append([int(m.group(1)), []])
            continue
        if cur is not None and hunks[cur] and (line[:1] in (" ", "-", "+") or line == ""):
            hunks[cur][-1][1].append(line)
    for path in order:
        try:
            with open(os.path.join(base, path), encoding="utf-8") as f:
                lines = f.read().split("\n")
        except OSError:
            return None
        new, pos = [], 0
        for start, body in hunks[path]:
            while body and body[-1] == "":
                body = body[:-1]
            old_seq = [l[1:] if l else "" for l in body if l[:1] in (" ", "-") or l == ""]
            new_seq = [l[1:] if l else "" for l in body if l[:1] in (" ", "+") or l == ""]
            s = max(start - 1, 0)
            if s < pos or lines[s:s + len(old_seq)] != old_seq:
                return None
            new.extend(lines[pos:s])
            new.extend(new_seq)
            pos = s + len(old_seq)
        new.extend(lines[pos:])
        out[path] = "\n".join(new)
    return out


def module_name(rel):
    name = rel[:-3].replace("/", ".")
    return name[: -len(".__init__")] if name.endswith(".__init__") else name


class _PatchedFinder(importlib.abc.MetaPathFinder):
    def __init__(self, sources, base):
        self.mods = {}
        for rel, text in sources.items():
            self.mods[module_name(rel)] = (os.path.join(base, rel), text, rel.endswith("__init__.py"))

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in self.mods:
            return None
        fpath, text, is_pkg = self.mods[fullname]

        class Loader(importlib.abc.SourceLoader):
            def get_filename(self, name):
                return fpath

            def get_data(self, p):
                if p == fpath:
                    return text.encode("utf-8")
                with open(p, "rb") as f:
                    return f.read()

            def path_stats(self, p):
                raise OSError  # never use the cached .pyc of the unpatched file

        return importlib.util.spec_from_file_location(
            fullname, fpath, loader=Loader(),
            submodule_search_locations=[os.path.dirname(fpath)] if is_pkg else None)


class _Stop(BaseException):
    """Raised from the result object at the first failure: the verdict is already known."""


def _make_result():
    import unittest

    class Result(unittest.TestResult):
        def __init__(self):
            super().__init__()
            self.bad = None

        def _fail(self, test):
            self.bad = test.id() if hasattr(test, "id") else str(test)
            raise _Stop()

        def addFailure(self, test, err):
            self._fail(test)

        def addError(self, test, err):
            self._fail(test)

        def addSubTest(self, test, subtest, err):
            if err is not None:
                self._fail(subtest)

        def addUnexpectedSuccess(self, test):
            self._fail(test)

    return Result()


class _InlinePool:
    """Some tests use a ProcessPoolExecutor; run their jobs inline (256-process limit, CPU)."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, *args, **kwargs):
        import concurrent.futures as cf
        import pickle
        f = cf.Future()
        try:
            fn2, a2, k2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn2(*a2, **k2))))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def map(self, fn, *its, **kw):
        return map(fn, *its)

    def shutdown(self, wait=True, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _child(sources, nodeids, wfd, cpu_limit):
    res = {"error": "no result"}
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit + 1))
        import concurrent.futures as cf
        import concurrent.futures.process as cfp
        import unittest
        cf.ProcessPoolExecutor = _InlinePool
        cfp.ProcessPoolExecutor = _InlinePool
        os.environ["SKIP_INTEGRATION"] = "0"
        os.chdir(BASE)
        if BASE not in sys.path:
            sys.path.insert(0, BASE)
        sys.meta_path.insert(0, _PatchedFinder(sources, BASE))
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        suite = unittest.TestSuite()
        try:
            for nid in nodeids:
                path, cls, meth = nid.split("::")
                mod = importlib.import_module(path[:-3].replace("/", "."))
                suite.addTest(getattr(mod, cls)(meth))
        except BaseException as e:  # noqa: BLE001  an import error is a failure; stay with doubt
            res = {"error": "load: " + repr(e)[:100]}
            raise
        result = _make_result()
        try:
            suite.run(result)
        except _Stop:
            pass
        if result.bad is not None:
            res = {"fail": result.bad}
        elif result.skipped or result.expectedFailures or result.testsRun != len(nodeids) \
                or result.errors or result.failures:
            res = {"error": "skipped or not all ran (%d of %d)" % (result.testsRun, len(nodeids))}
        else:
            res = {"pass": True}
    except BaseException as e:  # noqa: BLE001
        if "error" in res and res["error"] == "no result":
            res = {"error": repr(e)[:100]}
    finally:
        try:
            os.write(wfd, json.dumps(res).encode())
        except Exception:
            pass
        os._exit(0)


def run(sources, nodeids, wall_timeout, cpu_limit):
    """-> ("pass" | "fail" | None, detail)."""
    sys.stdout.flush(); sys.stderr.flush()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        _child(sources, nodeids, w, cpu_limit)
    os.close(w)
    buf, deadline, timed_out = b"", time.monotonic() + wall_timeout, False
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            timed_out = True
            break
        ready, _, _ = select.select([r], [], [], left)
        if not ready:
            continue
        chunk = os.read(r, 1 << 16)
        if not chunk:
            break
        buf += chunk
    os.close(r)
    if timed_out:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
    if timed_out:
        return None, "timeout"
    try:
        res = json.loads(buf.decode() or "{}")
    except ValueError:
        return None, "bad child output"
    if "fail" in res:
        return "fail", res["fail"]
    if res.get("pass") is True:
        return "pass", ""
    return None, res.get("error", "no result (cpu limit?)")


def warm_up():
    """Load what most children need into the parent, once."""
    import unittest  # noqa: F401  (before sqlglot: sqlglot reads this at import time)
    os.environ["SKIP_INTEGRATION"] = "0"
    if BASE not in sys.path:
        sys.path.insert(0, BASE)
    cwd = os.getcwd()
    os.chdir(BASE)
    try:
        import sqlglot  # noqa: F401
        import tests.helpers  # noqa: F401
        import tests.dialects.test_dialect  # noqa: F401
    finally:
        os.chdir(cwd)


_CPU = None


def estimate_cpu(nodeids):
    """Local CPU seconds to run these tests here without pytest (fitted on mutants)."""
    global _CPU
    if _CPU is None:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cpu_est.json")) as f:
                _CPU = json.load(f)
        except OSError:
            _CPU = {"test": {}, "class": {}}
    t = sum(_CPU["test"].get(n, 0.004) for n in nodeids)
    t += sum(_CPU["class"].get(c, 0.0) for c in {n.rsplit("::", 1)[0] for n in nodeids})
    old = 0.35 + 1.5 * t  # the pytest-based estimate this was fitted against
    return max(0.08, 0.9 * old - 0.17)
