"""Record DuckDB reference results of TestExecutor on the pinned commit -> maps/duck.pkl

    python tools/record_duck.py <sqlglot_root> <out.pkl>

DuckDB computes the expected result of every TPC-H / TPC-DS query from the CSV
fixtures. That result depends on sqlglot only through the SQL text sent to DuckDB,
so it is stored keyed by (connection, sql) and replayed when the text is unchanged.
"""
import os
import pickle
import sys
import unittest

repo = os.path.realpath(sys.argv[1])
out = os.path.abspath(sys.argv[2])
os.environ["SKIP_INTEGRATION"] = "0"
os.chdir(repo)
sys.path.insert(0, repo)

import concurrent.futures  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

rec = {}


class Rec:
    def __init__(self, real, name):
        self._real, self._name = real, name

    def execute(self, sql, *a, **k):
        r = self._real.execute(sql, *a, **k)
        if a or k:
            return r
        me = self

        class W:
            def fetchdf(self_inner):
                f = r.fetchdf()
                rec[(me._name, sql)] = f.copy()
                return f

        return W()

    def close(self):
        return self._real.close()


import runner  # noqa: E402  (for InlinePool semantics, reuse child_main's pool by running a job)

from concurrent.futures import Future  # noqa: E402


class InlinePool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
        if initializer:
            initializer(*initargs)

    def submit(self, fn, *a, **k):
        f = Future()
        try:
            a2, k2 = pickle.loads(pickle.dumps((a, k)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a2, **k2))))
        except BaseException as e:  # noqa: B902
            f.set_exception(e)
        return f

    def shutdown(self, wait=True, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


concurrent.futures.ProcessPoolExecutor = InlinePool
import tests.test_executor as te  # noqa: E402

orig = te.TestExecutor.__dict__["setUpClass"].__func__


def setUpClass(klass):
    orig(klass)
    klass.tpch_conn = Rec(klass.tpch_conn, "tpch")
    klass.tpcds_conn = Rec(klass.tpcds_conn, "tpcds")


te.TestExecutor.setUpClass = classmethod(setUpClass)
suite = unittest.TestLoader().loadTestsFromTestCase(te.TestExecutor)
r = unittest.TestResult()
suite.run(r)
print("failures", len(r.failures), "errors", len(r.errors))
with open(out, "wb") as f:
    pickle.dump(rec, f, protocol=4)
print("recorded", len(rec), "bytes", os.path.getsize(out))
