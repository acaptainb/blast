"""Download sqlglot at the pinned commit into a directory (used by the Dockerfile).

    python tools/fetch_sqlglot.py <target_dir>
"""

import io
import os
import sys
import tarfile
import urllib.request

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://github.com/tobymao/sqlglot/archive/{COMMIT}.tar.gz"

target = sys.argv[1]
os.makedirs(target, exist_ok=True)
last = None
for attempt in range(5):
    try:
        with urllib.request.urlopen(URL, timeout=120) as r:
            data = r.read()
        break
    except Exception as e:  # noqa: BLE001
        last = e
else:
    raise SystemExit(f"could not download {URL}: {last!r}")

with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
    for m in tar.getmembers():
        parts = m.name.split("/", 1)
        if len(parts) < 2 or not parts[1]:
            continue
        m.name = parts[1]  # drop the "sqlglot-<commit>/" prefix
        if m.issym() or m.islnk():
            continue
        tar.extract(m, target, filter="data")
print(f"sqlglot {COMMIT} -> {target}")
