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

# Dialect components. For each Tokenizer / Parser / Generator / Dialect class: itself, its
# subclasses, and every dialect class that uses one of them (and that dialect's subclasses).
# A class attribute of such a class is read through an instance of one of these classes.
import sqlglot.tokens as tokens  # noqa: E402
import sqlglot.parser as parser_mod  # noqa: E402
import sqlglot.generator as generator_mod  # noqa: E402

csubs = {}


def cwalk(c):
    acc = {key(c)}
    for s in c.__subclasses__():
        acc |= cwalk(s)
    csubs[key(c)] = acc
    return acc


for base in (tokens.Tokenizer, parser_mod.Parser, generator_mod.Generator, Dialect):
    cwalk(base)

users_of = {}  # component class -> dialect classes whose tokenizer/parser/generator it is
for dkey in csubs[key(Dialect)]:
    mod, qual = dkey.split(":")
    obj = sys.modules[mod]
    for part in qual.split("."):
        obj = getattr(obj, part)
    for attr in ("tokenizer_class", "parser_class", "generator_class", "jsonpath_tokenizer_class"):
        c = getattr(obj, attr, None)
        if isinstance(c, type):
            users_of.setdefault(key(c), set()).add(dkey)

comp = {}
for ckey, sub in csubs.items():
    fam = set(sub)
    for s in sub:
        for d in users_of.get(s, ()):
            fam |= csubs.get(d, {d})
    comp[ckey] = sorted(fam)

refs = {}
modattr = {}  # attribute name -> [(file, line, text of the object it is read from)] at module level
modrefs = {}  # identifier -> number of reads at module / class level (outside any function)
import itertools  # noqa: E402

for root, _dirs, files in itertools.chain(os.walk(os.path.join(repo, "sqlglot")), os.walk(os.path.join(repo, "tests"))):
    if os.sep + "fixtures" in root:
        continue
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
                    if isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load):
                        # module / class level read of X.NAME: keep the text of X
                        modattr.setdefault(child.attr, []).append((rel, child.lineno, ast.unparse(child.value)))
                visit(child, inner)

        visit(tree, False)

refs = {n: {f: sorted(ls) for f, ls in d.items()} for n, d in refs.items()}
blob = zlib.compress(pickle.dumps({"subs": subs, "comp": comp, "refs": refs, "modrefs": modrefs, "modattr": modattr}, protocol=4), 9)
with open(out, "wb") as f:
    f.write(blob)
print(f"expression classes={len(subs)} names={len(refs)} bytes={len(blob)}")
