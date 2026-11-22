"""Synthetic practice-like commits (development tool, not used at run time).

    python tools/mutants.py <sqlglot_root> <maps> <out_dir> <n> [seed]

Writes <out_dir>/commits/<id>.patch (single-line mutations of covered lines in sqlglot
function bodies, like the sample's "code" commits) and <out_dir>/tests.json.
"""

import ast
import difflib
import json
import os
import random
import re
import shutil
import sys

repo, maps, out, n = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
seed = int(sys.argv[5]) if len(sys.argv) > 5 else 1
os.environ["BLAST_SG"] = repo
os.environ["BLAST_MAPS"] = maps
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import selector  # noqa: E402

rng = random.Random(seed)
ffiles = selector.M["ffiles"]
cands = []
for rel, lines in ffiles.items():
    if rel.startswith("sqlglot/"):
        for ln in lines:
            cands.append((rel, ln))
rng.shuffle(cands)

SWAPS = [(" and ", " or "), (" or ", " and "), (" == ", " != "), (" != ", " == "), (" is not ", " is "),
         (" is ", " is not "), (" < ", " <= "), (" <= ", " < "), (" > ", " >= "), (" >= ", " > "),
         ("True", "False"), ("False", "True"), (" is None", " == None"), (" + 1", " - 1"), ("[0]", "[1]")]

os.makedirs(os.path.join(out, "commits"), exist_ok=True)
made = 0
srcs = {}
for rel, ln in cands:
    if made >= n:
        break
    if rel not in srcs:
        srcs[rel] = open(os.path.join(repo, rel), encoding="utf-8").read()
    src = srcs[rel]
    lines = src.split("\n")
    text = lines[ln - 1]
    if text.strip().startswith(("#", "def ", "class ", "@", '"""')):
        continue
    kind = rng.random()
    if kind < 0.12:  # comment only
        new = " " * (len(text) - len(text.lstrip())) + "# Keep in sync with the generator"
        new_lines = lines[: ln - 1] + [new] + lines[ln - 1:]
    else:
        opts = [(a, b) for a, b in SWAPS if a in text]
        nums = list(re.finditer(r"(?<![\w.])(\d+)(?![\w.])", text))
        if kind < 0.25 and nums:
            m = rng.choice(nums)
            new = text[: m.start()] + str(int(m.group(1)) + 1) + text[m.end():]
        elif kind < 0.35 and "not " in text:
            new = text.replace("not ", "", 1)
        elif opts:
            a, b = rng.choice(opts)
            new = text.replace(a, b, 1)
        else:
            continue
        if new == text:
            continue
        new_lines = lines[:]
        new_lines[ln - 1] = new
    new_src = "\n".join(new_lines)
    try:
        ast.parse(new_src)
    except SyntaxError:
        continue
    patch = "".join(difflib.unified_diff(src.splitlines(True), new_src.splitlines(True), "a/" + rel, "b/" + rel, n=3))
    cid = f"m{made:03d}"
    with open(os.path.join(out, "commits", cid + ".patch"), "w", encoding="utf-8", newline="\n") as f:
        f.write(f"diff --git a/{rel} b/{rel}\n" + patch)
    made += 1
shutil.copy(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample", "tests.json"), os.path.join(out, "tests.json"))
print("made", made)
