"""Label synthetic commits by running every test that executes a changed line
(development tool, not used at run time).

    python tools/label_mutants.py <sqlglot_root> <maps> <mut_dir> [jobs]

Writes <mut_dir>/answers.json in the sample's format.
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

repo, maps, mut = sys.argv[1], sys.argv[2], sys.argv[3]
jobs = int(sys.argv[4]) if len(sys.argv) > 4 else 12
os.environ["BLAST_SG"] = repo
os.environ["BLAST_MAPS"] = maps
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import selector  # noqa: E402
import runner  # noqa: E402

tests = json.load(open(os.path.join(mut, "tests.json"), encoding="utf-8"))["tests"]
id2num = {t["id"]: t["number"] for t in tests}
bit2id = selector.M["tests"]
out_path = os.path.join(mut, "answers.json")
answers = {}
if os.path.exists(out_path):
    answers = json.load(open(out_path))["commits"]


def label(name):
    cid = name[:-6]
    if cid in answers:
        return
    text = open(os.path.join(mut, "commits", name), encoding="utf-8").read()
    fcs = selector.parse_patch(text)
    patched = selector.patched_texts(fcs)
    bits = 0
    for fc in fcs:
        rel = fc.new_path
        for _a, _al, _b, _bl, body in fc.hunks:
            pass
        for rem, add in selector.change_groups(fc.hunks):
            for ln, _t in rem:
                bits |= selector.cov(rel, ln, ln, "any")
            for ln, _t in add:
                # an inserted line runs whenever its neighbours run
                bits |= selector.cov(rel, max(1, ln - 1), ln + 1, "any")
    ids = [bit2id[i] for i in range(selector.NBITS) if bits >> i & 1]
    if not ids:
        answers[cid] = {"failing": [], "kind": "code", "covering": 0}
        return
    res = runner.run_job({"sg": repo, "patched": patched, "tests": ids, "after_fail": ids,
                          "duck": os.path.join(maps, "duck.pkl")}, 900)
    failing = sorted(id2num[t] for t, f in res.items() if f)
    unfinished = [t for t in ids if t not in res]
    failing += sorted(id2num[t] for t in unfinished)  # unknown: count as failing
    answers[cid] = {"failing": sorted(set(failing)), "kind": "code", "covering": len(ids), "unfinished": len(unfinished)}
    print(cid, len(ids), "covering,", len(failing), "failing", flush=True)


names = sorted(n for n in os.listdir(os.path.join(mut, "commits")) if n.endswith(".patch"))
with ThreadPoolExecutor(jobs) as pool:
    list(pool.map(label, names))
json.dump({"commits": answers}, open(out_path, "w"), indent=1)
print("labelled", len(answers))
