#!/usr/bin/env python3
"""omni-root main/dev branch-sync check (operator rule, 2026-09-10, thread 1614).

The operator corrected the sync rules for the omni-root repo:

* **profile content** (``profiles/``: templates, skills, wikis, MEMORY) is
  synced **across BOTH omni-root branches** (``main`` + ``dev``);
* **services + compose files** are synced across BOTH branches **AND with the
  omni-stack repo**;
* **configs** (``config/*.yml``) are **independent per branch** and are never
  synced (production keeps the ``tasks.yml`` cron schedules and the ``telegram``
  plugin; the ``dev`` branch has neither).

This script enforces the first two rules (and the dev-side config shape) against
the local checkouts. It is a plain, dependency-free python check so it can run in
CI and inside the dev container:

    python3 tests/test_omni_root_branch_sync.py

Overrides: ``OMNI_ROOT_DIR`` (default /opt/workspace/omni-root), ``OMNI_STACK_DIR``
(default /opt/workspace/omni-stack), ``BRANCH_MAIN`` (default origin/main),
``BRANCH_DEV`` (default origin/dev).

Exit code 0 = rules hold, 1 = violation (or a required ref/checkout is missing).
"""

import os
import subprocess
import sys

OMNI_ROOT = os.environ.get("OMNI_ROOT_DIR", "/opt/workspace/omni-root")
OMNI_STACK = os.environ.get("OMNI_STACK_DIR", "/opt/workspace/omni-stack")
BRANCH_MAIN = os.environ.get("BRANCH_MAIN", "origin/main")
BRANCH_DEV = os.environ.get("BRANCH_DEV", "origin/dev")

FORBIDDEN_STACK_PREFIXES = ("config/", "profile/", "profiles/", "plugin/", "plugins/")

failures = []


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", repo] + list(args),
        capture_output=True,
        text=True,
    )


def git_ok(repo, *args):
    r = git(repo, *args)
    if r.returncode != 0:
        raise RuntimeError(f"git -C {repo} {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def ref_exists(repo, ref):
    return git(repo, "rev-parse", "--verify", "--quiet", ref).returncode == 0


def diff_paths(repo, ref_a, ref_b, paths):
    """Names of files under `paths` that differ between the two refs."""
    out = git_ok(repo, "diff", "--name-only", ref_a, ref_b, "--", *paths)
    return [line for line in out.splitlines() if line.strip()]


def compose_and_services(repo):
    """Compose file names at the repo root + top-level dirs that hold services."""
    tracked = git_ok(repo, "ls-tree", "--name-only", "HEAD").splitlines()
    compose = sorted(
        f
        for f in tracked
        if f.endswith((".yml", ".yaml")) and "compose" in f.lower()
    )
    dirs = [d for d in tracked if d in ("services", "scripts")]
    return compose, dirs


def blob_hash(repo, ref, path):
    r = git(repo, "rev-parse", f"{ref}:{path}")
    return r.stdout.strip() if r.returncode == 0 else None


def check_branches():
    if not os.path.isdir(os.path.join(OMNI_ROOT, ".git")):
        failures.append(f"not a git checkout: {OMNI_ROOT}")
        return
    for ref in (BRANCH_MAIN, BRANCH_DEV):
        if not ref_exists(OMNI_ROOT, ref):
            failures.append(
                f"{OMNI_ROOT}: ref '{ref}' missing - run `git fetch origin` first"
            )
    if failures:
        return

    # 1. profile content is shared by both branches
    drift = diff_paths(OMNI_ROOT, BRANCH_MAIN, BRANCH_DEV, ["profiles"])
    if drift:
        failures.append(
            "profile content differs between %s and %s (must be synced): %s"
            % (BRANCH_MAIN, BRANCH_DEV, ", ".join(drift[:10]))
        )
    else:
        print(f"ok profiles synced across {BRANCH_MAIN} and {BRANCH_DEV}")

    # 2. services + compose files are synced across both branches
    compose, dirs = compose_and_services(OMNI_ROOT)
    shared = compose + dirs
    drift = diff_paths(OMNI_ROOT, BRANCH_MAIN, BRANCH_DEV, shared)
    if drift:
        failures.append(
            "services/compose differ between %s and %s (must be synced): %s"
            % (BRANCH_MAIN, BRANCH_DEV, ", ".join(drift[:10]))
        )
    else:
        print(
            "ok services/compose synced across %s and %s (%s)"
            % (BRANCH_MAIN, BRANCH_DEV, ", ".join(shared) or "no paths")
        )

    # 3. services + compose files are synced with omni-stack
    if not os.path.isdir(os.path.join(OMNI_STACK, ".git")):
        failures.append(f"not a git checkout: {OMNI_STACK}")
        return
    stack_compose, stack_dirs = compose_and_services(OMNI_STACK)
    stack_shared = [p for p in shared if p in stack_compose + stack_dirs]
    if not stack_shared:
        failures.append("omni-root and omni-stack share no compose/service paths")
    for path in stack_shared:
        for ref in (BRANCH_MAIN, BRANCH_DEV):
            a = blob_hash(OMNI_ROOT, ref, path)
            b = blob_hash(OMNI_STACK, "HEAD", path)
            if a is None or b is None:
                continue
            if a != b:
                failures.append(
                    f"'{path}' differs between omni-root {ref} and omni-stack HEAD"
                )
    if not any("differs between omni-root" in f for f in failures):
        print(f"ok services/compose synced with omni-stack ({', '.join(stack_shared)})")

    # 4. configs stay independent: the dev branch carries neither the telegram
    #    plugin nor cron schedules (production keeps both), so the check only
    #    asserts the dev-side shape - it never requires configs to be equal.
    dev_plugins = git(
        OMNI_ROOT, "show", f"{BRANCH_DEV}:config/plugins.yml"
    ).stdout
    dev_tasks = git(OMNI_ROOT, "show", f"{BRANCH_DEV}:config/tasks.yml").stdout
    if dev_plugins and "  telegram:" in dev_plugins:
        failures.append(f"{BRANCH_DEV}: config/plugins.yml still declares telegram")
    else:
        print(f"ok {BRANCH_DEV} config/plugins.yml has no telegram plugin")
    if dev_tasks:
        head = dev_tasks.split("\n")[0].strip()
        if head.startswith("schedules:") and head != "schedules: []":
            failures.append(f"{BRANCH_DEV}: config/tasks.yml still has cron schedules")
        else:
            print(f"ok {BRANCH_DEV} config/tasks.yml has no cron schedules")

    # 5. omni-stack must not track config/profile(s)/plugin(s)
    tracked = git_ok(OMNI_STACK, "ls-files").splitlines()
    offenders = [
        f for f in tracked if f.startswith(FORBIDDEN_STACK_PREFIXES)
    ]
    if offenders:
        failures.append(
            "omni-stack tracks forbidden paths: " + ", ".join(offenders[:10])
        )
    else:
        print("ok omni-stack tracks no config/profile(s)/plugin(s) paths")


def main():
    check_branches()
    if failures:
        print("\nFAIL: omni-root branch sync / omni-stack hygiene violation")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nPASS: omni-root branch sync rules hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
