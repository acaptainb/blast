"""Synthetic practice-like benchmark (development tool, not used at run time).

    python tools/bench.py gen   <sqlglot_root> <maps> <dir> <n> [seed]
    python tools/bench.py label <sqlglot_root> <maps> <dir> [jobs]
    python tools/bench.py eval  <sqlglot_root> <maps> <dir>

gen    writes <dir>/commits/*.patch with the sample's mix of kinds: code edits of covered
       lines (function bodies, and class-body constants of dialect modules), fixture
       edits, comment-only edits, and multi-file commits.
label  runs, for each commit, every test that can possibly see it (tests covering a
       changed line, tests loading a changed module for class-body edits, tests loading
       a changed fixture) and appends {"commit", "failing", "ran"} to <dir>/labels.jsonl.
eval   runs the selector (verification off) and prints simulated cost against the oracle,
       misses, and where the gap is.
"""

import ast
import difflib
import json
import os
import random
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

cmd, repo, maps, out = sys.argv[1:5]
os.environ["BLAST_SG"] = repo
os.environ["BLAST_MAPS"] = maps
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, here)
import selector  # noqa: E402

bit2id = selector.M["tests"]
tests = json.load(open(os.path.join(here, "sample", "tests.json"), encoding="utf-8"))["tests"]
id2num = {t["id"]: t["number"] for t in tests}
ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}

SWAPS = [(" and ", " or "), (" or ", " and "), (" == ", " != "), (" != ", " == "), (" is not ", " is "),
         (" is ", " is not "), (" < ", " <= "), (" <= ", " < "), (" > ", " >= "), (" >= ", " > "),
         ("True", "False"), ("False", "True"), (" is None", " == None"), (" + 1", " - 1"), ("[0]", "[1]")]
COMMENTS = ["# Keep in sync with the generator", "# Intentionally not cached", "# Cheap check first",
            "# Kept for backwards compatibility", "# Mirrors the behavior of the reference implementation"]


def diff(rel, a, b):
    return f"diff --git a/{rel} b/{rel}\n" + "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True), "a/" + rel, "b/" + rel))


def code_edit(rng, srcs, cands):
    for _ in range(200):
        rel, ln = rng.choice(cands)
        src = srcs.setdefault(rel, open(os.path.join(repo, rel), encoding="utf-8").read())
        lines = src.split("\n")
        text = lines[ln - 1]
        if text.strip().startswith(("#", "def ", "class ", "@", '"""')):
            continue
        opts = [(a, b) for a, b in SWAPS if a in text]
        nums = list(re.finditer(r"(?<![\w.])(\d+)(?![\w.])", text))
        r = rng.random()
        if r < 0.15 and nums:
            m = rng.choice(nums)
            new = text[: m.start()] + str(int(m.group(1)) + 1) + text[m.end():]
        elif r < 0.25 and "not " in text:
            new = text.replace("not ", "", 1)
        elif opts:
            a, b = rng.choice(opts)
            new = text.replace(a, b, 1)
        else:
            continue
        nl = lines[:]
        nl[ln - 1] = new
        ns = "\n".join(nl)
        try:
            ast.parse(ns)
        except SyntaxError:
            continue
        if ns != src:
            return rel, src, ns
    return None


def class_const_edit(rng, srcs):
    files = [f for f in selector.M["files"] if re.match(r"sqlglot/(dialects|parsers|generators)/(?!dialect\.py)", f)]
    for _ in range(200):
        rel = rng.choice(files)
        src = srcs.setdefault(rel, open(os.path.join(repo, rel), encoding="utf-8").read())
        tree = ast.parse(src)
        spots = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for st in node.body:
                    if isinstance(st, ast.Assign):
                        for c in ast.walk(st.value):
                            if isinstance(c, ast.Constant) and isinstance(c.value, (bool, int, str)) and c.lineno == c.end_lineno:
                                spots.append(c)
        if not spots:
            continue
        c = rng.choice(spots)
        lines = src.split("\n")
        t = lines[c.lineno - 1]
        seg = t[c.col_offset:c.end_col_offset]
        if isinstance(c.value, bool):
            new = "False" if c.value else "True"
        elif isinstance(c.value, int):
            new = str(c.value + 1)
        else:
            new = seg[:-1] + "Z" + seg[-1:] if len(seg) >= 2 else None
        if not new or new == seg:
            continue
        lines[c.lineno - 1] = t[:c.col_offset] + new + t[c.end_col_offset:]
        ns = "\n".join(lines)
        try:
            ast.parse(ns)
        except SyntaxError:
            continue
        return rel, src, ns
    return None


def fixture_edit(rng, srcs):
    files = sorted(f for f in set(selector.M.get("hopened", {})) | set(selector.M["opened"])
                   if f.startswith("tests/fixtures/") and f.endswith(".sql"))
    for _ in range(200):
        rel = rng.choice(files)
        src = srcs.setdefault(rel, open(os.path.join(repo, rel), encoding="utf-8").read())
        lines = src.split("\n")
        ln = rng.randrange(len(lines))
        t = lines[ln]
        if not t.strip() or t.startswith(("#", "--")):
            continue
        r = rng.random()
        if r < 0.3:
            new = t.rstrip(";") + " 42" + (";" if t.endswith(";") else "")
        elif r < 0.6 and " " in t:
            i = rng.choice([m.start() for m in re.finditer(" ", t)])
            new = t[:i] + " 0" + t[i:]
        elif "AS " in t:
            new = t.replace("AS ", "AS x", 1)
        else:
            new = t.replace(" ", "  ", 1)
        if new == t:
            continue
        lines[ln] = new
        return rel, src, "\n".join(lines)
    return None


def comment_edit(rng, srcs, cands):
    rel, ln = rng.choice(cands)
    src = srcs.setdefault(rel, open(os.path.join(repo, rel), encoding="utf-8").read())
    lines = src.split("\n")
    t = lines[ln - 1]
    ind = " " * (len(t) - len(t.lstrip()))
    lines.insert(ln - 1, ind + rng.choice(COMMENTS))
    ns = "\n".join(lines)
    try:
        ast.parse(ns)
    except SyntaxError:
        return None
    return rel, src, ns


def gen():
    n = int(sys.argv[5])
    rng = random.Random(int(sys.argv[6]) if len(sys.argv) > 6 else 1)
    cdir = os.path.join(out, "commits")
    shutil.rmtree(cdir, ignore_errors=True)
    os.makedirs(cdir)
    cands = [(rel, ln) for rel, lines in selector.M["ffiles"].items() if rel.startswith("sqlglot/") for ln in lines]
    kinds = {}
    for i in range(n):
        srcs = {}
        r = rng.random()
        parts = []
        if r < 0.40:
            kind, e = "code", code_edit(rng, srcs, cands)
            parts = [e]
        elif r < 0.53:
            kind, e = "code", class_const_edit(rng, srcs)
            parts = [e]
        elif r < 0.80:
            kind, e = "fixture", fixture_edit(rng, srcs)
            parts = [e]
        elif r < 0.90:
            kind, e = "comment", comment_edit(rng, srcs, cands)
            parts = [e]
        else:
            kind = "multi"
            parts = [comment_edit(rng, srcs, cands), fixture_edit(rng, srcs) if rng.random() < 0.6 else code_edit(rng, srcs, cands)]
        if any(p is None for p in parts) or len({p[0] for p in parts}) != len(parts):
            continue
        cid = f"s{i:03d}"
        with open(os.path.join(cdir, cid + ".patch"), "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(diff(*p) for p in parts))
        kinds[cid] = kind
    json.dump(kinds, open(os.path.join(out, "kinds.json"), "w"))
    shutil.copy(os.path.join(here, "sample", "tests.json"), os.path.join(out, "tests.json"))
    print("generated", len(kinds))


def possible(fcs):
    """Every test that could see the commit (a superset, for labelling)."""
    bits = 0
    for fc in fcs:
        rel = fc.new_path or fc.old_path
        if rel.endswith(".py"):
            src = open(os.path.join(repo, rel), encoding="utf-8").read()
            tree = ast.parse(src)
            for rem, add in selector.change_groups(fc.hunks):
                for ln, _t in rem + [(max(1, a - 1), None) for a, _ in add]:
                    path = []
                    selector.locate(tree.body, ln, path)
                    if any(isinstance(p, selector._DEF) for p in path):
                        bits |= selector.cov(rel, ln, ln + (1 if _t is None else 0), "any")
                    else:
                        bits |= selector.loaders(rel)
            bits |= selector.openers(rel)
        else:
            bits |= selector.openers(rel)
            r = selector.M.get("hopened", {}).get(rel)
            if r is not None:
                bits |= selector.row(r)
    return bits


def label():
    import runner

    jobs = int(sys.argv[5]) if len(sys.argv) > 5 else 8
    lp = os.path.join(out, "labels.jsonl")
    done = set()
    if os.path.exists(lp):
        done = {json.loads(line)["commit"] for line in open(lp)}
    names = sorted(n[:-6] for n in os.listdir(os.path.join(out, "commits")) if n.endswith(".patch"))
    todo = [c for c in names if c not in done]

    def one(cid):
        text = open(os.path.join(out, "commits", cid + ".patch"), encoding="utf-8").read()
        fcs = selector.parse_patch(text)
        patched = selector.patched_texts(fcs)
        bits = possible(fcs)
        ids = [bit2id[i] for i in range(selector.NBITS) if bits >> i & 1]
        failing = []
        if ids:
            res = runner.run_job({"sg": repo, "patched": patched, "tests": ids, "after_fail": ids,
                                  "duck": os.path.join(maps, "duck.pkl")}, 1200)
            failing = sorted({id2num[t] for t, f in res.items() if f} | {id2num[t] for t in ids if t not in res})
        line = json.dumps({"commit": cid, "failing": failing, "ran": len(ids)})
        with open(lp, "a") as f:
            f.write(line + "\n")
        print(cid, len(ids), "ran", len(failing), "failing", flush=True)

    with ThreadPoolExecutor(jobs) as pool:
        list(pool.map(one, todo))


def best(m):
    if not m:
        return 0
    b = None
    for k in range(1, 9):
        L = [0] * k
        for x in sorted(m, reverse=True):
            L[L.index(min(L))] += x
        c = max(L) + 2000 * k
        b = c if b is None or c < b else b
    return b


def evaluate():
    labels = {}
    for line in open(os.path.join(out, "labels.jsonl")):
        r = json.loads(line)
        labels[r["commit"]] = r
    kinds = json.load(open(os.path.join(out, "kinds.json")))
    env = dict(os.environ, BLAST_DATA=out)
    env.setdefault("BLAST_VERIFY", "0")
    p = subprocess.run([sys.executable, os.path.join(here, "selector.py")], env=env, capture_output=True)
    open(os.path.join(out, "eval_out.jsonl"), "wb").write(p.stdout)
    open(os.path.join(out, "eval_err.log"), "wb").write(p.stderr)
    est = sum(float(m.group(1)) for m in re.finditer(r"est ([0-9.]+)s", p.stderr.decode()))
    print(f"verification jobs: {p.stderr.decode().count(' ran ')}, estimated cpu {est:.1f}s")
    rows = []
    for line in p.stdout.decode().splitlines():
        r = json.loads(line)
        c = r["commit"]
        if c not in labels:
            continue
        sel = {x for w in r["workers"] for x in w}
        cost = (max(sum(ms[x] for x in w) for w in r["workers"]) + 2000 * len(r["workers"])) if r["workers"] else 0
        fail = labels[c]["failing"]
        miss = any(x not in sel for x in fail)
        o = best([ms[x] for x in fail])
        rows.append((cost / 1000, o / 1000, miss, c, kinds.get(c), len(sel), len(fail), labels[c]["ran"]))
    tot = sum(r[0] for r in rows)
    orc = sum(r[1] for r in rows)
    misses = [r for r in rows if r[2]]
    print(f"commits {len(rows)}  selector {tot:.1f}  oracle {orc:.1f}  gap {tot - orc:.1f}  misses {len(misses)}")
    by = {}
    for r in rows:
        by.setdefault(r[4], [0, 0, 0])
        by[r[4]][0] += 1
        by[r[4]][1] += r[0]
        by[r[4]][2] += r[1]
    for k, (n, a, b) in by.items():
        print(f"  {k:8s} n={n:3d} selector {a:7.1f} oracle {b:7.1f} gap {a - b:6.1f}")
    for r in misses:
        print("  MISS", r[3], r[4], "selected", r[5], "failing", r[6])
    print("largest gaps:")
    for r in sorted(rows, key=lambda r: r[1] - r[0])[:15]:
        print(f"  {r[0]:6.1f} vs {r[1]:5.1f}  {r[3]} {r[4]:8s} sel={r[5]} fail={r[6]} possible={r[7]}")


{"gen": gen, "label": label, "eval": evaluate}[cmd]()
