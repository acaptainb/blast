#!/usr/bin/env python3
"""Blast-radius selector.

For every patch: work out which statements of the pinned sqlglot checkout really
changed, look up which tests execute those statements (maps/depmap.bin, built from
the pinned commit by tools/build_map.py), and print those tests packed into workers.

Nothing here is keyed by a patch or by a kind of edit: the only inputs are the diff
itself, the pinned sources and dependency maps of the pinned commit.
"""

import ast
import copy
import json
import os
import pickle
import re
import sys
import zlib

DATA = os.environ.get("BLAST_DATA", "/data")
SG = os.environ.get("BLAST_SG", "/app/sg")
MAPS = os.environ.get("BLAST_MAPS", "/app/maps")

WORKER_MS = 2000
MAX_WORKERS = 8


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- maps
def _load(name):
    with open(os.path.join(MAPS, name), "rb") as f:
        return pickle.loads(zlib.decompress(f.read()))


M = _load("depmap.bin")
S = _load("static.bin")
try:
    CMP = _load("cmpprof.bin")
except OSError:
    CMP = {"cases": {}, "sites": set()}
ROWS = M["rows"]
NBITS = len(M["tests"])
ALL = (1 << NBITS) - 1
_row_cache = {}


def row(i):
    v = _row_cache.get(i)
    if v is None:
        v = _row_cache[i] = int.from_bytes(ROWS[i], "little")
    return v


def cov(rel, lo, hi, level="any"):
    """Tests that executed any line lo..hi of a file. level: mod (import time), fn, any."""
    b = 0
    if level != "fn":
        d = M["files"].get(rel)
        if d:
            for ln in range(lo, hi + 1):
                r = d.get(ln)
                if r is not None:
                    b |= row(r)
    if level != "mod":
        d = M["ffiles"].get(rel)
        if d:
            for ln in range(lo, hi + 1):
                r = d.get(ln)
                if r is not None:
                    b |= row(r)
    return b


_file_cache = {}


def loaders(rel):
    """Tests that import the module (ran any of its module / class level lines)."""
    k = ("L", rel)
    if k not in _file_cache:
        b = 0
        for r in set(M["files"].get(rel, {}).values()):
            b |= row(r)
        _file_cache[k] = b
    return _file_cache[k]


def touchers(rel):
    k = ("T", rel)
    if k not in _file_cache:
        b = loaders(rel)
        for r in set(M["ffiles"].get(rel, {}).values()):
            b |= row(r)
        _file_cache[k] = b
    return _file_cache[k]


def openers(rel):
    r = M["opened"].get(rel)
    return row(r) if r is not None else 0


_ref_cache = {}


def refcov(name, prefix=""):
    """Tests that executed a function-body line which reads `name` (in files under prefix)."""
    b = _ref_cache.get((name, prefix))
    if b is None:
        b = 0
        for f, lines in S["refs"].get(name, {}).items():
            if not f.startswith(prefix):
                continue
            d = M["ffiles"].get(f)
            if d:
                for ln in lines:
                    r = d.get(ln)
                    if r is not None:
                        b |= row(r)
        _ref_cache[(name, prefix)] = b
    return b


# --------------------------------------------------------------------------- patch parsing
class FileChange:
    def __init__(self):
        self.old_path = None
        self.new_path = None
        self.is_new = False
        self.is_deleted = False
        self.rename_from = None
        self.rename_to = None
        self.binary = False
        self.hunks = []  # (old_start, old_len, new_start, new_len, body_lines)


_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip(p):
    p = p.split("\t")[0].strip()
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p[:2] in ("a/", "b/"):
        p = p[2:]
    return p


def parse_patch(text):
    lines = text.split("\n")
    files = []
    cur = None
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        if ln.startswith("diff --git "):
            cur = FileChange()
            files.append(cur)
            m = re.match(r"diff --git a/(.*?) b/(.*)$", ln)
            if m:
                cur.old_path, cur.new_path = m.group(1), m.group(2)
            i += 1
            continue
        if ln.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            if cur is None or cur.hunks:
                cur = FileChange()
                files.append(cur)
            a, b = _strip(ln[4:]), _strip(lines[i + 1][4:])
            if a == "/dev/null":
                cur.is_new = True
            else:
                cur.old_path = a
            if b == "/dev/null":
                cur.is_deleted = True
            else:
                cur.new_path = b
            i += 2
            continue
        if cur is not None:
            if ln.startswith("new file mode"):
                cur.is_new = True
            elif ln.startswith("deleted file mode"):
                cur.is_deleted = True
            elif ln.startswith("rename from "):
                cur.rename_from = ln[12:].strip()
            elif ln.startswith("rename to "):
                cur.rename_to = ln[10:].strip()
            elif ln.startswith("Binary files ") or ln.startswith("GIT binary patch"):
                cur.binary = True
            elif ln.startswith("@@"):
                m = _HUNK.match(ln)
                if m:
                    a = int(m.group(1))
                    al = 1 if m.group(2) is None else int(m.group(2))
                    b = int(m.group(3))
                    bl = 1 if m.group(4) is None else int(m.group(4))
                    i += 1
                    body = []
                    oc = nc = 0
                    while i < n and (oc < al or nc < bl):
                        h = lines[i]
                        c = h[:1]
                        if c == "\\":
                            body.append(h)
                            i += 1
                            continue
                        if c == "-":
                            oc += 1
                        elif c == "+":
                            nc += 1
                        else:
                            oc += 1
                            nc += 1
                        body.append(h)
                        i += 1
                    if i < n and lines[i].startswith("\\"):
                        body.append(lines[i])
                        i += 1
                    cur.hunks.append((a, al, b, bl, body))
                    continue
        i += 1
    return files


def apply_hunks(old_text, hunks):
    """Apply hunks to old_text. Returns (new_text, ok)."""
    old = old_text.split("\n")
    out = []
    pos = 0
    ok = True
    drop_final_newline = False
    add_final_newline = False
    for a, al, _b, _bl, body in hunks:
        start = a - 1 if al > 0 else a
        if start < pos or start > len(old):
            return old_text, False
        out.extend(old[pos:start])
        pos = start
        prev = " "
        for h in body:
            c = h[:1]
            t = h[1:]
            if c == "\\":
                if prev == "+":
                    drop_final_newline = True
                elif prev == "-":
                    add_final_newline = True
                else:
                    drop_final_newline = False
                continue
            if c == "+":
                out.append(t)
            elif c == "-":
                if pos >= len(old) or old[pos] != t:
                    ok = False
                pos += 1
            else:
                if pos < len(old):
                    if old[pos] != t and old[pos].rstrip() != t.rstrip():
                        ok = False
                    out.append(old[pos])
                else:
                    ok = False
                pos += 1
            prev = c
    out.extend(old[pos:])
    if drop_final_newline and out and out[-1] == "":
        out.pop()
    if add_final_newline and (not out or out[-1] != ""):
        out.append("")
    return "\n".join(out), ok


def change_groups(hunks):
    """-> [(removed [(old_line, text)], added [(new_line, text)])] for each run of -/+ lines."""
    groups = []
    for a, al, b, bl, body in hunks:
        o = a if al > 0 else a + 1
        nw = b if bl > 0 else b + 1
        rem, add = [], []
        for h in body:
            c = h[:1]
            if c == "\\":
                continue
            if c == "-":
                rem.append((o, h[1:]))
                o += 1
            elif c == "+":
                add.append((nw, h[1:]))
                nw += 1
            else:
                if rem or add:
                    groups.append((rem, add))
                    rem, add = [], []
                o += 1
                nw += 1
        if rem or add:
            groups.append((rem, add))
    return groups


# --------------------------------------------------------------------------- python analysis
_DEF = (ast.FunctionDef, ast.AsyncFunctionDef)


def stmt_lo(s):
    return min([d.lineno for d in getattr(s, "decorator_list", [])] + [s.lineno])


def child_bodies(s):
    for f in ("body", "orelse", "finalbody"):
        v = getattr(s, f, None)
        if isinstance(v, list):
            yield v
    for h in getattr(s, "handlers", None) or ():
        yield h.body
    for c in getattr(s, "cases", None) or ():
        yield c.body


def locate(body, line, path):
    """Append to path the chain of statements (outermost first) that contain `line`."""
    for s in body:
        if stmt_lo(s) <= line <= s.end_lineno:
            path.append(s)
            for cb in child_bodies(s):
                if locate(cb, line, path):
                    break
            return True
    return False


def string_lines(tree):
    """Lines that lie inside a multi-line string literal (a '#' there is not a comment)."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Constant, ast.JoinedStr)) and node.end_lineno > node.lineno:
            if isinstance(node, ast.JoinedStr) or isinstance(node.value, (str, bytes)):
                out.update(range(node.lineno, node.end_lineno + 1))
    return out


def significant(lines, strs):
    out = []
    for ln, txt in lines:
        t = txt.strip()
        if (t and not t.startswith("#")) or ln in strs:
            out.append((ln, txt))
    return out


def indent(txt):
    return len(txt) - len(txt.lstrip())


class _MaskLambdas(ast.NodeTransformer):
    def visit_Lambda(self, node):
        return ast.Lambda(args=node.args, body=ast.Constant(value=0))


def masked_dump(s):
    return ast.dump(_MaskLambdas().visit(copy.deepcopy(s)))


def is_docstring(s, parent_body):
    return (
        isinstance(s, ast.Expr)
        and isinstance(s.value, ast.Constant)
        and isinstance(s.value.value, str)
        and parent_body
        and parent_body[0] is s
    )


def target_names(s):
    names = []
    targets = []
    if isinstance(s, ast.Assign):
        targets = s.targets
    elif isinstance(s, (ast.AnnAssign, ast.AugAssign)):
        targets = [s.target]
    else:
        return None
    for t in targets:
        if isinstance(t, ast.Name):
            names.append(t.id)
        else:
            return None
    return names or None


_CONST_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def scope_stmt_bits(rel, s, path):
    """A simple statement that runs at import time, directly in a module or class body."""
    load = cov(rel, s.lineno, s.end_lineno, "mod")
    names = target_names(s)
    if not names:
        return load
    classes = [p for p in path if isinstance(p, ast.ClassDef)]
    if classes and rel.endswith(".py"):
        ckey = rel[:-3].replace("/", ".") + ":" + ".".join(c.name for c in classes)
        subs = S["subs"].get(ckey)
        if subs is not None:
            # An expression class: its attributes are only reachable through an instance
            # of the class (or a subclass), or through code that names the class.
            users = 0
            for k in subs:
                r = M["inst"].get(k)
                if r is not None:
                    users |= row(r)
                users |= refcov(k.split(":")[1].split(".")[-1])
            return load & users
        fam = S.get("comp", {}).get(ckey)
        if fam is not None and M.get("comp_inst"):
            # A Tokenizer / Parser / Generator / Dialect class: its attributes are read through
            # an instance of the class, a subclass, or a dialect that uses one of them, or by
            # code that names one of these classes. Unless another module body copies it.
            outer = {k.split(":")[1].split(".")[0] for k in fam} | {classes[0].name}
            copied = any(
                set(re.findall(r"\w+", txt)) & outer for nm in names for _f, _ln, txt in S["modattr"].get(nm, ())
            )
            if not copied:
                users = 0
                for k in fam:
                    r = M["inst"].get(k)
                    if r is not None:
                        users |= row(r)
                for o in outer:
                    users |= refcov(o)
                # tests that read the attribute off a class themselves (e.g. looping over dialects)
                for nm in names:
                    users |= refcov(nm, "tests/")
                load &= users
    # independently: a constant is read by code that names it (both are supersets: intersect)
    users = 0
    for nm in names:
        if not _CONST_NAME.match(nm):
            return load
        if nm not in S["refs"] or S["modrefs"].get(nm, 0) > 0:
            return load
        users |= refcov(nm)
    return load & users


_BODY_FIELDS = {"body", "orelse", "finalbody", "handlers", "cases"}
ANDOR = os.environ.get("BLAST_ANDOR", "0") == "1"  # and/or swap proofs: off until fully mutation-checked
_SWAP = {
    frozenset(("Is", "Eq")): "eqis",
    frozenset(("IsNot", "NotEq")): "eqis",
    frozenset(("Lt", "LtE")): "bound",
    frozenset(("Gt", "GtE")): "bound",
}


def op_swaps(a, b, out, top=True):
    """Walk two ASTs in step. Collect (old Compare node, kind) for every single-operator
    comparison whose operator changed. False if they differ in any other way."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        if ANDOR and isinstance(a, ast.BoolOp) and len(a.values) == 2 and len(b.values) == 2 and type(a.op) is not type(b.op):
            out.append((a, "andor"))
            return op_swaps(a.values, b.values, out, False)
        if isinstance(a, ast.Compare) and len(a.ops) == 1 and len(b.ops) == 1 and type(a.ops[0]) is not type(b.ops[0]):
            kind = _SWAP.get(frozenset((type(a.ops[0]).__name__, type(b.ops[0]).__name__)))
            if kind is None:
                return False
            out.append((a, kind))
            return op_swaps(a.left, b.left, out, False) and op_swaps(a.comparators, b.comparators, out, False)
        for f in a._fields:
            if top and f in _BODY_FIELDS:
                continue  # a compound statement's header only: its body lines are other lines
            if not op_swaps(getattr(a, f, None), getattr(b, f, None), out, False):
                return False
        return True
    if isinstance(a, list):
        return len(a) == len(b) and all(op_swaps(x, y, out, False) for x, y in zip(a, b))
    return a == b


def swap_bits(rel, s_old, s_new):
    """Tests that can observe the change of s_old into s_new, if the only change is an
    operator swap the comparison profile covers; None otherwise."""
    swaps = []
    if s_new is None or not op_swaps(s_old, s_new, swaps) or not swaps:
        return None
    cases = CMP["cases"]
    sites = CMP["sites"]
    bits = 0
    for node, kind in swaps:
        key = (rel, node.lineno, node.col_offset, "B" if kind == "andor" else "C")
        if key not in sites:
            return None
        e = cases.get(key)
        hit = e.get(kind, -1) if e else []
        if hit == -1:
            return None
        for i in hit:
            bits |= 1 << i
    # only tests that ran the statement can see it
    return bits & cov(rel, s_old.lineno, s_old.end_lineno, "any")


def find_def(tree, qual):
    body = tree.body
    node = None
    for name in qual:
        node = None
        for s in body:
            if isinstance(s, _DEF + (ast.ClassDef,)) and s.name == name:
                node = s
        if node is None:
            return None
        body = node.body
    return node


def analyze_py(rel, old_text, new_text, hunks, patch_ok):
    try:
        ot = ast.parse(old_text.encode("utf-8", "surrogateescape"))
    except Exception:
        return touchers(rel) | openers(rel)
    try:
        nt = ast.parse(new_text.encode("utf-8", "surrogateescape"))
    except Exception:
        # the module no longer compiles: every test that imports it breaks
        return touchers(rel) | openers(rel)
    if patch_ok and ast.dump(ot) == ast.dump(nt):
        # same program (comments / layout only): only tests that read the file as data can notice
        return openers(rel)
    if not patch_ok:
        return touchers(rel) | openers(rel)

    ostr, nstr = string_lines(ot), string_lines(nt)
    bits = openers(rel)
    for rem, add in change_groups(hunks):
        srem = significant(rem, ostr)
        sadd = significant(add, nstr)
        if not srem and not sadd:
            continue
        for r, _txt in srem:
            bits |= mark_old_line(rel, ot, nt, r, srem, sadd)
        if sadd and (not srem or indent(srem[0][1]) != indent(sadd[0][1]) or len(sadd) != len(srem)):
            bits |= mark_insertion(rel, ot, nt, sadd)
    return bits


def mark_old_line(rel, ot, nt, r, srem, sadd):
    path = []
    locate(ot.body, r, path)
    if not path:
        return loaders(rel)
    s = path[-1]
    parent_body = path[-2].body if len(path) > 1 and isinstance(getattr(path[-2], "body", None), list) else ot.body
    in_func = any(isinstance(p, _DEF) for p in path[:-1])
    if isinstance(s, _DEF):
        if r < s.lineno:  # a decorator
            return cov(rel, stmt_lo(s), s.end_lineno, "any")
        return cov(rel, stmt_lo(s), s.end_lineno, "fn")  # signature: whoever runs the body
    if isinstance(s, ast.ClassDef):
        return cov(rel, stmt_lo(s), s.end_lineno, "any")
    s_new = None
    if sadd:
        npath = []
        locate(nt.body, sadd[0][0], npath)
        if npath and type(npath[-1]) is type(s) and len(npath) == len(path):
            s_new = npath[-1]
    sw = swap_bits(rel, s, s_new) if len(srem) == len(sadd) else None
    if sw is not None:
        return sw
    if isinstance(getattr(s, "body", None), list):  # if / for / while / with / try / match header or clause
        return cov(rel, s.lineno, s.end_lineno, "any")
    # a simple statement
    if is_docstring(s, parent_body):
        return 0
    if sadd:
        npath = []
        locate(nt.body, sadd[0][0], npath)
        if npath and type(npath[-1]) is type(s):
            try:
                same_outside = masked_dump(s) == masked_dump(npath[-1])
            except Exception:
                same_outside = False
            if same_outside:
                # the statements differ only inside lambda bodies: only running a lambda can show it
                lams = [
                    n
                    for n in ast.walk(s)
                    if isinstance(n, ast.Lambda) and n.lineno <= r <= n.end_lineno
                ]
                if lams:
                    b = 0
                    for lam in lams:
                        b |= cov(rel, lam.lineno, lam.end_lineno, "fn")
                    return b
    if not in_func:
        return scope_stmt_bits(rel, s, path)
    return cov(rel, s.lineno, s.end_lineno, "any")


def mark_insertion(rel, ot, nt, sadd):
    npath = []
    locate(nt.body, sadd[0][0], npath)
    last_def = None
    for i, p in enumerate(npath):
        if isinstance(p, _DEF):
            last_def = i
    if last_def is None:
        return loaders(rel)
    qual = [p.name for p in npath[: last_def + 1] if isinstance(p, _DEF + (ast.ClassDef,))]
    if len(qual) != last_def + 1:
        return loaders(rel)  # the function sits under an if/try at module level: stay coarse
    old_def = find_def(ot, qual)
    if old_def is None or not isinstance(old_def, _DEF):
        return loaders(rel)  # a brand new function may override an inherited one
    return cov(rel, stmt_lo(old_def), old_def.end_lineno, "fn")


# --------------------------------------------------------------------------- fixture entries
FIXDIR = "tests/fixtures/"


def _filter_comments(text):  # tests/helpers.py, pinned commit
    return "\n".join([line for line in text.splitlines() if line and not line.startswith("--")])


def _extract_meta(sql):  # tests/helpers.py, pinned commit
    meta = {}
    sql_lines = sql.split("\n")
    i = 0
    while sql_lines[i].startswith("#"):
        key, val = sql_lines[i].split(":", maxsplit=1)
        meta[key.lstrip("#").strip()] = val.strip()
        i += 1
    sql = "\n".join(sql_lines[i:])
    return sql, meta


def fixture_pairs(text):
    """What load_sql_fixture_pairs yields for this text, or None if it would raise."""
    try:
        statements = _filter_comments(text).split(";")
        out = []
        for i in range(0, len(statements), 2):
            if i + 1 < len(statements):
                sql, meta = _extract_meta(statements[i].strip())
                out.append((meta, sql, statements[i + 1].strip()))
        return out
    except Exception:
        return None


def fixture_lines(text):
    """What load_sql_fixtures yields for this text."""
    out, lines = [], []
    for line in _filter_comments(text).splitlines():
        lines.append(line)
        if not line.startswith("#"):
            out.append("\n".join(lines))
            lines.clear()
    return out


_fx_index = None


def fx_index():
    global _fx_index
    if _fx_index is None:
        _fx_index = {}
        for (name, i, role), r in M.get("fx", {}).items():
            _fx_index.setdefault(name, {})[(i, role)] = r
    return _fx_index


def fixture_bits(rel, old_text, new_text):
    """Tests that read a changed entry of a fixture through the test helpers, plus tests
    that read the file any other way."""
    bits = openers(rel)  # read outside the helpers
    hr = M.get("hopened", {}).get(rel)
    if hr is None:
        return bits
    idx = fx_index().get(rel[len(FIXDIR):], {})
    pair_users = fix_users = 0
    for (i, role), r in idx.items():
        if role == "f":
            fix_users |= row(r)
        else:
            pair_users |= row(r)
    # load_sql_fixture_pairs
    op, np_ = fixture_pairs(old_text), fixture_pairs(new_text)
    if op is None or np_ is None or len(op) != len(np_):
        bits |= row(hr)  # entries shift, or the loader raises: every test that loads it
    else:
        for i, (a, b) in enumerate(zip(op, np_)):
            for role, x, y in (("m", a[0], b[0]), ("s", a[1], b[1]), ("e", a[2], b[2])):
                if x != y:
                    r = idx.get((i, role))
                    if r is not None:
                        bits |= row(r)
    # load_sql_fixtures
    ol, nl = fixture_lines(old_text), fixture_lines(new_text)
    if len(ol) != len(nl):
        bits |= fix_users
    else:
        for i, (x, y) in enumerate(zip(ol, nl)):
            if x != y:
                r = idx.get((i, "f"))
                if r is not None:
                    bits |= row(r)
    return bits


# --------------------------------------------------------------------------- per file
PYTEST_FILES = {"pytest.ini", "tox.ini", ".pytest.ini", "conftest.py"}


def read_old(rel):
    p = os.path.join(SG, rel)
    try:
        with open(p, "rb") as f:
            return f.read().decode("utf-8", "surrogateescape")
    except OSError:
        return None


def dir_openers(rel):
    d = rel.rsplit("/", 1)[0] + "/" if "/" in rel else ""
    b = 0
    for f, r in M["opened"].items():
        if f.startswith(d) and "/" not in f[len(d):]:
            b |= row(r)
    return b


def file_bits(fc):
    bits = 0
    old_rel = fc.rename_from or fc.old_path
    new_rel = fc.rename_to or fc.new_path
    rel = old_rel or new_rel
    if rel is None:
        return ALL
    base = rel.rsplit("/", 1)[-1]
    if base in PYTEST_FILES or (new_rel and new_rel.rsplit("/", 1)[-1] in PYTEST_FILES):
        return ALL
    old_text = read_old(old_rel) if old_rel and not fc.is_new else None

    if fc.is_deleted or (fc.rename_from and fc.rename_to and fc.rename_from != fc.rename_to):
        bits |= touchers(old_rel) | openers(old_rel)
        if not fc.is_deleted:
            bits |= new_file_bits(new_rel)
        return bits
    if old_text is None:  # a file the pinned commit does not have
        return new_file_bits(new_rel or rel)
    if fc.binary:
        return touchers(rel) | openers(rel)
    if not fc.hunks:
        return 0  # mode change only

    new_text, ok = apply_hunks(old_text, fc.hunks)
    if rel.endswith(".py"):
        return analyze_py(rel, old_text, new_text, fc.hunks, ok)
    if base in ("pyproject.toml", "setup.cfg"):
        added = "\n".join(h[1:] for _a, _al, _b, _bl, body in fc.hunks for h in body if h[:1] == "+")
        if "pytest" in added or "pytest" in new_text and "pytest" not in old_text:
            return ALL
    if new_text == old_text and ok:
        return 0
    if ok and rel.startswith(FIXDIR) and "fx" in M:
        return fixture_bits(rel, old_text, new_text)
    return openers(rel) | (row(M["hopened"][rel]) if rel in M.get("hopened", {}) else 0)


def new_file_bits(rel):
    if rel is None:
        return ALL
    base = rel.rsplit("/", 1)[-1]
    if base in PYTEST_FILES:
        return ALL
    if rel.endswith(".py"):
        if "/" not in rel:
            return ALL  # the checkout root is on sys.path: a new module there can shadow anything
        if rel.startswith("sqlglot/"):
            return dir_openers(rel)
        return 0
    return openers(rel) | dir_openers(rel)


# --------------------------------------------------------------------------- packing
def split(nums, ms):
    if not nums:
        return []
    items = sorted(nums, key=lambda t: -ms[t])
    total = sum(ms[t] for t in items)
    big = ms[items[0]]
    best = None
    for k in range(1, min(MAX_WORKERS, len(items)) + 1):
        lower = max(big, -(-total // k)) + WORKER_MS * k
        if best is not None and lower >= best[0]:
            continue
        loads = [0] * k
        ws = [[] for _ in range(k)]
        for t in items:
            j = loads.index(min(loads))
            loads[j] += ms[t]
            ws[j].append(t)
        c = max(loads) + WORKER_MS * k
        if best is None or c < best[0]:
            best = (c, ws)
    return best[1]


# --------------------------------------------------------------------------- verification
def patched_texts(fcs):
    """{rel: new text or None (deleted)} for a patch, or None when it cannot be replayed exactly."""
    out = {}
    for fc in fcs:
        if fc.binary:
            return None
        old_rel = fc.rename_from or fc.old_path
        new_rel = fc.rename_to or fc.new_path
        for rel in (old_rel, new_rel):
            if rel and (rel.rsplit("/", 1)[-1] in PYTEST_FILES or rel in ("pyproject.toml", "setup.cfg")):
                return None
        if fc.is_deleted:
            out[old_rel] = None
            continue
        old_text = "" if fc.is_new else read_old(old_rel)
        if old_text is None:
            old_text = ""
        new_text, ok = apply_hunks(old_text, fc.hunks) if fc.hunks else (old_text, True)
        if not ok:
            return None
        if old_rel and new_rel and old_rel != new_rel and not fc.is_new:
            out[old_rel] = None
        out[new_rel or old_rel] = new_text
    return out


def _load_json(name, default):
    try:
        with open(os.path.join(MAPS, name), encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return default


CPU = _load_json("cpu.json", {"tests": {}, "file_overhead": {}})
CPU_SCALE = float(os.environ.get("BLAST_CPU_SCALE", "1.15"))


FORK = hasattr(os, "fork") and os.environ.get("BLAST_FORK", "1") == "1"
HEAVY_FILES = {"tests/test_executor.py", "tests/test_optimizer.py"}  # import pandas / duckdb
FORK_SAVE = float(os.environ.get("BLAST_FORK_SAVE", "0.3"))  # interpreter + stdlib, paid once
HEAVY_SAVE = float(os.environ.get("BLAST_HEAVY_SAVE", "0.0"))  # pandas + numpy + duckdb (preloaded only when >= 2 heavy jobs)


def file_overhead(f):
    o = CPU["file_overhead"].get(f, 1.0)
    if FORK:
        o -= FORK_SAVE + (HEAVY_SAVE if f in HEAVY_FILES else 0.0)
    return max(o, 0.15)


def run_cost(tids):
    if not tids:
        return 0.0
    files = {t.split("::")[0] for t in tids}
    over = max(file_overhead(f) for f in files) + 0.08 * (len(files) - 1)
    return CPU_SCALE * (over + sum(CPU["tests"].get(t, 1.0) for t in tids))


def pack_cost(nums, ms):
    if not nums:
        return 0.0
    ws = split(nums, ms)
    return max(sum(ms[t] for t in w) for w in ws) / 1000 + 2 * len(ws)


def p_clean(patched):
    """Prior probability that a change breaks no test at all (given it reaches some test)."""
    q = 1.0
    for rel in patched:
        if rel.endswith(".py"):
            q *= Q_CODE
        else:
            q *= Q_DATA  # an edited fixture almost always changes an expected result
    return q


Q_CODE = float(os.environ.get("BLAST_Q_CODE", "0.45"))
Q_DATA = float(os.environ.get("BLAST_Q_DATA", "0.04"))


def order_tests(tids):
    """Classes cheapest first, tests cheapest first inside a class."""
    cpu = CPU["tests"]
    groups = {}
    for t in tids:
        groups.setdefault(t.rsplit("::", 1)[0], []).append(t)
    keys = sorted(groups, key=lambda k: sum(cpu.get(t, 1.0) for t in groups[k]))
    out = []
    for k in keys:
        out += sorted(groups[k], key=lambda t: cpu.get(t, 1.0))
    return out


MAX_TEST_CPU = float(os.environ.get("BLAST_MAX_TEST_CPU", "1.0"))  # never run a test heavier than this
MAX_JOB_CPU = float(os.environ.get("BLAST_MAX_JOB_CPU", "4.0"))  # nor a job heavier than this
MARGIN = float(os.environ.get("BLAST_MARGIN", "0.25"))  # expected saving must beat the cost by this much


def plan(cands, ms, num2id, q):
    """-> (tests to run, tests still worth running after a failure, estimated cpu).

    Only light tests are ever run (the scorer's CPU is dear and estimates are rough); heavy
    tests are included without running. Running pays off mainly when it can prove a commit
    clean, so a job runs only when q * (what proving it clean saves) clearly beats its cost."""
    if not cands:
        return [], [], 0.0
    cpu = CPU["tests"]
    heavy = [n for n in cands if cpu.get(num2id[n], 1.0) > MAX_TEST_CPU]
    light = [n for n in cands if cpu.get(num2id[n], 1.0) <= MAX_TEST_CPU]
    if not light:
        return [], [], 0.0
    run = [num2id[n] for n in light]
    rc_all = run_cost(run)
    if rc_all > MAX_JOB_CPU:
        return [], [], 0.0
    ordered = order_tests(run)
    base = pack_cost(cands, ms)
    clean_cost = rc_all + pack_cost(sorted(heavy), ms)
    fail_cost = run_cost(ordered[:1]) + base  # stops at the first failure
    e = q * clean_cost + (1 - q) * fail_cost
    if e < base - MARGIN:
        return ordered, [], rc_all
    return [], [], 0.0


id2num_g = {}


# --------------------------------------------------------------------------- main
def main():
    import time

    import runner

    t_start = time.monotonic()
    budget = float(os.environ.get("BLAST_BUDGET", "140"))
    verify = os.environ.get("BLAST_VERIFY", "1") == "1"

    with open(os.path.join(DATA, "tests.json"), encoding="utf-8") as f:
        tests = json.load(f)["tests"]
    ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    id2num = {t["id"]: t["number"] for t in tests}
    num2id = {t["number"]: t["id"] for t in tests}
    id2num_g.update(id2num)
    bit_num = [id2num.get(tid) for tid in M["tests"]]
    known = set(M["tests"])
    unknown = [t["number"] for t in tests if t["id"] not in known]
    all_nums = sorted(ms)

    cdir = os.path.join(DATA, "commits")
    commits = []
    for name in sorted(os.listdir(cdir)):
        if not name.endswith(".patch"):
            continue
        cid = name[: -len(".patch")]
        patched = None
        try:
            with open(os.path.join(cdir, name), "rb") as f:
                text = f.read().decode("utf-8", "surrogateescape")
            fcs = parse_patch(text)
            bits = 0
            for fc in fcs:
                bits |= file_bits(fc)
            nums = [bit_num[i] for i in range(NBITS) if bits >> i & 1 and bit_num[i] is not None]
            if bits:
                nums += unknown
            try:
                patched = patched_texts(fcs)
            except Exception:
                patched = None
        except Exception as e:  # never lose a commit: fall back to running everything
            log(f"{cid}: analysis failed ({e!r}); selecting every test")
            nums = all_nums
        nums = sorted(set(nums))
        run, after, est = [], [], 0.0
        if verify and patched is not None and nums and not unknown:
            run, after, est = plan(nums, ms, num2id, p_clean(patched))
        commits.append({"cid": cid, "cands": nums, "run": run, "after": after, "est": est, "patched": patched})

    def saving(c):
        return pack_cost(c["cands"], ms) - pack_cost(sorted(set(c["cands"]) - {id2num[t] for t in c["run"]}), ms) - c["est"]

    # most valuable verifications first, while the time budget lasts
    jobs = [c for c in commits if c["run"]]
    jobs.sort(key=lambda c: saving(c) / max(c["est"], 0.1), reverse=True)
    duck = os.path.join(MAPS, "duck.pkl")
    duck = duck if os.path.exists(duck) else None
    nworkers = int(os.environ.get("BLAST_JOBS", "2"))

    def job_of(c):
        return {"sg": SG, "patched": c["patched"], "tests": c["run"], "after_fail": c["after"], "duck": duck}

    def timeout_of(c, left):
        return min(max(20.0, 3 * c["est"] + 10), max(5.0, left - 5))

    if FORK and jobs:
        # preloading pandas / duckdb costs about as much as one heavy job saves by it
        heavy = sum(any(t.split("::")[0] in HEAVY_FILES for t in c["run"]) for c in jobs) >= 2
        fs = runner.ForkServer(heavy)
        queue = list(jobs)
        running = {}  # jid -> [commit, deadline, current test, start]
        committed = 0.0
        jid = 0
        while queue or running:
            while queue and len(running) < nworkers:
                c = queue.pop(0)
                left = budget - (time.monotonic() - t_start)
                if committed + c["est"] * 1.5 + 3 > left * nworkers:
                    log(f"{c['cid']}: verification skipped (time budget)")
                    c["res"] = {}
                    continue
                committed += c["est"]
                c["res"] = {}
                jid += 1
                fs.send({"id": jid, "job": job_of(c)})
                now = time.monotonic()
                running[jid] = [c, now + timeout_of(c, left), None, now]
            if not running:
                break
            msgs = fs.read(0.05)
            if msgs is None:  # the fork server died: whatever is unfinished stays unverified
                for j, (c, _d, cur, _s) in running.items():
                    if cur is not None and cur not in c["res"]:
                        c["res"][cur] = True
                running.clear()
                break
            for j, kind, tid in msgs:
                ent = running.get(j)
                if ent is None:
                    continue
                c = ent[0]
                if kind == "S":
                    ent[2] = tid
                elif kind in ("P", "F"):
                    c["res"][tid] = kind == "F"
                    ent[2] = None
                elif kind == "X":
                    if ent[2] is not None and ent[2] not in c["res"]:
                        c["res"][ent[2]] = True  # crashed or was killed inside this test
                    committed -= c["est"]
                    log(
                        f"{c['cid']}: ran {len(c['run'])} tests (est {c['est']:.1f}s) in {time.monotonic() - ent[3]:.1f}s, "
                        f"{sum(c['res'].values())} failed, {len(c['run']) - len(c['res'])} unfinished"
                    )
                    del running[j]
            now = time.monotonic()
            for j, ent in list(running.items()):
                if now > ent[1]:
                    fs.send({"kill": j})
                    ent[1] = now + 30  # the X line follows; ask again only if it does not
        fs.close()
    else:
        import threading
        from concurrent.futures import ThreadPoolExecutor

        lock = threading.Lock()
        committed_ = [0.0]

        def do(c):
            with lock:
                left = budget - (time.monotonic() - t_start)
                if committed_[0] + c["est"] * 1.5 + 3 > left * nworkers:
                    log(f"{c['cid']}: verification skipped (time budget)")
                    c["res"] = {}
                    return
                committed_[0] += c["est"]
            t0 = time.monotonic()
            c["res"] = runner.run_job(job_of(c), timeout_of(c, left))
            with lock:
                committed_[0] -= c["est"]
            log(
                f"{c['cid']}: ran {len(c['run'])} tests (est {c['est']:.1f}s) in {time.monotonic() - t0:.1f}s, "
                f"{sum(c['res'].values())} failed, {len(c['run']) - len(c['res'])} unfinished"
            )

        with ThreadPoolExecutor(max(1, nworkers)) as pool:
            list(pool.map(do, jobs))

    out = []
    for c in commits:
        res = c.get("res", {})
        sel = [n for n in c["cands"] if res.get(num2id[n], True)]
        ws = split(sel, ms)
        out.append(json.dumps({"commit": c["cid"], "workers": ws}, separators=(",", ":")))
        log(f"{c['cid']}: {len(c['cands'])} candidates, {len(sel)} selected, {len(ws)} workers")
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    sys.stdout.flush()
    log(f"total wall {time.monotonic() - t_start:.1f}s")


if __name__ == "__main__":
    main()
