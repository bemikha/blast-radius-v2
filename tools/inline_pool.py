"""pytest plugin: run ProcessPoolExecutor work inline so coverage/tracing see it."""
import concurrent.futures as cf

class InlinePool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*initargs)
    def submit(self, fn, *args, **kwargs):
        import pickle
        f = cf.Future()
        try:
            fn2, a2, k2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn2(*a2, **k2))))
        except BaseException as e:
            f.set_exception(e)
        return f
    def map(self, fn, *its, **kw):
        return map(fn, *its)
    def shutdown(self, wait=True, **kw):
        pass
    def __enter__(self): return self
    def __exit__(self, *a): self.shutdown(); return False

cf.ProcessPoolExecutor = InlinePool
import concurrent.futures.process as _p
_p.ProcessPoolExecutor = InlinePool
