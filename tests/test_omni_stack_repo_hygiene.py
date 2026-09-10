#!/usr/bin/env python3
"""omni-stack repo hygiene regression check (operator rule, 2026-09-10, thread 1614).

The omni-stack repo must contain NO `config/`, `profile(s)/` or `plugin(s)/`
directories (only transiently during a run, never committed), and those paths
must NOT be gitignored - so if they ever appear transiently they are visible in
`git status` instead of silently becoming a de-facto part of the repo.

This check FAILS when:
  1. a forbidden top-level path is TRACKED in git (`git ls-files`);
  2. `.gitignore` lists a pattern that ignores a forbidden top-level path;
  3. with `--strict` (or OMNI_STACK_HYGIENE_STRICT=1): a forbidden path exists
     on disk without being tracked (a permanent runtime leftover).

Usage:
  test_omni_stack_repo_hygiene.py [--repo-dir DIR] [--strict]
  OMNI_STACK_DIR=DIR test_omni_stack_repo_hygiene.py

Exit code: 0 = clean, 1 = violation(s) found.
"""

import argparse
import os
import re
import subprocess
import sys

FORBIDDEN = ("config", "profile", "profiles", "plugin", "plugins")

GLOB_CHARS = "*?[]"


def git(repo_dir, *args):
    proc = subprocess.run(
        ["git", "-C", repo_dir, *args],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "git %s failed in %s: %s" % (" ".join(args), repo_dir, proc.stderr.strip())
        )
    return proc.stdout


def tracked_violations(repo_dir):
    """Return the tracked paths under a forbidden top-level directory."""
    out = git(repo_dir, "ls-files")
    bad = []
    for line in out.splitlines():
        path = line.strip()
        if not path:
            continue
        top = path.split("/", 1)[0]
        if top in FORBIDDEN:
            bad.append(path)
    return bad


def _pattern_targets_forbidden(pattern):
    """True when a .gitignore pattern hides a forbidden top-level path."""
    p = pattern.strip()
    if not p or p.startswith("#"):
        return None
    # Negations re-include content; they never hide it.
    if p.startswith("!"):
        return None
    if any(ch in p for ch in GLOB_CHARS):
        # A glob cannot name a forbidden path precisely; only flag globs that
        # would match every forbidden name (e.g. "*/config", "*/conf*").
        core = p.strip("/").strip()
        if core in ("*", "**"):
            return None
        if any(re.fullmatch(core.replace(".", r"\.").replace("*", ".*").replace("?", "."),
                            name)
               for name in FORBIDDEN):
            return p
        return None
    core = p.strip("/")
    if core in FORBIDDEN:
        return p
    # Patterns rooted deeper (build/config) do not hide the repo root paths.
    return None


def gitignore_violations(repo_dir):
    """Return the .gitignore patterns that hide a forbidden top-level path."""
    path = os.path.join(repo_dir, ".gitignore")
    bad = []
    if not os.path.isfile(path):
        return bad
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            hit = _pattern_targets_forbidden(line)
            if hit:
                bad.append(hit)
    return bad


def on_disk_violations(repo_dir, tracked):
    """Forbidden top-level paths present on disk but not tracked."""
    bad = []
    for name in FORBIDDEN:
        p = os.path.join(repo_dir, name)
        if not os.path.exists(p):
            continue
        if any(t.split("/", 1)[0] == name for t in tracked):
            continue
        bad.append(name + "/")
    return bad


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-dir",
        default=os.environ.get("OMNI_STACK_DIR", "/opt/workspace/omni-stack"),
        help="omni-stack checkout to inspect (default: $OMNI_STACK_DIR or "
        "/opt/workspace/omni-stack)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=os.environ.get("OMNI_STACK_HYGIENE_STRICT") == "1",
        help="also fail on untracked on-disk forbidden paths (no transient presence)",
    )
    args = parser.parse_args()

    repo_dir = os.path.abspath(args.repo_dir)
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        print("FAIL: %s is not a git checkout" % repo_dir)
        return 1

    tracked = git(repo_dir, "ls-files").splitlines()
    failures = []

    tracked_bad = tracked_violations(repo_dir)
    if tracked_bad:
        failures.append(
            "TRACKED forbidden paths (must not be part of omni-stack): %s"
            % ", ".join(sorted(tracked_bad)[:20])
        )

    ignore_bad = gitignore_violations(repo_dir)
    if ignore_bad:
        failures.append(
            ".gitignore hides forbidden paths (must stay visible to git status): %s"
            % ", ".join(ignore_bad)
        )

    disk_bad = on_disk_violations(repo_dir, tracked)
    if disk_bad:
        msg = "forbidden paths present on disk: %s" % ", ".join(disk_bad)
        if args.strict:
            failures.append("STRICT: %s" % msg)
        else:
            print("WARN (transient presence allowed during a run; use --strict to fail): %s" % msg)

    if failures:
        print("FAIL: omni-stack repo hygiene violation in %s" % repo_dir)
        for f in failures:
            print("  - %s" % f)
        return 1

    print(
        "PASS: omni-stack repo hygiene clean in %s "
        "(no tracked config/profile(s)/plugin(s) paths, no .gitignore entries for them)"
        % repo_dir
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
