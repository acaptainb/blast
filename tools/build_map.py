"""Build maps/depmap.bin from the pinned sqlglot checkout.

    python tools/build_map.py <sqlglot_root> <tests.json> <work_dir> <out_map> [jobs]

Runs every test in its own process (tools/cov_one.py), then inverts the per-test
records into   source line -> set of tests   (and file -> tests, class -> tests).
Nothing here looks at a patch: the map only describes the pinned commit.
"""

import sys
import os
import json
import pickle
import zlib
import subprocess
from concurrent.futures import ThreadPoolExecutor

repo, tests_json, work, out_map = sys.argv[1:5]
jobs = int(sys.argv[5]) if len(sys.argv) > 5 else (os.cpu_count() or 2)
here = os.path.dirname(os.path.abspath(__file__))
os.makedirs(work, exist_ok=True)

tests = [t["id"] for t in json.load(open(tests_json, encoding="utf-8"))["tests"]]


def run(i):
    out = os.path.join(work, f"{i}.json")
    if os.path.exists(out):
        return
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONHASHSEED="0")
    p = subprocess.run(
        [sys.executable, os.path.join(here, "cov_one.py"), repo, out, tests[i]],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if not os.path.exists(out):
        sys.stderr.write(f"NO OUTPUT for {tests[i]}: {p.stderr[-500:]!r}\n")


# longest first so the stragglers do not sit at the end
order = sorted(range(len(tests)), key=lambda i: ("test_max_nodes" not in tests[i], "tpcds" not in tests[i]))
with ThreadPoolExecutor(jobs) as pool:
    list(pool.map(run, order))

lines = {}  # file -> line -> bitset   (module / class level execution)
flines = {}  # file -> line -> bitset  (execution inside function bodies)
opened = {}
inst = {}
cpu = []
bad = []
for i, tid in enumerate(tests):
    p = os.path.join(work, f"{i}.json")
    if not os.path.exists(p):
        bad.append((tid, "no output"))
        cpu.append(0.0)
        continue
    r = json.load(open(p, encoding="utf-8"))
    if r["out"] == "fail":
        bad.append((tid, r["msg"][-300:]))
    bit = 1 << i
    for src, dst in ((r["cov"], lines), (r["covf"], flines)):
        for f, ls in src.items():
            d = dst.setdefault(f, {})
            for ln in ls:
                d[ln] = d.get(ln, 0) | bit
    for f in r["opened"]:
        opened[f] = opened.get(f, 0) | bit
    for c in r["inst"]:
        inst[c] = inst.get(c, 0) | bit
    cpu.append(r["cpu_test"])

rows = []
row_id = {}


def rid(bits):
    k = row_id.get(bits)
    if k is None:
        k = row_id[bits] = len(rows)
        rows.append(bits.to_bytes((len(tests) + 7) // 8, "little"))
    return k


m = {
    "tests": tests,
    "files": {f: {ln: rid(b) for ln, b in d.items()} for f, d in lines.items()},
    "ffiles": {f: {ln: rid(b) for ln, b in d.items()} for f, d in flines.items()},
    "opened": {f: rid(b) for f, b in opened.items()},
    "inst": {c: rid(b) for c, b in inst.items()},
    "rows": rows,
    "cpu": cpu,
    "comp_inst": True,  # inst also records Tokenizer / Parser / Generator / Dialect classes
}
blob = zlib.compress(pickle.dumps(m, protocol=4), 9)
os.makedirs(os.path.dirname(os.path.abspath(out_map)), exist_ok=True)
with open(out_map, "wb") as f:
    f.write(blob)
print(f"tests={len(tests)} files={len(lines)} rows={len(rows)} opened={len(opened)} inst={len(inst)} bytes={len(blob)}")
print(f"baseline problems: {len(bad)}")
for tid, msg in bad[:40]:
    print("  ", tid, "::", msg.replace("\n", " | ")[-300:])
