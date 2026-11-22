"""Mutation check of fixture-entry selection (development tool, not used at run time).

    python tools/validate_fixtures.py <sqlglot_root> <tests.json> <maps> <n> [seed]

Makes n random one-line edits to fixture files (like the sample's "fixture" commits),
asks the selector which tests each needs, runs every test that loads the file, and
reports failing tests the selector left out.
"""

import difflib
import json
import os
import random
import sys

repo, tests_json, maps, n = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
seed = int(sys.argv[5]) if len(sys.argv) > 5 else 1
os.environ["BLAST_SG"] = repo
os.environ["BLAST_MAPS"] = maps
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import selector  # noqa: E402
import runner  # noqa: E402

bit2id = selector.M["tests"]
rng = random.Random(seed)
files = sorted(f for f in set(selector.M.get("hopened", {})) | set(selector.M["opened"])
               if f.startswith("tests/fixtures/") and f.endswith(".sql"))
# the big shared files several times over: they are where a wrong answer costs most
weighted = files + [f for f in files if "tpc-" in f or "identity" in f] * 6
EDITS = [
    lambda t: t.replace(" AS ", " AS x", 1),
    lambda t: t + " 42",
    lambda t: t.replace("SELECT", "SELECT 0", 1),
    lambda t: "# Keep in sync with the generator",
    lambda t: "-- Kept for backwards compatibility",
    lambda t: t.replace("(", "( ", 1),
    lambda t: t.replace(",", "", 1),
    lambda t: t.rstrip(";"),
]
done = bad = 0
while done < n:
    rel = rng.choice(weighted)
    src = open(os.path.join(repo, rel), encoding="utf-8").read()
    lines = src.split("\n")
    ln = rng.randrange(len(lines))
    if not lines[ln].strip():
        continue
    new_line = rng.choice(EDITS)(lines[ln])
    if new_line == lines[ln]:
        continue
    new_lines = lines[:]
    if new_line.startswith(("#", "--")):
        new_lines.insert(ln, new_line)
    else:
        new_lines[ln] = new_line
    new_src = "\n".join(new_lines)
    patch = "".join(difflib.unified_diff(src.splitlines(True), new_src.splitlines(True), "a/" + rel, "b/" + rel))
    patch = f"diff --git a/{rel} b/{rel}\n" + patch
    bits = 0
    for fc in selector.parse_patch(patch):
        bits |= selector.file_bits(fc)
    picked = {bit2id[i] for i in range(selector.NBITS) if bits >> i & 1}
    loaders = selector.openers(rel) | (selector.row(selector.M["hopened"][rel]) if rel in selector.M.get("hopened", {}) else 0)
    ids = [bit2id[i] for i in range(selector.NBITS) if loaders >> i & 1]
    if not ids:
        continue
    res = runner.run_job({"sg": repo, "patched": {rel: new_src}, "tests": ids, "after_fail": ids,
                          "duck": os.path.join(maps, "duck.pkl")}, 900)
    failing = {t for t, f in res.items() if f} | (set(ids) - set(res))
    missed = failing - picked
    done += 1
    bad += bool(missed)
    print(f"{'MISS' if missed else 'ok'} {rel}:{ln + 1} loaders={len(ids)} picked={len(picked)} failing={len(failing)}"
          + (f" missed={sorted(missed)[:3]}" if missed else ""), flush=True)
print(f"checked {done}, misses {bad}")
