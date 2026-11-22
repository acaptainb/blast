"""Mutation check of class-constant selection (development tool, not used at run time).

    python tools/validate_consts.py <sqlglot_root> <tests.json> <maps> <n> [seed] [jobs]

Flips / bumps one simple class constant (`NAME = True`, `NAME = 3`, `NAME = "x"`) in a
dialect-specific module, asks the selector which tests that needs, runs every test that
loads the module, and reports failing tests the selector left out.
"""

import ast
import difflib
import os
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor

repo, tests_json, maps, n = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
seed = int(sys.argv[5]) if len(sys.argv) > 5 else 1
jobs = int(sys.argv[6]) if len(sys.argv) > 6 else 3
os.environ["BLAST_SG"] = repo
os.environ["BLAST_MAPS"] = maps
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import selector  # noqa: E402
import runner  # noqa: E402

bit2id = selector.M["tests"]
cpu = selector.CPU["tests"]
rng = random.Random(seed)
files = sorted(f for f in selector.M["files"] if re.match(r"sqlglot/(dialects|parsers|generators|tokenizers)/(?!dialect\.py)", f))
spots = []
for rel in files:
    src = open(os.path.join(repo, rel), encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ClassDef):
            for st in node.body:
                if (isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name)
                        and re.match(r"^[A-Z][A-Z0-9_]*$", st.targets[0].id) and isinstance(st.value, ast.Constant)
                        and isinstance(st.value.value, (bool, int, str)) and st.lineno == st.end_lineno):
                    spots.append((rel, st.lineno, st.value))
rng.shuffle(spots)
muts = []
for rel, ln, c in spots:
    if len(muts) >= n:
        break
    src = open(os.path.join(repo, rel), encoding="utf-8").read()
    lines = src.split("\n")
    t = lines[ln - 1]
    seg = t[c.col_offset:c.end_col_offset]
    if isinstance(c.value, bool):
        new = "False" if c.value else "True"
    elif isinstance(c.value, int):
        new = str(c.value + 1)
    else:
        new = seg[:-1] + "Z" + seg[-1:] if len(seg) >= 2 else None
    if not new or new == seg:
        continue
    lines[ln - 1] = t[:c.col_offset] + new + t[c.end_col_offset:]
    ns = "\n".join(lines)
    loaders = selector.loaders(rel)
    ids = [bit2id[i] for i in range(selector.NBITS) if loaders >> i & 1]
    if not ids or sum(cpu.get(x, 1.0) for x in ids) > 30:
        continue
    patch = f"diff --git a/{rel} b/{rel}\n" + "".join(difflib.unified_diff(src.splitlines(True), ns.splitlines(True), "a/" + rel, "b/" + rel))
    bits = 0
    for fc in selector.parse_patch(patch):
        bits |= selector.file_bits(fc)
    picked = {bit2id[i] for i in range(selector.NBITS) if bits >> i & 1}
    muts.append((rel, ln, seg, new, ns, ids, picked))
out = []


def check(m):
    rel, ln, seg, new, ns, ids, picked = m
    res = runner.run_job({"sg": repo, "patched": {rel: ns}, "tests": ids, "after_fail": ids,
                          "duck": os.path.join(maps, "duck.pkl")}, 900)
    failing = {t for t, f in res.items() if f} | (set(ids) - set(res))
    missed = failing - picked
    out.append(bool(missed))
    print(f"{'MISS' if missed else 'ok'} {rel}:{ln} {seg}->{new} loaders={len(ids)} picked={len(picked)} failing={len(failing)}"
          + (f" missed={sorted(missed)[:3]}" if missed else ""), flush=True)


with ThreadPoolExecutor(jobs) as pool:
    list(pool.map(check, muts))
print(f"checked {len(out)}, misses {sum(out)}")
