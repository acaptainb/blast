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


PURE_CALLS = {"isinstance", "len", "hasattr", "type", "issubclass", "bool", "callable", "__blast_cmp__", "__blast_bool__"}


def _pure(e):
    """No side effects when evaluated (so it may be evaluated where the original skips it).
    Calls are allowed only to a few builtins; every other call may change state."""
    for x in ast.walk(e):
        if isinstance(x, ast.Call):
            if not (isinstance(x.func, ast.Name) and x.func.id in PURE_CALLS):
                return False
        elif isinstance(x, (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await, ast.Lambda,
                            ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            return False
    return True


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

    # --- and / or in a boolean context (if / while / ternary / assert / not / comprehension if)
    def _bool_ok(self, node):
        if not (self.depth and isinstance(node, ast.BoolOp) and len(node.values) == 2):
            return False
        for x in ast.walk(node):
            if isinstance(x, (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await, ast.Starred)):
                return False
            if isinstance(x, ast.Name) and x.id == "super":
                return False
        return True

    def _wrap_bool(self, node):
        if not self._bool_ok(node):
            return node
        sid = len(self.sites)
        self.sites.append((self.rel, node.lineno, node.col_offset, "BOOL_" + type(node.op).__name__))
        lam = lambda e: ast.Lambda(  # noqa: E731
            args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]), body=e
        )
        call = ast.Call(
            func=ast.Name(id="__blast_bool__", ctx=ast.Load()),
            args=[ast.Constant(sid), lam(node.values[0]), lam(node.values[1]), ast.Constant(isinstance(node.op, ast.And)),
                  ast.Constant(_pure(node.values[1]))],
            keywords=[],
        )
        return ast.copy_location(call, node)

    def _test_holder(self, node):
        self.generic_visit(node)
        node.test = self._wrap_bool(node.test)
        return node

    visit_If = visit_While = visit_IfExp = visit_Assert = _test_holder

    def visit_UnaryOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Not):
            node.operand = self._wrap_bool(node.operand)
        return node

    def visit_comprehension(self, node):
        self.generic_visit(node)
        node.ifs = [self._wrap_bool(x) for x in node.ifs]
        return node

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
                if not isinstance(r, bool):
                    e = None  # overloaded operator: never claim the swap is invisible
                elif op == "Eq":
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
                # an overloaded operator (e.g. sqlglot expressions build an LT node) returns
                # something else than a bool: swapping it changes the result outright
                e = a == b
                if not isinstance(r, bool) or not isinstance(e, bool) or e:
                    note(bound, sid)
        except Exception:
            note(eqis, sid)
            note(bound, sid)
        return r

    builtins.__blast_cmp__ = blast_cmp
    andor = {}

    def blast_bool(sid, fa, fb, is_and, pure):
        """Original semantics of `a and b` / `a or b`; notes when the other operator would
        give a different truth value (evaluating b where the original short-circuits)."""
        a = fa()
        if bool(a) == (not is_and):
            # short-circuit: `and` with falsy a / `or` with truthy a. The swapped operator
            # would also evaluate b: its truth decides whether the outcome changes. Only a
            # side-effect-free b may be evaluated here; otherwise the swap counts as visible.
            if not pure:
                note(andor, sid)
                return a
            try:
                bb = bool(fb())
                if bb == is_and:
                    note(andor, sid)
            except Exception:
                note(andor, sid)
            return a
        b = fb()
        try:
            # `and` with truthy a -> bool(b); `or` would give True.  `or` with falsy a -> bool(b);
            # `and` would give False. They differ when bool(b) disagrees.
            if bool(b) != is_and:
                note(andor, sid)
        except Exception:
            note(andor, sid)
        return b

    builtins.__blast_bool__ = blast_bool
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
    reported = set()
    _write = Out.write

    def write(self, b):
        kind, _, tid = b.decode().strip().partition(" ")
        if kind in ("P", "F"):
            reported.add(tid)
        _write(self, b)

    Out.write = write
    # run every test even after a failure: an unprofiled test must never look "clean"
    runner.child_main({"sg": repo, "patched": {}, "tests": job["tests"], "after_fail": job["tests"], "duck": job.get("duck")}, Out())
    for tid in job["tests"]:
        if tid not in reported:
            sys.__stderr__.write(f"BASELINE FAIL {tid} (did not report)\n")
            unsure.add(index[tid])
    with open(out_path, "wb") as f:
        pickle.dump({"sites": sites, "eqis": eqis, "bound": bound, "andor": andor, "unsure": sorted(unsure)}, f)
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
        else:
            print("done", f, file=sys.stderr, flush=True)

    heavy = ("test_max_nodes", "tpcds", "tpch", "test_generator_memory", "test_scope")
    items = sorted(enumerate(byfile.items()), key=lambda kv: not any(h in kv[1][0] for h in heavy))
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
        instrumented.update((rel, line, col, "B" if op.startswith("BOOL_") else "C") for rel, line, col, op in sites)
        if r.get("unsure"):
            for sid in range(len(sites)):
                for kind in ("eqis", "bound", "andor"):
                    r.setdefault(kind, {}).setdefault(sid, set()).update(r["unsure"])
        for kind in ("eqis", "bound", "andor"):
            for sid, tests_ in r.get(kind, {}).items():
                rel, line, col, op = sites[sid]
                key = (rel, line, col, "B" if op.startswith("BOOL_") else "C")
                e = prof.setdefault(key, {"op": op, "eqis": set(), "bound": set(), "andor": set()})
                e[kind] |= tests_
                seen_any += 1
    for e in prof.values():
        for kind in ("eqis", "bound", "andor"):
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
