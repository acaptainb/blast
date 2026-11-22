"""Mutation check of the operator-swap analysis (development tool, not used at run time).

    python tools/validate_swaps.py <sqlglot_root> <tests.json> <maps> <n> [seed]

Makes n random single-operator swaps (is<->==, is not<->!=, <<->=<=, ><->=>) in sqlglot,
asks the selector which tests each one needs, then runs every test that executes the
changed line and reports any failing test the selector left out.
"""

import ast
import difflib
import json
import os
import random
import sys
import tempfile

repo, tests_json, maps, n = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
seed = int(sys.argv[5]) if len(sys.argv) > 5 else 1
os.environ["BLAST_SG"] = repo
os.environ["BLAST_MAPS"] = maps
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import selector  # noqa: E402
import runner  # noqa: E402

SWAP = {"Is": "==", "Eq": " is ", "IsNot": "!=", "NotEq": " is not ", "Lt": "<=", "LtE": "<", "Gt": ">=", "GtE": ">"}
tests = json.load(open(tests_json, encoding="utf-8"))["tests"]
num2id = {t["number"]: t["id"] for t in tests}
bit2id = selector.M["tests"]
cpu = selector.CPU["tests"]

sites = sorted(selector.CMP["sites"])
rng = random.Random(seed)
rng.shuffle(sites)
done = bad = proven = 0
for rel, line, col in sites:
    if done >= n:
        break
    src = open(os.path.join(repo, rel), encoding="utf-8").read()
    lines = src.split("\n")
    tree = ast.parse(src)
    node = next((x for x in ast.walk(tree) if isinstance(x, ast.Compare) and x.lineno == line and x.col_offset == col), None)
    if node is None or node.left.end_lineno != line or node.comparators[0].lineno != line:
        continue
    op = type(node.ops[0]).__name__
    text = lines[line - 1]
    a, b = node.left.end_col_offset, node.comparators[0].col_offset
    seg = text[a:b]
    if seg.strip() not in ("is", "is not", "==", "!=", "<", "<=", ">", ">="):
        continue
    new = text[:a] + " " + SWAP[op].strip() + " " + text[b:]
    new_lines = lines[:]
    new_lines[line - 1] = new
    new_src = "\n".join(new_lines)
    try:
        ast.parse(new_src)
    except SyntaxError:
        continue
    covering = selector.cov(rel, line, line, "any")
    if not covering:
        continue
    cov_ids = [bit2id[i] for i in range(selector.NBITS) if covering >> i & 1]
    est = sum(cpu.get(t, 1.0) for t in cov_ids)
    if est > 25:
        continue
    patch = "".join(difflib.unified_diff(src.splitlines(True), new_src.splitlines(True), "a/" + rel, "b/" + rel))
    patch = f"diff --git a/{rel} b/{rel}\n" + patch
    bits = 0
    for fc in selector.parse_patch(patch):
        bits |= selector.file_bits(fc)
    picked = {bit2id[i] for i in range(selector.NBITS) if bits >> i & 1}
    res = runner.run_job({"sg": repo, "patched": {rel: new_src}, "tests": cov_ids, "duck": os.path.join(maps, "duck.pkl")}, 300)
    failing = {t for t, f in res.items() if f}
    unfinished = set(cov_ids) - set(res)
    missed = failing - picked
    done += 1
    proven += len(picked) < len(cov_ids)
    flag = "MISS" if missed else "ok"
    bad += bool(missed)
    print(f"{flag} {rel}:{line} {op}->{SWAP[op].strip()} covering={len(cov_ids)} picked={len(picked)} failing={len(failing)} unfinished={len(unfinished)}"
          + (f" missed={sorted(missed)[:3]}" if missed else ""), flush=True)
print(f"checked {done}, narrowed {proven}, misses {bad}")
