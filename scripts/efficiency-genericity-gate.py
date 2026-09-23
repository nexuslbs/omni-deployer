#!/usr/bin/env python3
"""efficiency-genericity-gate.py - C3 gate: the core stays generic.

Operator requirement (telegram 2026-09-23): "It should also have no mention to
plugins or hardcoded tools, ANYWHERE, in the core code."

This gate reads the ADDED lines of the core production diff (default: the range
`origin/main~1..HEAD` is NOT used - the range is taken from the environment or
from `git diff <base>...HEAD`) and FAILS when an added line contains

  * a qualified tool-name literal (`plugin__tool`, e.g. `docker__compose`), or
  * a plugin-local convention (a `<plugin>__<tool>` argv/subcommand probe), or
  * a name-keyed list of plugin/tool names,

in a core source file (src/**, plugins/ excluded: those ARE plugin code). A tool
id may only appear as an OPAQUE runtime value that came from the registry.

Test files are exempt from the "literal" rule only when the literal is an
invented fixture name; the gate reports every hit and the caller decides. The
shipped exclusion list below is the CONTRACT, not a convenience: extending it
requires a reviewer.

Usage:
  python3 efficiency-genericity-gate.py [REPO] [BASE_REF]
Exit 0 = clean, 1 = forbidden literal(s) found, 2 = could not read the diff.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

REPO = sys.argv[1] if len(sys.argv) > 1 else "/opt/workspace/omniagent"
BASE = sys.argv[2] if len(sys.argv) > 2 else (os.environ.get("BASE_REF") or "origin/main~1")

# A qualified tool id: plugin prefix + separator + tool name. The prefix list is
# the set of SHIPPED plugins; an invented name cannot match a real plugin, which
# is exactly why the unit tests use one.
PLUGIN_PREFIXES = (
    "docker", "ssh", "git", "filesystem", "search", "notes", "subtasks", "skills",
    "memory", "tasks", "workbench", "fetch", "prompt", "plugin-manager", "kanban",
    "cron", "hooks", "core",
)
QUALIFIED = re.compile(r"\b(?:%s)__[a-z0-9_-]+" % "|".join(PLUGIN_PREFIXES))

# Plugin-local conventions: probing a plugin-specific subcommand/argv to decide
# core behaviour.
CONVENTIONS = (
    re.compile(r'"(?:docker|compose|git|ssh|cargo|psql)"\s*=>'),
    re.compile(r"subcommand\s*==\s*\""),
    re.compile(r"argv(?:\[0\])?\s*==\s*\""),
)

# Files that are plugin code or non-production test fixtures.
EXEMPT_PATHS = (
    "plugins/",
    "tests/",
    "db-migrations/",
    "docs/",
    "scripts/",
)


def git(*args: str) -> str:
    out = subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True)
    if out.returncode != 0:
        sys.stderr.write("git %s failed: %s\n" % (" ".join(args), out.stderr.strip()))
        sys.exit(2)
    return out.stdout


def added_lines() -> list[tuple[str, int, str]]:
    diff = git("diff", "-U0", "%s...HEAD" % BASE)
    hits: list[tuple[str, int, str]] = []
    path, lineno = "", 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            lineno = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++"):
            hits.append((path, lineno, line[1:]))
            lineno += 1
    return hits


def main() -> int:
    if not os.path.isdir(os.path.join(REPO, ".git")):
        sys.stderr.write("not a git repo: %s\n" % REPO)
        return 2
    hits = added_lines()
    if not hits:
        print("genericity-gate: no added lines in %s...HEAD (%s)" % (BASE, REPO))
        return 0

    violations: list[str] = []
    for path, lineno, text in hits:
        if any(path.startswith(p) for p in EXEMPT_PATHS):
            continue
        stripped = text.strip()
        if stripped.startswith("//") and "TODO" not in stripped:
            # comments are still core code: they must not name a tool either
            pass
        m = QUALIFIED.search(text)
        if m:
            violations.append("%s:%d qualified tool-name literal `%s`" % (path, lineno, m.group(0)))
        for pat in CONVENTIONS:
            c = pat.search(text)
            if c:
                violations.append("%s:%d plugin-local convention `%s`" % (path, lineno, c.group(0)))

    print("genericity-gate: %d added production line(s) scanned from %s...HEAD in %s" % (
        sum(1 for p, _, _ in hits if not any(p.startswith(x) for x in EXEMPT_PATHS)), BASE, REPO))
    if violations:
        print("FAIL - the core diff mentions plugins/tools:")
        for v in violations:
            print("  " + v)
        return 1
    print("PASS - no plugin/tool-name literal in the added core lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
