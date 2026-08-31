#!/usr/bin/env python3
"""Push a release tag to the omni repos, verifying versions FIRST.

Fail-fast contract (checked BEFORE any push or tag):
  - omniagent version (Cargo.toml) == tag without the "v" prefix
  - omni-dashboard version (package.json) == tag without the "v" prefix
Any mismatch aborts with a clear error and a non-zero exit code before
anything is pushed or tagged.

Usage:
  python3 scripts/push-tag.py v0.1.3               # verify + tag + push
  python3 scripts/push-tag.py 0.1.3 --dry-run      # verify only, no git ops
"""
import argparse
import json
import os
import re
import subprocess
import sys

REPOS = ["omni-stack", "omniagent", "omni-dashboard", "omni-plugins"]


def read_cargo_version(cargo_toml):
    """Extract the workspace package version from Cargo.toml."""
    with open(cargo_toml, "r", encoding="utf-8") as f:
        text = f.read()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        raise ValueError(f"no version found in {cargo_toml}")
    return m.group(1)


def read_package_json_version(package_json):
    """Extract the version field from package.json."""
    with open(package_json, "r", encoding="utf-8") as f:
        data = json.load(f)
    version = data.get("version")
    if not version:
        raise ValueError(f"no version found in {package_json}")
    return version


def verify_versions(workspace, tag_version):
    """Fail fast: omniagent + omni-dashboard versions must equal the tag."""
    errors = []
    cargo_path = os.path.join(workspace, "omniagent", "Cargo.toml")
    pkg_path = os.path.join(workspace, "omni-dashboard", "package.json")

    try:
        agent_ver = read_cargo_version(cargo_path)
    except Exception as e:
        agent_ver = None
        errors.append(f"omniagent Cargo.toml: {e}")
    if agent_ver is not None and agent_ver != tag_version:
        errors.append(f"omniagent Cargo.toml version {agent_ver!r} != tag version {tag_version!r}")

    try:
        dash_ver = read_package_json_version(pkg_path)
    except Exception as e:
        dash_ver = None
        errors.append(f"omni-dashboard package.json: {e}")
    if dash_ver is not None and dash_ver != tag_version:
        errors.append(f"omni-dashboard package.json version {dash_ver!r} != tag version {tag_version!r}")

    if errors:
        print("ERROR: push-tag aborted, version mismatch:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        print(f"Fix the version file(s) to match tag {tag_version!r}, then retry.", file=sys.stderr)
        sys.exit(1)

    print(f"OK: omniagent {agent_ver} == omni-dashboard {dash_ver} == tag {tag_version}")


def main():
    parser = argparse.ArgumentParser(
        description="Push a release tag with fail-fast version verification."
    )
    parser.add_argument("tag", help="release tag, e.g. v0.1.3 or 0.1.3")
    parser.add_argument(
        "--workspace",
        default=os.environ.get("WORKSPACE_DIR", "/opt/workspace"),
        help="workspace containing the omni repos (default: $WORKSPACE_DIR or /opt/workspace)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="verify versions only; do not tag or push",
    )
    parser.add_argument(
        "--repos",
        nargs="*",
        default=REPOS,
        help="repos to tag and push (default: omni-stack omniagent omni-dashboard omni-plugins)",
    )
    args = parser.parse_args()

    tag = args.tag
    tag_version = tag[1:] if tag.startswith("v") else tag
    if not re.fullmatch(r"\d+\.\d+\.\d+", tag_version):
        print(f"ERROR: tag {tag!r} is not a valid vX.Y.Z release tag", file=sys.stderr)
        sys.exit(1)

    workspace = os.path.abspath(args.workspace)
    if not os.path.isdir(workspace):
        print(f"ERROR: workspace not found: {workspace}", file=sys.stderr)
        sys.exit(1)

    # ── 1. FAIL-FAST VERSION VERIFICATION (before anything else) ──
    verify_versions(workspace, tag_version)

    if args.dry_run:
        print("dry-run: verification passed, no tags created or pushed")
        return

    # ── 2. Tag + push ──
    for repo in args.repos:
        repo_dir = os.path.join(workspace, repo)
        git_dir = os.path.join(repo_dir, ".git")
        if not os.path.isdir(git_dir):
            print(f"WARN: skipping {repo}: not a git repo at {repo_dir}", file=sys.stderr)
            continue
        subprocess.run(["git", "-C", repo_dir, "tag", tag], check=True)
        subprocess.run(["git", "-C", repo_dir, "push", "origin", tag], check=True)
        print(f"tagged + pushed {repo}: {tag}")

    print("push-tag complete")


if __name__ == "__main__":
    main()
