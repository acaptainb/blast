"""Class-constant read profile of the pinned commit -> maps/attrprof.bin

    python tools/profile_attr.py <sqlglot_root> <tests.json> <work_dir> <out> [jobs]

In every function body of sqlglot/ and tests/, reads `x.NAME` and `getattr(x, "NAME")` of an
UPPER_CASE name are rewritten to record which class provides the value (the first class
in the MRO whose __dict__ has NAME), per test; reads outside tests (imports, metaclasses,
class setup) are kept apart as "global". Enum members are not recorded. Each test runs in
its own process. Also records every identifier that appears as a string literal in
sqlglot/ (such a name may be read dynamically, e.g. klass.__dict__.get("NAME")).
"""

import ast
import json
import os
import pickle
import re
import subprocess
import sys
import zlib
from concurrent.futures import ThreadPoolExecutor

CONST = re.compile(r"^[A-Z][A-Z0-9_]*$")


class Rewrite(ast.NodeTransformer):
    def __init__(self):
        self.funcs = 0

    def _func(self, node):
        self.funcs += 1
        self.generic_visit(node)
        self.funcs -= 1
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _func

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if self.funcs and isinstance(node.ctx, ast.Load) and CONST.match(node.attr):
            return ast.copy_location(ast.Call(func=ast.Name(id="__blast_attr__", ctx=ast.Load()),
                                              args=[node.value, ast.Constant(node.attr)], keywords=[]), node)
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        if (self.funcs and isinstance(node.func, ast.Name) and node.func.id in ("getattr", "hasattr")
                and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str) and CONST.match(node.args[1].value)):
            return ast.copy_location(ast.Call(func=ast.Name(id="__blast_getattr__", ctx=ast.Load()),
                                              args=[ast.Constant(node.func.id)] + node.args, keywords=[]), node)
        return node


def child(repo, job_path, out_path):
    import builtins
    import enum
    import importlib.abc
    import importlib.machinery

    repo = os.path.realpath(repo)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cur = [None]
    reads = {}  # (owner key, NAME) -> set of test index (-1: outside tests)

    def note(obj, name):
        try:
            mro = obj.__mro__ if isinstance(obj, type) else type(obj).__mro__
        except Exception:
            return
        if not isinstance(obj, type):
            try:
                if name in obj.__dict__:
                    return  # an instance attribute
            except Exception:
                pass
        for k in mro:
            try:
                if name in k.__dict__:
                    if isinstance(k, enum.EnumMeta) and issubclass(k, enum.Enum):
                        return
                    key = (f"{k.__module__}:{k.__qualname__}", name)
                    reads.setdefault(key, set()).add(-1 if cur[0] is None else cur[0])
                    return
            except Exception:
                return

    def blast_attr(obj, name):
        note(obj, name)
        return getattr(obj, name)

    _missing = object()

    def blast_getattr(fn, obj, name, default=_missing):
        note(obj, name)
        if fn == "hasattr":
            return hasattr(obj, name)
        return getattr(obj, name) if default is _missing else getattr(obj, name, default)

    builtins.__blast_attr__ = blast_attr
    builtins.__blast_getattr__ = blast_getattr

    class Loader(importlib.machinery.SourceFileLoader):
        def source_to_code(self, data, path, *, _optimize=-1):
            tree = Rewrite().visit(ast.parse(data, filename=path))
            ast.fix_missing_locations(tree)
            return compile(tree, path, "exec", dont_inherit=True)

        def path_stats(self, path):
            raise OSError("no bytecode")

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            top = fullname.split(".")[0]
            if top not in ("sqlglot", "tests"):
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, [repo] if "." not in fullname else path)
            if spec is None or not (spec.origin or "").endswith(".py"):
                return spec
            spec.loader = Loader(fullname, spec.origin)
            return spec

    sys.meta_path.insert(0, Finder())
    sys.dont_write_bytecode = True

    job = json.load(open(job_path))
    index = {t: i for i, t in enumerate(job["all_tests"])}
    unsure, reported = set(), set()

    class Out:
        def write(self, b):
            kind, _, tid = b.decode().strip().partition(" ")
            if kind == "S":
                cur[0] = index[tid]
            elif kind in ("P", "F"):
                cur[0] = None
                reported.add(tid)
                if kind == "F":
                    sys.__stderr__.write(f"BASELINE FAIL {tid}\n")
                    unsure.add(index[tid])

        def flush(self):
            pass

    import runner

    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    runner.child_main({"sg": repo, "patched": {}, "tests": job["tests"], "after_fail": job["tests"],
                       "duck": job.get("duck")}, Out())
    for tid in job["tests"]:
        if tid not in reported:
            sys.__stderr__.write(f"BASELINE FAIL {tid} (did not report)\n")
            unsure.add(index[tid])
    with open(out_path, "wb") as f:
        pickle.dump({"reads": {k: sorted(v) for k, v in reads.items()}, "unsure": sorted(unsure)}, f)
    os._exit(0)


def main():
    repo, tests_json, work, out = sys.argv[1:5]
    jobs = int(sys.argv[5]) if len(sys.argv) > 5 else 8
    os.makedirs(work, exist_ok=True)
    tests = [t["id"] for t in json.load(open(tests_json, encoding="utf-8"))["tests"]]
    duck = os.path.join(os.path.dirname(os.path.abspath(out)), "duck.pkl")
    heavy = ("test_max_nodes", "tpcds", "tpch", "test_generator_memory", "test_scope")
    items = sorted(enumerate(tests), key=lambda kv: not any(h in kv[1] for h in heavy))

    def run(item):
        k, tid = item
        res = os.path.join(work, f"{k}.pkl")
        if os.path.exists(res):
            return
        jp = os.path.join(work, f"{k}.json")
        json.dump({"tests": [tid], "all_tests": tests, "duck": duck if os.path.exists(duck) else None}, open(jp, "w"))
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--child", repo, jp, res],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for line in p.stderr.decode(errors="replace").splitlines():
            if line.startswith("BASELINE FAIL"):
                print(line, file=sys.stderr)
        if not os.path.exists(res):
            print("NO RESULT", tid, p.stderr.decode(errors="replace")[-600:], file=sys.stderr)

    with ThreadPoolExecutor(jobs) as pool:
        list(pool.map(run, items))

    merged = {}  # (owner, NAME) -> {"global": bool, "tests": set}
    unsure_all = set()
    for k in range(len(tests)):
        p = os.path.join(work, f"{k}.pkl")
        if not os.path.exists(p):
            unsure_all.add(k)
            continue
        r = pickle.load(open(p, "rb"))
        unsure_all.update(r["unsure"])
        for key, ts in r["reads"].items():
            m = merged.setdefault(key, {"global": False, "tests": set()})
            for t in ts:
                if t == -1:
                    m["global"] = True
                else:
                    m["tests"].add(t)
    strings = set()
    for root, _d, files in os.walk(os.path.join(repo, "sqlglot")):
        for fn in files:
            if fn.endswith(".py"):
                for node in ast.walk(ast.parse(open(os.path.join(root, fn), "rb").read())):
                    if isinstance(node, ast.Constant) and isinstance(node.value, str) and CONST.match(node.value):
                        strings.add(node.value)
    data = {"reads": {k: {"global": m["global"], "tests": sorted(m["tests"])} for k, m in merged.items()},
            "unsure": sorted(unsure_all), "strings": strings}
    blob = zlib.compress(pickle.dumps(data, protocol=4), 9)
    with open(out, "wb") as f:
        f.write(blob)
    print(f"class constants read {len(merged)}  global {sum(m['global'] for m in merged.values())}  "
          f"unsure tests {len(unsure_all)}  string names {len(strings)}  bytes={len(blob)}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(*sys.argv[2:5])
    else:
        main()
