"""Dict-entry profile of the pinned commit -> maps/dictprof.bin

    python tools/profile_dict.py <sqlglot_root> <tests.json> <work_dir> <out> [jobs]

Every dict display assigned to a name directly in a class body under sqlglot/
(`FUNCTIONS = {...}`, `NUMERIC_ESCAPES = {...}`, `arg_types = {...}`) is created as a
dict that records reads:
  per test       which keys were read, and whether all values were read at once
                 (items(), values(), copy, **spread, ==, ...)
  outside tests  (imports, class setup) the same, kept apart as "global"
If a patch changes only the value of some entries of such a dict, and nothing outside
tests read those entries (no derived table can carry the change), the tests that can
see it are the tests that read one of those keys, or read all values.
"""

import ast
import json
import os
import pickle
import subprocess
import sys
import zlib
from concurrent.futures import ThreadPoolExecutor


class Rewrite(ast.NodeTransformer):
    """Wrap `NAME = {...}` in class bodies: __blast_dict__(id, {...}, [entry keys], [entry index])."""

    def __init__(self, rel):
        self.rel = rel
        self.classes = []
        self.funcs = 0

    def visit_ClassDef(self, node):
        self.classes.append(node.name)
        self.generic_visit(node)
        self.classes.pop()
        return node

    def _func(self, node):
        self.funcs += 1
        self.generic_visit(node)
        self.funcs -= 1
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _func

    def visit_Assign(self, node):
        self.generic_visit(node)
        if (self.classes and not self.funcs and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Dict)):
            did = f"{self.rel}:{'.'.join(self.classes)}.{node.targets[0].id}"
            idx = [i for i, k in enumerate(node.value.keys) if k is not None]
            keys = [node.value.keys[i] for i in idx]
            node.value = ast.copy_location(ast.Call(
                func=ast.Name(id="__blast_dict__", ctx=ast.Load()),
                args=[ast.Constant(did), node.value, ast.List(elts=keys, ctx=ast.Load()),
                      ast.Constant(tuple(idx))],
                keywords=[]), node.value)
        return node


def child(repo, job_path, out_path):
    import builtins
    import importlib.abc
    import importlib.machinery

    repo = os.path.realpath(repo)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cur = [None]
    GLOBAL = -1
    # did -> {"keys": {test: set(key repr)}, "all": set(test), "entries": [(entry index, key repr)]}
    prof = {}

    def k_of(k):
        try:
            return repr(k)
        except Exception:
            return "<unrepr>"

    class TDict(dict):
        __slots__ = ("_did",)

        def _key(self, k):
            e = prof[self._did]["keys"].setdefault(GLOBAL if cur[0] is None else cur[0], set())
            e.add(k_of(k))

        def _all(self):
            prof[self._did]["all"].add(GLOBAL if cur[0] is None else cur[0])

        def __getitem__(self, k):
            self._key(k)
            return dict.__getitem__(self, k)

        def get(self, k, *d):
            self._key(k)
            return dict.get(self, k, *d)

        def __contains__(self, k):
            self._key(k)
            return dict.__contains__(self, k)

        def pop(self, k, *d):
            self._key(k)
            return dict.pop(self, k, *d)

        def setdefault(self, k, *d):
            self._key(k)
            return dict.setdefault(self, k, *d)

        def __setitem__(self, k, v):
            self._key(k)
            return dict.__setitem__(self, k, v)

        def __delitem__(self, k):
            self._key(k)
            return dict.__delitem__(self, k)

    def _whole(name):
        base = getattr(dict, name)

        def f(self, *a, **kw):
            self._all()
            return base(self, *a, **kw)

        f.__name__ = name
        return f

    # anything that can expose values (or the key set) counts as reading every entry
    for name in ("keys", "values", "items", "copy", "__iter__", "__len__", "__eq__", "__ne__", "__repr__",
                 "__or__", "__ror__", "__ior__", "update", "popitem", "clear", "__reversed__", "__reduce_ex__",
                 "__sizeof__", "fromkeys"):
        if hasattr(dict, name):
            setattr(TDict, name, _whole(name))
    TDict.__hash__ = None

    def blast_dict(did, d, keys, idx):
        t = TDict(d)
        t._did = did
        if did not in prof:
            prof[did] = {"keys": {}, "all": set(), "entries": [(i, k_of(k)) for i, k in zip(idx, keys)]}
        return t

    builtins.__blast_dict__ = blast_dict

    class Loader(importlib.machinery.SourceFileLoader):
        def source_to_code(self, data, path, *, _optimize=-1):
            rel = os.path.relpath(os.path.realpath(path), repo).replace("\\", "/")
            tree = Rewrite(rel).visit(ast.parse(data, filename=path))
            ast.fix_missing_locations(tree)
            return compile(tree, path, "exec", dont_inherit=True)

        def path_stats(self, path):
            raise OSError("no bytecode")

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
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
    unsure = set()
    reported = set()

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
    out = {}
    for did, e in prof.items():
        out[did] = {"keys": {t: sorted(ks) for t, ks in e["keys"].items()}, "all": sorted(e["all"]),
                    "entries": e["entries"]}
    with open(out_path, "wb") as f:
        pickle.dump({"prof": out, "unsure": sorted(unsure)}, f)
    os._exit(0)


def main():
    repo, tests_json, work, out = sys.argv[1:5]
    jobs = int(sys.argv[5]) if len(sys.argv) > 5 else 8
    os.makedirs(work, exist_ok=True)
    tests = [t["id"] for t in json.load(open(tests_json, encoding="utf-8"))["tests"]]
    # one fresh process per test: caches built while a test runs (memoized results, copied
    # tables) must be charged to every test that uses them, not only to the first one
    byfile = {t: [t] for t in tests}
    duck = os.path.join(os.path.dirname(os.path.abspath(out)), "duck.pkl")

    def run(item):
        k, (f, ids) = item
        res = os.path.join(work, f"{k}.pkl")
        if os.path.exists(res):
            return
        jp = os.path.join(work, f"{k}.json")
        json.dump({"tests": ids, "all_tests": tests, "duck": duck if os.path.exists(duck) else None}, open(jp, "w"))
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--child", repo, jp, res],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        err = p.stderr.decode(errors="replace")
        for line in err.splitlines():
            if line.startswith("BASELINE FAIL"):
                print(line, file=sys.stderr)
        if not os.path.exists(res):
            print("NO RESULT", f, err[-800:], file=sys.stderr)

    with ThreadPoolExecutor(jobs) as pool:
        list(pool.map(run, list(enumerate(byfile.items()))))

    # merge. did -> {"entries": [(index, key)], "global_all": bool, "global_keys": set,
    #                "all": [tests], "keys": {key: [tests]}}
    merged = {}
    for k in range(len(byfile)):
        r = pickle.load(open(os.path.join(work, f"{k}.pkl"), "rb"))
        unsure = r["unsure"]
        for did, e in r["prof"].items():
            m = merged.setdefault(did, {"entries": e["entries"], "global_all": False, "global_keys": set(),
                                        "all": set(), "keys": {}})
            for t in e["all"]:
                if t == -1:
                    m["global_all"] = True
                else:
                    m["all"].add(t)
            for t, ks in e["keys"].items():
                if t == -1:
                    m["global_keys"].update(ks)
                else:
                    for key in ks:
                        m["keys"].setdefault(key, set()).add(t)
            m["all"].update(unsure)  # a test whose profile is incomplete may read anything
    for m in merged.values():
        m["all"] = sorted(m["all"])
        m["keys"] = {key: sorted(ts) for key, ts in m["keys"].items()}
    blob = zlib.compress(pickle.dumps(merged, protocol=4), 9)
    with open(out, "wb") as f:
        f.write(blob)
    clean = sum(1 for m in merged.values() if not m["global_all"])
    print(f"dicts {len(merged)}  without import-time whole reads {clean}  bytes={len(blob)}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(*sys.argv[2:5])
    else:
        main()
