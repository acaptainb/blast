"""Run ONE sqlglot test in this (fresh) process and record what it depends on.

    python cov_one.py <sqlglot_root> <out.json> <test_id>

Recorded, all from the pinned commit:
  cov     source lines executed by module / class bodies (i.e. at import time)
  covf    source lines executed inside function bodies
  opened  every file under the checkout that was opened through Python
  inst    every sqlglot expression class that was instantiated
The test runs through unittest directly; process pools run inline (with a pickle
round trip, as a real pool would do) so one process sees everything.
"""

import sys
import os
import json
import time

repo = os.path.realpath(sys.argv[1])
out_path = os.path.abspath(sys.argv[2])
test_id = sys.argv[3]
prefix = repo + os.sep

# ---- line coverage -------------------------------------------------------
mon = sys.monitoring
TOOL = mon.COVERAGE_ID
mon.use_tool_id(TOOL, "depmap")
seen = set()
_ok = {}


def _on_line(code, line):
    fn = code.co_filename
    ok = _ok.get(fn)
    if ok is None:
        ok = _ok[fn] = fn.startswith(prefix)
    if ok:
        # co_flags & 1 (CO_OPTIMIZED): a def / lambda / generator body, not a module or class body
        seen.add((fn, line, code.co_flags & 1))
    return mon.DISABLE


def _on_start(code, offset):
    # A lambda (or one-line function) whose first line also runs as class / module code gets
    # no LINE event for that line when it is called: record its first line on every start.
    fn = code.co_filename
    ok = _ok.get(fn)
    if ok is None:
        ok = _ok[fn] = fn.startswith(prefix)
    if ok:
        seen.add((fn, code.co_firstlineno, 1))
    return mon.DISABLE


mon.register_callback(TOOL, mon.events.LINE, _on_line)
mon.register_callback(TOOL, mon.events.PY_START, _on_start)
mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)

# ---- opened files ---------------------------------------------------------
import builtins  # noqa: E402
import io  # noqa: E402

opened = set()
_real_open = builtins.open
_real_open_code = io.open_code


hopened = set()  # opened by tests/helpers.py fixture loaders (their use is tracked per entry)
HELPER_LOADERS = {"load_sql_fixture_pairs", "load_sql_fixtures"}


def _note(path):
    try:
        if isinstance(path, int):
            return
        p = os.path.realpath(os.fspath(path))
        if isinstance(p, bytes):
            p = p.decode("utf-8", "replace")
        if p.startswith(prefix):
            rel = p[len(prefix):].replace("\\", "/")
            f = sys._getframe(2)
            if f.f_code.co_name in HELPER_LOADERS and f.f_code.co_filename.replace("\\", "/").endswith("tests/helpers.py"):
                hopened.add(rel)
            else:
                opened.add(rel)
    except Exception:
        pass


def _open(file, *a, **k):
    _note(file)
    return _real_open(file, *a, **k)


def _open_code(path):
    _note(path)
    return _real_open_code(path)


builtins.open = _open
io.open = _open
io.open_code = _open_code

# ---- inline process pools -------------------------------------------------
import pickle  # noqa: E402
import concurrent.futures  # noqa: E402
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

# ---- run -------------------------------------------------------------------
os.chdir(repo)
sys.path.insert(0, repo)
import unittest  # noqa: E402
import importlib  # noqa: E402

inst = set()
import sqlglot.expressions.core as _core  # noqa: E402

_orig_init = _core.Expr.__init__


def _init(self, **args):
    inst.add(type(self))
    _orig_init(self, **args)


try:
    _core.Expr.__init__ = _init
except Exception:
    pass

# dialect components: record every Tokenizer / Parser / Generator / Dialect class instantiated
import sqlglot.tokens as _tokens  # noqa: E402
import sqlglot.parser as _parser  # noqa: E402
import sqlglot.generator as _generator  # noqa: E402
import sqlglot.dialects.dialect as _dialect  # noqa: E402


def _recording_new(base):
    orig = base.__dict__.get("__new__")

    def __new__(cls, *a, **k):
        inst.add(cls)
        if orig is not None:
            return orig(cls, *a, **k)
        return object.__new__(cls)

    base.__new__ = __new__


for _base in (_tokens.Tokenizer, _parser.Parser, _generator.Generator, _dialect.Dialect):
    _recording_new(_base)

# ---- fixture entries actually used ---------------------------------------------
fx = set()


def _rec(key):
    if key is not None:
        fx.add(key)


class TrackStr(str):
    """A fixture string that records when anything reads it."""

    def __new__(cls, value, key):
        o = str.__new__(cls, value)
        o.__dict__["_k"] = key
        return o

    def __getattribute__(self, name):
        if name not in ("__dict__", "__class__"):
            _rec(str.__getattribute__(self, "__dict__").get("_k"))
        return str.__getattribute__(self, name)


def _str_dunder(name):
    base = getattr(str, name)

    def f(self, *a):
        _rec(str.__getattribute__(self, "__dict__").get("_k"))
        return base(self, *a)

    f.__name__ = name
    return f


for _n in ("__getitem__", "__iter__", "__len__", "__contains__", "__eq__", "__ne__", "__lt__", "__le__",
           "__gt__", "__ge__", "__hash__", "__add__", "__mul__", "__rmul__", "__mod__", "__format__",
           "__str__", "__repr__", "__sizeof__"):
    setattr(TrackStr, _n, _str_dunder(_n))


def _radd(self, other):
    _rec(str.__getattribute__(self, "__dict__").get("_k"))
    return other + str(self)


TrackStr.__radd__ = _radd


def _newargs(self):
    _rec(str.__getattribute__(self, "__dict__").get("_k"))
    return (str.__str__(self), str.__getattribute__(self, "__dict__")["_k"])


TrackStr.__getnewargs__ = _newargs


class TrackDict(dict):
    def __init__(self, value=(), key=None):
        dict.__init__(self, value)
        if key is not None:
            self.__dict__["_k"] = key

    def __getattribute__(self, name):
        if name not in ("__dict__", "__class__"):
            _rec(dict.__getattribute__(self, "__dict__").get("_k"))
        return dict.__getattribute__(self, name)


def _dict_dunder(name):
    base = getattr(dict, name)

    def f(self, *a):
        _rec(dict.__getattribute__(self, "__dict__").get("_k"))
        return base(self, *a)

    f.__name__ = name
    return f


for _n in ("__getitem__", "__iter__", "__len__", "__contains__", "__eq__", "__ne__", "__repr__", "__or__"):
    setattr(TrackDict, _n, _dict_dunder(_n))
TrackDict.__hash__ = None

import tests.helpers as _helpers  # noqa: E402

_orig_pairs = _helpers.load_sql_fixture_pairs
_orig_fixtures = _helpers.load_sql_fixtures


def load_sql_fixture_pairs(filename):
    for i, (meta, sql, expected) in enumerate(_orig_pairs(filename)):
        yield TrackDict(meta, (filename, i, "m")), TrackStr(sql, (filename, i, "s")), TrackStr(expected, (filename, i, "e"))


def load_sql_fixtures(filename):
    for i, sql in enumerate(_orig_fixtures(filename)):
        yield TrackStr(sql, (filename, i, "f"))


_helpers.load_sql_fixture_pairs = load_sql_fixture_pairs
_helpers.load_sql_fixtures = load_sql_fixtures

path, clsname, meth = test_id.split("::")
outcome, msg = "pass", ""
c0 = time.process_time()
try:
    mod = importlib.import_module(path[:-3].replace("/", "."))
    cls = getattr(mod, clsname)
    suite = unittest.TestSuite([cls(meth)])
    result = unittest.TestResult()
    c1 = time.process_time()
    suite.run(result)
    cpu_test = time.process_time() - c1
    if result.failures or result.errors or result.unexpectedSuccesses:
        outcome = "fail"
        msg = ((result.failures + result.errors) or [("", "unexpected success")])[0][1][-800:]
    elif result.skipped:
        outcome = "skip"
except BaseException as e:  # noqa: B902
    outcome, msg, cpu_test = "fail", repr(e), 0.0
cpu_all = time.process_time() - c0

mon.set_events(TOOL, 0)
cov = {}  # lines run by module / class bodies (import time)
covf = {}  # lines run inside function bodies
for fn, line, isfunc in seen:
    (covf if isfunc else cov).setdefault(fn[len(prefix):].replace("\\", "/"), []).append(line)
for v in list(cov.values()) + list(covf.values()):
    v.sort()

with _real_open(out_path, "w") as f:
    json.dump(
        {
            "id": test_id,
            "out": outcome,
            "msg": msg,
            "cpu_all": round(cpu_all, 4),
            "cpu_test": round(cpu_test, 4),
            "cov": cov,
            "covf": covf,
            "opened": sorted(opened),
            "hopened": sorted(hopened),
            "fx": sorted(list(k) for k in fx),
            "inst": sorted(f"{c.__module__}:{c.__qualname__}" for c in inst),
        },
        f,
    )
