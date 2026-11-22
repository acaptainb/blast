"""Mutation check of the per-query TPC-DS check (development tool, not used at run time).

    python tools/validate_tpcds.py <sqlglot_root> <maps> <n> [seed] [jobs]

Mutates random lines that test_execute_tpcds executes, and where the selector would run
only some of its queries, runs both that per-query check and the full test. A commit
where the full test fails but the per-query check passes is a miss.
"""

import json
import os
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor

repo, maps, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
seed = int(sys.argv[4]) if len(sys.argv) > 4 else 1
jobs = int(sys.argv[5]) if len(sys.argv) > 5 else 3
MODE = sys.argv[6] if len(sys.argv) > 6 else "any"  # "replan": only mutants that use the plan check
os.environ["BLAST_SG"] = repo
os.environ["BLAST_MAPS"] = maps
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ast  # noqa: E402
import difflib  # noqa: E402

import selector  # noqa: E402
import runner  # noqa: E402

SWAPS = [(" and ", " or "), (" or ", " and "), (" == ", " != "), (" != ", " == "), (" is not ", " is "),
         (" is ", " is not "), (" < ", " <= "), (" <= ", " < "), (" > ", " >= "), (" >= ", " > "),
         ("True", "False"), ("False", "True"), (" + 1", " - 1"), ("[0]", "[1]"), ("not ", "")]
rng = random.Random(seed)
T = selector.TPCDS
lines = set()
for q in T["queries"]:
    for f, ls in q.items():
        if f.startswith("sqlglot/"):
            lines.update((f, ln) for ln in ls)
lines = sorted(lines)
rng.shuffle(lines)

muts = []
srcs = {}
for rel, ln in lines:
    if len(muts) >= n * 4:
        break
    src = srcs.setdefault(rel, open(os.path.join(repo, rel), encoding="utf-8").read())
    text = src.split("\n")[ln - 1]
    opts = [(a, b) for a, b in SWAPS if a in text]
    if not opts or text.strip().startswith("#"):
        continue
    a, b = rng.choice(opts)
    nl = src.split("\n")
    nl[ln - 1] = text.replace(a, b, 1)
    ns = "\n".join(nl)
    try:
        ast.parse(ns)
    except SyntaxError:
        continue
    patch = f"diff --git a/{rel} b/{rel}\n" + "".join(difflib.unified_diff(src.splitlines(True), ns.splitlines(True), "a/" + rel, "b/" + rel))
    selector.touch_reset()
    for fc in selector.parse_patch(patch):
        selector.file_bits(fc)
    lite = selector.tpcds_lite()
    if lite is None or (len(lite[0]) >= 57 and not lite[2]):
        continue
    if MODE == "replan" and not lite[2]:
        continue
    muts.append((rel, ln, a, b, ns, lite[0], lite[2]))
muts = muts[:n]
duck = os.path.join(maps, "duck.pkl")
res_all = []


def check(m):
    rel, ln, a, b, ns, only, check = m
    base = {"sg": repo, "patched": {rel: ns}, "tests": [selector.TPCDS_ID], "after_fail": [selector.TPCDS_ID], "duck": duck}
    lite = runner.run_job(dict(base, tpcds_only=only, tpcds_check={str(k): v for k, v in check.items()}), 600).get(selector.TPCDS_ID, True)
    full = runner.run_job(dict(base), 900).get(selector.TPCDS_ID, True)
    flag = "MISS" if full and not lite else "ok"
    print(f"{flag} {rel}:{ln} {a.strip()!r}->{b.strip()!r} execute={len(only)} replan={len(check)} lite_fail={lite} full_fail={full}", flush=True)
    res_all.append(flag)


with ThreadPoolExecutor(jobs) as pool:
    list(pool.map(check, muts))
print(f"checked {len(res_all)}, misses {res_all.count('MISS')}")
