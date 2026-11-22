"""Clean CPU timing of every test through runner.child_main -> maps/cpu.json

    python tools/time_tests.py <sqlglot_root> <tests.json> <duck.pkl> <out.json>

One fresh process per test file, run one after another (no contention).
Records each test's CPU seconds and, per file, the CPU spent before its first test
(interpreter start + imports + class setup).
"""
import json
import os
import subprocess
import sys

if len(sys.argv) > 1 and sys.argv[1] == "--one":
    import time

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    job = json.loads(sys.stdin.read())
    t0 = time.process_time()
    log = []

    class Out:
        def write(self, b):
            log.append((time.process_time(), b.decode().strip()))

        def flush(self):
            pass

    import runner

    real_stdout = os.dup(1)
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.dup2(null, 2)
    runner.child_main(job, Out())
    res = {"tests": {}, "first": None}
    last = t0
    for t, line in log:
        kind, _, tid = line.partition(" ")
        if kind == "S":
            if res["first"] is None:
                res["first"] = t - t0
            last = t
        elif kind in ("P", "F"):
            res["tests"][tid] = [round(t - last, 4), kind]
            last = t
    os.write(real_stdout, json.dumps(res).encode())
    os._exit(0)

repo, tests_json, duck, out = sys.argv[1:5]
tests = [t["id"] for t in json.load(open(tests_json, encoding="utf-8"))["tests"]]
byfile = {}
for t in tests:
    byfile.setdefault(t.split("::")[0], []).append(t)
result = {"tests": {}, "file_overhead": {}}
for f, ids in byfile.items():
    job = {"sg": repo, "patched": {}, "tests": ids, "duck": os.path.abspath(duck)}
    p = subprocess.run([sys.executable, os.path.abspath(__file__), "--one"], input=json.dumps(job).encode(), capture_output=True)
    try:
        r = json.loads(p.stdout)
    except Exception:
        print("FAILED", f, p.stderr[-500:], file=sys.stderr)
        continue
    result["file_overhead"][f] = round(r["first"] or 0, 4)
    for tid, (c, k) in r["tests"].items():
        result["tests"][tid] = c
        if k == "F":
            print("baseline failure:", tid, file=sys.stderr)
    print(f"{f}: overhead {r['first']:.2f}s, tests {sum(c for c, _ in r['tests'].values()):.2f}s", file=sys.stderr, flush=True)
json.dump(result, open(out, "w"), indent=0)
print("total test cpu", round(sum(result["tests"].values()), 2), "files", len(result["file_overhead"]))
