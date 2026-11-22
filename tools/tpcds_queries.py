"""Per-query coverage of TestExecutor::test_execute_tpcds on the pinned commit -> maps/tpcds.bin

    python tools/tpcds_queries.py <sqlglot_root> <duck.pkl> <out>

The test parses every TPC-DS query, executes the ones marked `execute: true` with sqlglot's
Python executor and compares each result with DuckDB's. Queries are independent: a query
whose parse / execution / comparison never runs a changed line gives the same result as
on the pinned commit. Recorded:
  setup    lines run while importing and in setUpClass (a change there: every query)
  queries  for each query, the lines run while parsing, executing and comparing it
  cpu      CPU seconds of each query, and of the setup
"""

import os
import pickle
import sys
import time
import zlib

repo, duck_path, out = os.path.realpath(sys.argv[1]), sys.argv[2], os.path.abspath(sys.argv[3])
prefix = repo + os.sep
os.environ["SKIP_INTEGRATION"] = "0"
os.chdir(repo)
sys.path.insert(0, repo)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

mon = sys.monitoring
TOOL = mon.COVERAGE_ID
mon.use_tool_id(TOOL, "tpcds")
cur = set()
_ok = {}


def _rec(code, line):
    fn = code.co_filename
    ok = _ok.get(fn)
    if ok is None:
        ok = _ok[fn] = fn.startswith(prefix)
    if ok:
        cur.add((fn[len(prefix):].replace("\\", "/"), line))
    return mon.DISABLE


def _start(code, offset):
    return _rec(code, code.co_firstlineno)


mon.register_callback(TOOL, mon.events.LINE, _rec)
mon.register_callback(TOOL, mon.events.PY_START, _start)
mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)


def take():
    global cur
    s = cur
    cur = set()
    mon.restart_events()
    d = {}
    for f, ln in s:
        d.setdefault(f, set()).add(ln)
    return d


c0 = time.process_time()
import runner  # noqa: E402,F401
import concurrent.futures  # noqa: E402
import tests.test_executor as te  # noqa: E402
from sqlglot import parse_one  # noqa: E402

with open(duck_path, "rb") as f:
    duck = pickle.load(f)
runner._install_duck_cache(te, te.TestExecutor, duck)
cls = te.TestExecutor
cls.setUpClass()
test = cls("test_execute_tpcds")
te.initializer(te.TPCDS_SCHEMA, cls.tpcds_tables)
setup = take()
setup_cpu = time.process_time() - c0

queries, cpu = [], []
failed = []
for i, (meta, sql, _) in enumerate(cls.tpcds_sqls):
    t0 = time.process_time()
    e = pickle.loads(pickle.dumps(parse_one(sql)))
    table = te.mp_execute(e, pickle.loads(pickle.dumps(meta)))
    if table is not None:
        table = pickle.loads(pickle.dumps(table))
        try:
            test.subtestHelper(i, table, tpch=False)
        except Exception as ex:  # noqa: BLE001
            failed.append((i, repr(ex)[:200]))
    cpu.append(time.process_time() - t0)
    queries.append(take())

blob = zlib.compress(pickle.dumps({"setup": setup, "queries": queries, "cpu": cpu, "setup_cpu": setup_cpu,
                                   "executes": [bool(m.get("execute")) for m, _s, _e in cls.tpcds_sqls]}, protocol=4), 9)
with open(out, "wb") as f:
    f.write(blob)
print(f"queries {len(queries)} executed {sum(1 for m, _s, _e in cls.tpcds_sqls if m.get('execute'))} "
      f"setup_cpu {setup_cpu:.2f} query cpu {sum(cpu):.2f} failed {failed} bytes {len(blob)}")
