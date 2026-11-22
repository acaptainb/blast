"""Mutation check of dict-entry selection (development tool, not used at run time).

    python tools/validate_dicts.py <sqlglot_root> <tests.json> <maps> <n> [seed]

Changes the value of one random entry of a class-body dict in a dialect-specific module,
asks the selector which tests that needs, runs every test that loads the module, and
reports failing tests the selector left out.
"""

import ast
import difflib
import json
import os
import random
import re
import sys

repo, tests_json, maps, n = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
seed = int(sys.argv[5]) if len(sys.argv) > 5 else 1
os.environ["BLAST_SG"] = repo
os.environ["BLAST_MAPS"] = maps
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import selector  # noqa: E402
import runner  # noqa: E402

bit2id = selector.M["tests"]
cpu = selector.CPU["tests"]
rng = random.Random(seed)
cands = [d for d, m in selector.DPROF.items() if not m["global_all"]
         and re.match(r"sqlglot/(dialects|parsers|generators|tokenizers|typing)/(?!dialect\.py)", d)]
rng.shuffle(cands)


def mutate_value(text):
    for pat, rep in ((r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r"(?<![\w.])(\d+)(?![\w.])", None),
                     (r'"([^"]*)"', None)):
        m = re.search(pat, text)
        if m:
            if rep is not None:
                return text[: m.start()] + rep + text[m.end():]
            if pat.startswith("(?<!"):
                return text[: m.start()] + str(int(m.group(1)) + 1) + text[m.end():]
            return text[: m.start()] + '"' + m.group(1) + 'Z"' + text[m.end():]
    return None


done = bad = 0
for did in cands:
    if done >= n:
        break
    rel, qual = did.split(":")
    src = open(os.path.join(repo, rel), encoding="utf-8").read()
    tree = ast.parse(src)
    parts = qual.split(".")
    body = tree.body
    node = None
    for name in parts[:-1]:
        cls = next((x for x in body if isinstance(x, ast.ClassDef) and x.name == name), None)
        if cls is None:
            break
        body = cls.body
    else:
        node = next((x for x in body if isinstance(x, ast.Assign) and len(x.targets) == 1
                     and isinstance(x.targets[0], ast.Name) and x.targets[0].id == parts[-1]
                     and isinstance(x.value, ast.Dict)), None)
    if node is None:
        continue
    entries = [(k, v) for k, v in zip(node.value.keys, node.value.values) if k is not None and v.lineno == v.end_lineno]
    if not entries:
        continue
    k, v = rng.choice(entries)
    lines = src.split("\n")
    text = lines[v.lineno - 1]
    seg = text[v.col_offset: v.end_col_offset]
    new_seg = mutate_value(seg)
    if new_seg is None or new_seg == seg:
        continue
    new_lines = lines[:]
    new_lines[v.lineno - 1] = text[: v.col_offset] + new_seg + text[v.end_col_offset:]
    new_src = "\n".join(new_lines)
    try:
        ast.parse(new_src)
    except SyntaxError:
        continue
    loaders = selector.loaders(rel)
    ids = [bit2id[i] for i in range(selector.NBITS) if loaders >> i & 1]
    if not ids or sum(cpu.get(t, 1.0) for t in ids) > 30:
        continue
    patch = "".join(difflib.unified_diff(src.splitlines(True), new_src.splitlines(True), "a/" + rel, "b/" + rel))
    bits = 0
    for fc in selector.parse_patch(f"diff --git a/{rel} b/{rel}\n" + patch):
        bits |= selector.file_bits(fc)
    picked = {bit2id[i] for i in range(selector.NBITS) if bits >> i & 1}
    res = runner.run_job({"sg": repo, "patched": {rel: new_src}, "tests": ids, "after_fail": ids,
                          "duck": os.path.join(maps, "duck.pkl")}, 900)
    failing = {t for t, f in res.items() if f} | (set(ids) - set(res))
    missed = failing - picked
    done += 1
    bad += bool(missed)
    print(f"{'MISS' if missed else 'ok'} {did}[{seg[:30]!r}->{new_seg[:30]!r}] loaders={len(ids)} picked={len(picked)} failing={len(failing)}"
          + (f" missed={sorted(missed)[:3]}" if missed else ""), flush=True)
print(f"checked {done}, misses {bad}")
