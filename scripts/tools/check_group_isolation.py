#!/usr/bin/env python3
"""Audit that `scripts/tests.py` groups are resumable / runnable in isolation.

Checks (static, no stack needed):

* DEFINITION TRAPS (fatal, exit 1): a `def`/`import`/pure constant bound at
  group-body level inside one `_seg_<key>()` and used by a different segment.
  Such a name only exists when the providing group ran, so a fresh-interpreter
  run (`--from-group N`, `--group N` after its prerequisites) hits NameError.
  Fix: `python3 scripts/tools/hoist_trapped_defs.py scripts/tests.py scripts/tests.py`.

* LIVE-STATE TRAPS (warning): a scratch variable holding a runtime value fetched
  by an earlier group (e.g. `admin_token`) read by a later group.  These cannot be
  hoisted; the groups that need them declare `requires` (see `--list`) and are
  documented as not resumable in isolation.

Exit code 0 = no definition traps (resume-safe), 1 = definition traps found.
Optional `--json` prints the report as JSON.
"""
import ast
import builtins
import json
import sys

from hoist_trapped_defs import (analyse, free_vars, hoist_safe, stmt_bound)  # noqa: F401


def main():
    args = [a for a in sys.argv[1:] if a != "--json"]
    path = args[0] if args else "scripts/tests.py"
    src = open(path).read()
    tree, segs, mod_provides, provides, stmt_of, trapped = analyse(src)

    order = list(segs)
    users = {}
    for key, fn in segs.items():
        for u in free_vars(fn):
            users.setdefault(u, set()).add(key)

    def_traps, state_traps = [], []
    for nm in sorted(trapped):
        if nm in mod_provides:
            # the name also exists at module level (e.g. a stdlib module that a
            # group body re-imports), so it is never a NameError: not a trap.
            continue
        st = stmt_of[nm]
        rec = {"name": nm, "provider": provides[nm],
               "line": st.lineno, "users": sorted(users.get(nm, set()) - {provides[nm]})}
        if hoist_safe(st):
            def_traps.append(rec)
        else:
            state_traps.append(rec)

    if "--json" in sys.argv:
        print(json.dumps({"definition_traps": def_traps,
                          "live_state_traps": state_traps}, indent=2))
    else:
        print("group isolation audit: %s" % path)
        print("segments: %d, trapped cross-group names: %d" % (
            len(segs), len(trapped)))
        print()
        print("DEFINITION TRAPS (fatal - a resume run raises NameError): %d" % len(def_traps))
        for r in def_traps:
            print("  %-28s def in %-12s line %-6d used by %s" % (
                r["name"], r["provider"], r["line"], ",".join(r["users"])[:60]))
        print()
        print("LIVE-STATE TRAPS (warning - runtime value from an earlier group): %d" % len(state_traps))
        for r in state_traps:
            print("  %-28s set in %-12s line %-6d used by %s" % (
                r["name"], r["provider"], r["line"], ",".join(r["users"])[:60]))
        print()
        if def_traps:
            print("FAIL: %d definition trap(s) - run scripts/tools/hoist_trapped_defs.py"
                  % len(def_traps))
        else:
            print("PASS: no definition traps - every group's helpers exist at module level")
    return 1 if def_traps else 0


if __name__ == "__main__":
    sys.exit(main())
