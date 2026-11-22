"""Static facts about the pinned checkout, written to maps/static.bin.

    python tools/prep.py <sqlglot_root> <out>

  subs   expression class -> itself and every subclass ("module:QualName")
  refs   identifier -> {file: [lines]} for every place inside a function body
         (def or lambda) under sqlglot/ where that name is read as  x.NAME  or  NAME
  modrefs identifier -> how many times it is read outside any function body
"""

import sys
import os
import ast
import pickle
import zlib

repo = os.path.realpath(sys.argv[1])
out = sys.argv[2]
sys.path.insert(0, repo)
os.chdir(repo)

import logging  # noqa: E402

logging.disable(logging.CRITICAL)
import sqlglot  # noqa: E402,F401
import sqlglot.expressions.core as core  # noqa: E402
from sqlglot.dialects.dialect import Dialect  # noqa: E402
from sqlglot.dialects import Dialects  # noqa: E402

for d in Dialects:
    if d.value:
        Dialect.get_or_raise(d.value)


def key(c):
    return f"{c.__module__}:{c.__qualname__}"


subs = {}


def walk(c):
    acc = {key(c)}
    for s in c.__subclasses__():
        acc |= walk(s)
    subs[key(c)] = sorted(acc)
    return acc


walk(core.Expr)

refs = {}
modrefs = {}  # identifier -> number of reads at module / class level (outside any function)
for root, _dirs, files in os.walk(os.path.join(repo, "sqlglot")):
    for fn in files:
        if not fn.endswith(".py"):
            continue
        p = os.path.join(root, fn)
        rel = os.path.relpath(p, repo).replace("\\", "/")
        tree = ast.parse(open(p, "rb").read())

        def visit(node, infunc):
            for child in ast.iter_child_nodes(node):
                inner = infunc or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                if infunc:
                    name = None
                    if isinstance(child, ast.Attribute):
                        name = child.attr
                    elif isinstance(child, ast.Name):
                        name = child.id
                    elif isinstance(child, ast.Constant) and isinstance(child.value, str) and child.value.isidentifier():
                        name = child.value  # getattr(x, "NAME") style access
                    if name:
                        refs.setdefault(name, {}).setdefault(rel, set()).add(child.lineno)
                else:
                    name = None
                    if isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load):
                        name = child.attr
                    elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                        name = child.id
                    if name:
                        modrefs[name] = modrefs.get(name, 0) + 1
                visit(child, inner)

        visit(tree, False)

refs = {n: {f: sorted(ls) for f, ls in d.items()} for n, d in refs.items()}
blob = zlib.compress(pickle.dumps({"subs": subs, "refs": refs, "modrefs": modrefs}, protocol=4), 9)
with open(out, "wb") as f:
    f.write(blob)
print(f"expression classes={len(subs)} names={len(refs)} bytes={len(blob)}")
