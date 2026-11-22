"""Run sqlglot tests against a patched copy of the pinned checkout, in a child process.

The child never touches the checkout on disk: patched Python modules are served by an
import hook, and patched data files (fixtures) are served by redirecting open().

    job = {"sg": "/app/sg", "patched": {rel: text or None}, "tests": [test ids],
           "duck": path to the DuckDB result cache or None}

The child writes one line per finished test to its result stream:
    "P <test id>"  passed (or skipped)        "F <test id>"  failed / errored
and "S <test id>" right before a test starts, so the parent knows which test hung.
"""

import os
import sys


# ----------------------------------------------------------------------------- child side
def child_main(job, out):
    """Runs in the child. `out` is a binary file object for result lines."""
    import builtins
    import io
    import importlib.abc
    import importlib.machinery
    import importlib.util
    import pickle

    sg = os.path.realpath(job["sg"])
    patched = job["patched"]
    sys.dont_write_bytecode = True
    os.environ["SKIP_INTEGRATION"] = "0"
    os.chdir(sg)
    if sys.path[0] != sg:
        sys.path.insert(0, sg)

    def emit(kind, tid):
        out.write(f"{kind} {tid}\n".encode())
        out.flush()

    # -- patched python modules ------------------------------------------------
    mods = {}
    for rel, text in patched.items():
        if rel.endswith(".py"):
            name = rel[:-3].replace("/", ".")
            pkg = name.endswith(".__init__")
            if pkg:
                name = name[: -len(".__init__")]
            mods[name] = (os.path.join(sg, rel), text, pkg)

    class Loader(importlib.machinery.SourceFileLoader):
        def get_data(self, path):
            ent = mods.get(self.name)
            if ent is not None and os.path.realpath(path) == os.path.realpath(ent[0]):
                return ent[1].encode("utf-8", "surrogateescape")
            return super().get_data(path)

        def path_stats(self, path):
            raise OSError("no bytecode for patched modules")

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            ent = mods.get(fullname)
            if ent is None:
                return None
            real, text, pkg = ent
            if text is None:
                raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
            return importlib.util.spec_from_file_location(
                fullname,
                real,
                loader=Loader(fullname, real),
                submodule_search_locations=[os.path.dirname(real)] if pkg else None,
            )

    sys.meta_path.insert(0, Finder())

    # -- patched data files ------------------------------------------------------
    data = {}
    for rel, text in patched.items():
        if not rel.endswith(".py"):
            data[os.path.realpath(os.path.join(sg, rel))] = text
    if data:
        ovdir = os.path.join("/tmp" if os.name != "nt" else os.environ.get("TEMP", "."), f"blast_ov_{os.getpid()}")
        redirect = {}
        for real, text in data.items():
            if text is None:
                redirect[real] = os.path.join(ovdir, "__deleted__", os.path.basename(real))
                continue
            dst = os.path.join(ovdir, os.path.relpath(real, sg))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(text.encode("utf-8", "surrogateescape"))
            redirect[real] = dst
        real_open = io.open

        def _open(file, *a, **k):
            if isinstance(file, (str, bytes, os.PathLike)):
                try:
                    p = os.path.realpath(os.fsdecode(file))
                except Exception:
                    p = None
                if p in redirect:
                    file = redirect[p]
            return real_open(file, *a, **k)

        real_open_code = io.open_code

        def _open_code(path):
            try:
                p = os.path.realpath(os.fsdecode(path))
            except Exception:
                p = None
            return real_open_code(redirect.get(p, path))

        builtins.open = _open
        io.open = _open
        io.open_code = _open_code

    # -- process pools run inline -----------------------------------------------
    import concurrent.futures
    from concurrent.futures import Future

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

    import unittest
    import importlib

    # -- group tests by class, in the order given --------------------------------
    order = []
    groups = {}
    for tid in job["tests"]:
        path, cls, meth = tid.split("::")
        k = (path, cls)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(meth)

    duck = None
    if job.get("duck") and any(p == "tests/test_executor.py" for p, _ in order):
        try:
            with open(job["duck"], "rb") as f:
                duck = pickle.load(f)
        except Exception:
            duck = None

    after_fail = set(job.get("after_fail") or ())
    failed_any = [False]

    for path, clsname in order:
        meths = groups[(path, clsname)]
        if failed_any[0]:
            meths = [m for m in meths if f"{path}::{clsname}::{m}" in after_fail]
            if not meths:
                continue
        tids = [f"{path}::{clsname}::{m}" for m in meths]
        try:
            mod = importlib.import_module(path[:-3].replace("/", "."))
            cls = getattr(mod, clsname)
        except BaseException:  # noqa: B902  the file cannot be imported: all its tests fail
            for tid in tids:
                emit("F", tid)
            continue
        if duck is not None and path == "tests/test_executor.py":
            _install_duck_cache(mod, cls, duck)

        bad = set()
        class_error = [False]

        class Result(unittest.TestResult):
            def startTest(self, test):
                super().startTest(test)
                emit("S", f"{path}::{clsname}::{test._testMethodName}")

            def _mark(self, test):
                m = getattr(test, "_testMethodName", None)
                if isinstance(test, cls) and m:
                    bad.add(m)
                else:
                    class_error[0] = True

            def addError(self, test, err):
                super().addError(test, err)
                self._mark(test)

            def addFailure(self, test, err):
                super().addFailure(test, err)
                self._mark(test)

            def addSubTest(self, test, subtest, err):
                super().addSubTest(test, subtest, err)
                if err is not None:
                    self._mark(test)

            def addUnexpectedSuccess(self, test):
                super().addUnexpectedSuccess(test)
                self._mark(test)

            def stopTest(self, test):
                super().stopTest(test)
                m = test._testMethodName
                emit("F" if m in bad else "P", f"{path}::{clsname}::{m}")
                if m in bad:
                    failed_any[0] = True


        tests_ = []
        for m in meths:
            t = cls(m)
            orig = t.run

            def run_gated(result=None, _t=t, _orig=orig, _m=m):
                if failed_any[0] and f"{path}::{clsname}::{_m}" not in after_fail:
                    return result  # not run: reported as nothing (unverified)
                return _orig(result)

            t.run = run_gated
            tests_.append(t)
        suite = unittest.TestSuite(tests_)
        result = Result()
        try:
            suite.run(result)
        except BaseException:  # noqa: B902
            class_error[0] = True
        if class_error[0]:
            # setUpClass / tearDownClass broke: every test of the class counts as failed
            for tid in tids:
                emit("F", tid)
    emit("D", "-")


def _install_duck_cache(mod, cls, duck):
    """Serve DuckDB reference results recorded on the pinned commit when the SQL is unchanged."""
    transpile = mod.transpile

    class Cached:
        def __init__(self, frame):
            self.frame = frame

        def fetchdf(self):
            return self.frame.copy()

        df = fetchdf

    class Conn:
        def __init__(self, real, name):
            self._real = real
            self._name = name

        def execute(self, sql, *a, **k):
            if not a and not k:
                f = duck.get((self._name, sql))
                if f is not None:
                    return Cached(f)
            return self._real.execute(sql, *a, **k)

        def close(self):
            return self._real.close()

        def __getattr__(self, item):
            return getattr(self._real, item)

    orig_setup = cls.__dict__["setUpClass"].__func__

    def setUpClass(klass):
        orig_setup(klass)
        klass.tpch_conn = Conn(klass.tpch_conn, "tpch")
        klass.tpcds_conn = Conn(klass.tpcds_conn, "tpcds")

    cls.setUpClass = classmethod(setUpClass)
    del transpile


# ----------------------------------------------------------------------------- parent side
def run_job(job, timeout, python=sys.executable):
    """Run one job in a child. Returns {test id: True (failed) / False (passed)}.
    Tests that did not finish are missing from the result (treat them as unverified),
    except a test that was running when the child hung or died: that one is a failure."""
    import json
    import subprocess
    import time

    res = {}
    current = None
    me = os.path.abspath(__file__)
    p = subprocess.Popen(
        [python, "-I", me],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    p.stdin.write(json.dumps(job).encode())
    p.stdin.close()
    deadline = time.monotonic() + timeout
    import selectors

    sel = None
    if os.name != "nt":
        sel = selectors.DefaultSelector()
        sel.register(p.stdout, selectors.EVENT_READ)
    done = False
    reader = None
    buf = b""
    while True:
        if sel is not None:
            left = deadline - time.monotonic()
            if left <= 0 or not sel.select(left):
                break
            chunk = os.read(p.stdout.fileno(), 65536)
        else:
            # Windows: no select() on pipes; read in a thread and wait with a deadline
            if reader is None:
                import queue
                import threading

                q = queue.Queue()

                def _pump():
                    while True:
                        c = p.stdout.read1(65536) if hasattr(p.stdout, "read1") else p.stdout.read(65536)
                        q.put(c)
                        if not c:
                            return

                reader = threading.Thread(target=_pump, daemon=True)
                reader.start()
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                chunk = q.get(timeout=left)
            except Exception:
                break
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            kind, _, tid = line.decode().partition(" ")
            if kind == "S":
                current = tid
            elif kind in ("P", "F"):
                res[tid] = kind == "F"
                current = None
            elif kind == "D":
                done = True
        if done:
            break
    if p.poll() is None:
        p.kill()
    p.wait()
    if current is not None and current not in res:
        res[current] = True  # hung or crashed inside this test
    return res


class ForkServer:
    """POSIX only. One process imports the heavy libraries once, then forks a child per job.

    Protocol on the server's stdin:   {"id": n, "job": {...}}   or   {"kill": n}
    and on its stdout:                "<id> <S|P|F|D> <test id>"  and  "<id> X -" when a child exits.
    """

    def __init__(self, heavy, python=sys.executable):
        import subprocess

        self.p = subprocess.Popen(
            [python, "-I", os.path.abspath(__file__), "--zygote", "1" if heavy else "0"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.buf = b""

    def send(self, msg):
        import json

        self.p.stdin.write((json.dumps(msg) + "\n").encode())
        self.p.stdin.flush()

    def read(self, timeout):
        import select

        r, _, _ = select.select([self.p.stdout], [], [], timeout)
        out = []
        if r:
            chunk = os.read(self.p.stdout.fileno(), 1 << 16)
            if not chunk:
                return None
            self.buf += chunk
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                jid, kind, tid = line.decode().split(" ", 2)
                out.append((int(jid), kind, tid))
        return out

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(5)
        except Exception:
            self.p.kill()


def zygote_main(heavy):
    import json
    import select
    import signal

    # what every child needs anyway
    import pickle  # noqa: F401
    import unittest  # noqa: F401
    import concurrent.futures  # noqa: F401
    import importlib.abc  # noqa: F401
    import importlib.machinery  # noqa: F401
    import importlib.util  # noqa: F401
    import logging  # noqa: F401
    import tempfile  # noqa: F401
    import datetime  # noqa: F401
    import decimal  # noqa: F401
    import typing  # noqa: F401
    import re  # noqa: F401

    if heavy:
        try:
            import numpy  # noqa: F401
            import pandas  # noqa: F401
            import pandas.testing  # noqa: F401
            import duckdb  # noqa: F401
        except Exception:
            pass

    out_fd = os.dup(1)
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.dup2(null, 2)
    children = {}
    pids = {}
    buf = b""
    eof = False
    while not eof or children:
        if not eof:
            r, _, _ = select.select([0], [], [], 0.02)
            if r:
                chunk = os.read(0, 1 << 16)
                if not chunk:
                    eof = True
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    msg = json.loads(line)
                    if "kill" in msg:
                        pid = pids.get(msg["kill"])
                        if pid:
                            try:
                                os.kill(pid, signal.SIGKILL)
                            except OSError:
                                pass
                        continue
                    jid = msg["id"]
                    pid = os.fork()
                    if pid == 0:
                        try:
                            prefix = f"{jid} ".encode()

                            class Out:
                                def write(self, b):
                                    os.write(out_fd, prefix + b)

                                def flush(self):
                                    pass

                            sys.stdout = open(os.devnull, "w")
                            sys.stderr = sys.stdout
                            child_main(msg["job"], Out())
                        finally:
                            os._exit(0)
                    children[pid] = jid
                    pids[jid] = pid
        else:
            select.select([], [], [], 0.02)
        while children:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                children.clear()
                break
            if pid == 0:
                break
            jid = children.pop(pid, None)
            if jid is not None:
                pids.pop(jid, None)
                os.write(out_fd, f"{jid} X -\n".encode())
    os._exit(0)


if __name__ == "__main__" and len(sys.argv) > 2 and sys.argv[1] == "--zygote":
    zygote_main(sys.argv[2] == "1")

if __name__ == "__main__":
    import json

    _job = json.loads(sys.stdin.buffer.read())
    _out = os.fdopen(os.dup(1), "wb")
    _null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(_null, 1)
    os.dup2(_null, 2)
    sys.stdout = open(os.devnull, "w")
    sys.stderr = sys.stdout
    try:
        child_main(_job, _out)
    finally:
        _out.flush()
        os._exit(0)
