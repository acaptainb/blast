"""Comparison profile of the pinned commit -> maps/cmpprof.bin

    python tools/profile_cmp.py <sqlglot_root> <tests.json> <work_dir> <out> [jobs]

Every single-operator comparison inside a function body of sqlglot/ is rewritten to
record, for each test that evaluates it:
  eqis   a case where (a == b) and (a is b) disagree  -> `is` and `==` can differ there
  bound  a case where a == b                           -> `<` and `<=` can differ there
If a patch only swaps such an operator, the tests that can observe the swap are the
tests that saw such a case (nothing else in those tests can change: up to that
comparison the run is identical, and the comparison gives the same answer).
Cases seen outside any test (imports, class setup) count for every test.
"""

import ast
import json
import os
import pickle
import subprocess
import sys
import zlib
from concurrent.futures import ThreadPoolExecutor

OPS = (ast.Is, ast.IsNot, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)


class Rewrite(ast.NodeTransformer):
    def __init__(self, rel, sites):
        self.rel = rel
        self.sites = sites
        self.depth = 0

    def _func(self, node):
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _func

    def visit_Compare(self, node):
        self.generic_visit(node)
        if self.depth and len(node.ops) == 1 and isinstance(node.ops[0], OPS):
            sid = len(self.sites)
            self.sites.append((self.rel, node.lineno, node.col_offset, type(node.ops[0]).__name__))
            call = ast.Call(
                func=ast.Name(id="__blast_cmp__", ctx=ast.Load()),
                args=[ast.Constant(sid), node.left, node.comparators[0]],
                keywords=[],
            )
            return ast.copy_location(call, node)
        return node


def child(repo, job_path, out_path):
    import builtins
    import importlib.abc
    import importlib.machinery
    import importlib.util
    import operator

    repo = os.path.realpath(repo)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sites = []
    fn = {"Is": operator.is_, "IsNot": operator.is_not, "Eq": operator.eq, "NotEq": operator.ne,
          "Lt": operator.lt, "LtE": operator.le, "Gt": operator.gt, "GtE": operator.ge}
    site_op = []
    eqis = {}
    bound = {}
    cur = [None]
    GLOBAL = -1

    def note(d, sid):
        s = d.get(sid)
        if s is None:
            s = d[sid] = set()
        s.add(GLOBAL if cur[0] is None else cur[0])

    def blast_cmp(sid, a, b):
        op = site_op[sid]
        r = fn[op](a, b)
        try:
            if op in ("Is", "IsNot", "Eq", "NotEq"):
                ident = a is b
                if op == "Eq":
                    e = r
                elif isinstance(a, EXPR) or isinstance(b, EXPR):
                    # comparing trees is expensive; reflexive when identical, otherwise assume
                    # they may compare equal (conservative: the swap counts as visible)
                    e = ident if ident else None
                else:
                    e = a == b
                if not isinstance(e, bool) or e != ident:
                    note(eqis, sid)
            else:
                e = a == b
                if not isinstance(e, bool) or e:
                    note(bound, sid)
        except Exception:
            note(eqis, sid)
            note(bound, sid)
        return r

    builtins.__blast_cmp__ = blast_cmp
    EXPR = ()

    def _late_expr():
        nonlocal EXPR
        try:
            EXPR = (sys.modules["sqlglot.expressions.core"].Expr,)
        except Exception:
            pass

    class Loader(importlib.machinery.SourceFileLoader):
        def source_to_code(self, data, path, *, _optimize=-1):
            rel = os.path.relpath(os.path.realpath(path), repo).replace("\\", "/")
            tree = ast.parse(data, filename=path)
            start = len(sites)
            tree = Rewrite(rel, sites).visit(tree)
            for s in sites[start:]:
                site_op.append(s[3])
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

    unsure = set()
    job = json.load(open(job_path))
    index = {t: i for i, t in enumerate(job["all_tests"])}

    class Out:
        def write(self, b):
            kind, _, tid = b.decode().strip().partition(" ")
            if kind == "S":
                cur[0] = index[tid]
            elif kind in ("P", "F"):
                cur[0] = None
                if kind == "F":
                    # this test's profile is incomplete: it may see any comparison
                    sys.__stderr__.write(f"BASELINE FAIL {tid}\n")
                    unsure.add(index[tid])

        def flush(self):
            pass

    import runner
    import sqlglot.expressions.core  # noqa: F401  (instrumented import)

    _late_expr()

    real = os.dup(1)
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    runner.child_main({"sg": repo, "patched": {}, "tests": job["tests"], "duck": job.get("duck")}, Out())
    with open(out_path, "wb") as f:
        pickle.dump({"sites": sites, "eqis": eqis, "bound": bound, "unsure": sorted(unsure)}, f)
    os._exit(0)


def main():
    repo, tests_json, work, out = sys.argv[1:5]
    jobs = int(sys.argv[5]) if len(sys.argv) > 5 else 8
    os.makedirs(work, exist_ok=True)
    tests = [t["id"] for t in json.load(open(tests_json, encoding="utf-8"))["tests"]]
    byfile = {}
    for t in tests:
        byfile.setdefault(t.split("::")[0], []).append(t)
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
        else:
            print("done", f, file=sys.stderr, flush=True)

    items = sorted(enumerate(byfile.items()), key=lambda kv: -len(kv[1][1]) if "optimizer" in kv[1][0] or "parser" in kv[1][0] or "executor" in kv[1][0] else 0)
    with ThreadPoolExecutor(jobs) as pool:
        list(pool.map(run, items))

    # merge: site key -> {"eqis": set(test idx) | -1 for all, "bound": ...}
    prof = {}
    instrumented = set()
    seen_any = 0
    for k in range(len(byfile)):
        res = os.path.join(work, f"{k}.pkl")
        if not os.path.exists(res):
            raise SystemExit(f"missing profile part {k}")
        r = pickle.load(open(res, "rb"))
        sites = r["sites"]
        instrumented.update((rel, line, col) for rel, line, col, _op in sites)
        if r.get("unsure"):
            for sid in range(len(sites)):
                for kind in ("eqis", "bound"):
                    r[kind].setdefault(sid, set()).update(r["unsure"])
        for kind in ("eqis", "bound"):
            for sid, tests_ in r[kind].items():
                rel, line, col, op = sites[sid]
                key = (rel, line, col)
                e = prof.setdefault(key, {"op": op, "eqis": set(), "bound": set()})
                e[kind] |= tests_
                seen_any += 1
    for e in prof.values():
        for kind in ("eqis", "bound"):
            e[kind] = -1 if -1 in e[kind] else sorted(e[kind])
    blob = zlib.compress(pickle.dumps({"cases": prof, "sites": instrumented}, protocol=4), 9)
    with open(out, "wb") as f:
        f.write(blob)
    print(f"instrumented sites: {len(instrumented)}  with recorded cases: {len(prof)}  bytes={len(blob)}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(*sys.argv[2:5])
    else:
        main()
