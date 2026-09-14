#!/usr/bin/env python3
"""Hoist segment-trapped definitions in tests.py to module level.

WHY
---
`scripts/tests.py` is split into `_seg_<key>()` group functions by the group
isolation harness (F1).  A group body that binds a helper (`def _wf_cleanup()`)
or a constant binds it as a module GLOBAL (the transform declares `global` for
every name the block assigns), so later groups can use it - but only when the
providing group actually RAN.  A resume run (`--from-group N`) re-enters in a
FRESH interpreter and deliberately does not re-run earlier groups, so every
later group calling such a helper died with `NameError` (thread 1971 full run:
52x `_wf_cleanup`, 28x `tasks_yml_remove_keys`, 14x `_g24_mcp_execute`, ...).

WHAT
----
Move every trapped FUNCTION / IMPORT / pure CONSTANT binding out of its segment
body to module level, together with any other segment-body-level name those
definitions still reference.  At module level the names always exist, so every
group can run alone or be resumed into; when the providing group does run it
re-binds the same module global, so full-run semantics are unchanged.

Live-state scratch variables (values fetched from the API in an earlier group,
e.g. `MM` / `admin_token`) are deliberately NOT hoisted: they are runtime state,
not definitions.  `scripts/tools/check_group_isolation.py` lists them as
warnings so the boundary stays visible.

USAGE
-----
    python3 scripts/tools/hoist_trapped_defs.py scripts/tests.py scripts/tests.py
    python3 scripts/tools/check_group_isolation.py scripts/tests.py   # audit
"""
import ast
import builtins
import sys

# functions whose calls are safe to evaluate at import time (pure helpers)
_PURE_MODULES = ("json", "os.path", "sys", "time", "re")


def stmt_bound(st):
    """Names bound by a module/segment-body level statement."""
    out = set()
    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out.add(st.name)
    elif isinstance(st, ast.Assign):
        for t in st.targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    out.add(n.id)
    elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
        out.add(st.target.id)
    elif isinstance(st, ast.Import):
        for a in st.names:
            out.add(a.asname or a.name.split(".")[0])
    elif isinstance(st, ast.ImportFrom):
        for a in st.names:
            out.add(a.asname or a.name)
    else:
        for field in ("body", "orelse", "finalbody"):
            for sub in getattr(st, field, []) or []:
                out |= stmt_bound(sub)
        for h in getattr(st, "handlers", []) or []:
            for sub in h.body:
                out |= stmt_bound(sub)
    return out


def bound_direct(fn):
    """name -> the DIRECT child statement of the segment body that binds it."""
    out = {}
    for st in fn.body:
        for nm in stmt_bound(st):
            out.setdefault(nm, st)
    return out


def globals_decl(fn):
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Global):
            out.update(n.names)
    return out


def free_vars(node):
    """Names loaded in `node` and not bound inside it (imports count as bound)."""
    bound, loads = set(), set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for a in (node.args.args + node.args.kwonlyargs + node.args.posonlyargs):
            bound.add(a.arg)
        if node.args.vararg:
            bound.add(node.args.vararg.arg)
        if node.args.kwarg:
            bound.add(node.args.kwarg.arg)
    for n in ast.walk(node):
        if isinstance(n, ast.Import):
            for a in n.names:
                bound.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                bound.add(a.asname or a.name)
        elif isinstance(n, ast.Name):
            (bound if isinstance(n.ctx, ast.Store) else loads).add(n.id)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bound.update(n.names)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not node:
            bound.add(n.name)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, ast.comprehension):
            for t in ast.walk(n.target):
                if isinstance(t, ast.Name):
                    bound.add(t.id)
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
    return loads - bound


def _pure(v):
    if isinstance(v, (ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set,
                      ast.Name, ast.Starred, ast.JoinedStr)):
        return True
    if isinstance(v, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        return True
    if isinstance(v, ast.BinOp):
        return _pure(v.left) and _pure(v.right)
    if isinstance(v, ast.Call):
        f = v.func
        if isinstance(f, ast.Name):
            return f.id in ("dict", "list", "str", "int", "float")
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            return f.value.id in ("json", "os", "sys", "time", "re")
    return False


def hoist_safe(st):
    """Only pure definition statements may be hoisted (no live API call)."""
    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                       ast.Import, ast.ImportFrom)):
        return True
    return isinstance(st, ast.Assign) and _pure(st.value)


def analyse(src):
    tree = ast.parse(src)
    segs = {n.name: n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name.startswith("_seg_")}
    mod_provides = set(dir(builtins)) | {"__file__", "__name__"}
    for node in tree.body:
        mod_provides |= stmt_bound(node)
    provides, stmt_of = {}, {}
    for key, fn in segs.items():
        g = globals_decl(fn)
        for nm, st in bound_direct(fn).items():
            if nm in g or nm.startswith("_"):
                provides[nm] = key
                stmt_of[nm] = st
    users = {}
    for key, fn in segs.items():
        for u in free_vars(fn):
            users.setdefault(u, set()).add(key)
    trapped = {nm for nm, prov in provides.items()
               if users.get(nm, set()) - {prov}}
    return tree, segs, mod_provides, provides, stmt_of, trapped


def main():
    in_path, out_path = sys.argv[1], sys.argv[2]
    src = open(in_path).read()
    lines = src.split("\n")
    tree, segs, mod_provides, provides, stmt_of, trapped = analyse(src)

    hoist, queue = set(), sorted(trapped)
    while queue:
        nm = queue.pop()
        if nm in hoist or nm in mod_provides or nm not in stmt_of:
            continue
        hoist.add(nm)
        for dep in free_vars(stmt_of[nm]):
            if dep not in mod_provides and dep in stmt_of:
                queue.append(dep)

    move_stmts = {}
    for nm in sorted(hoist):
        st = stmt_of[nm]
        if not stmt_bound(st) <= hoist:
            print("SKIP (multi-name stmt) %s binds %s" % (nm, sorted(stmt_bound(st))))
            hoist.discard(nm)
            continue
        if not hoist_safe(st):
            print("SKIP (impure stmt, live state) %s line %d" % (nm, st.lineno))
            hoist.discard(nm)
            continue
        move_stmts[id(st)] = st

    residual = set()
    for nm in sorted(hoist):
        for fv in free_vars(stmt_of[nm]):
            if fv in mod_provides or fv in hoist or fv in builtins.__dict__:
                continue
            residual.add((nm, fv))

    print("trapped=%d hoisted=%d statements=%d" % (len(trapped), len(hoist), len(move_stmts)))
    for nm in sorted(hoist):
        print("  HOIST %s (from %s, lines %d-%d)" % (
            nm, provides[nm], stmt_of[nm].lineno, stmt_of[nm].end_lineno))
    for nm, fv in sorted(residual):
        print("  RESIDUAL %s needs %s" % (nm, fv))

    blocks = []
    for st in sorted(move_stmts.values(), key=lambda s: s.lineno):
        body = "\n".join(lines[st.lineno - 1:st.end_lineno])
        blocks.append("\n".join((ln[4:] if ln.startswith("    ") else ln)
                                for ln in body.split("\n")))
    hoist_block = "\n\n".join(blocks) if blocks else ""
    if not hoist_block:
        print("nothing to hoist")
        return 0

    drop = set()
    for st in move_stmts.values():
        drop.update(range(st.lineno, st.end_lineno + 1))
    out_lines = [ln for i, ln in enumerate(lines, 1) if i not in drop]

    marker = "#  GROUP ISOLATION HARNESS (F1, 2026-09-13)"
    insert_at = next((i - 1 for i, ln in enumerate(out_lines) if marker in ln), None)
    assert insert_at is not None, "harness marker not found"
    header = [
        "# ---------------------------------------------------------------------------",
        "#  HOISTED CROSS-GROUP DEFINITIONS (F3, 2026-09-14)",
        "#",
        "#  These helpers/constants used to be defined INSIDE a group body, so only a",
        "#  run in which that group executed could see them: a resume run",
        "#  (`--from-group N`) starts a fresh interpreter, the providing group never",
        "#  runs, and every later group calling them died with NameError (thread 1971:",
        "#  _wf_cleanup x52, tasks_yml_remove_keys x28, _g24_mcp_execute x14, ...).",
        "#  Defining them at module level makes every group runnable alone and from any",
        "#  starting group; a group that defines them itself re-binds the same module",
        "#  global when it runs, so full-run semantics are unchanged.",
        "#  Generated by scripts/tools/hoist_trapped_defs.py;",
        "#  audit with scripts/tools/check_group_isolation.py.",
        "# ---------------------------------------------------------------------------",
        "",
        hoist_block,
        "",
        "",
    ]
    out_lines[insert_at:insert_at] = header
    open(out_path, "w").write("\n".join(out_lines))
    print("wrote %s (%d -> %d lines)" % (out_path, len(lines), len(out_lines)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
