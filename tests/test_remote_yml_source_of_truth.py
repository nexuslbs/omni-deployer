#!/usr/bin/env python3
"""Integration test: remote.yml is the SOURCE OF TRUTH for remote plugins.

Regression guard for the dashboard Platforms page (workstream 1 of
task_omnidev_omni_root_main_dev_branch_split).

Operator requirement (2026-09-10): for remote plugins, ``config/remote.yml`` is
the source of truth. It must NOT matter whether the plugin repository is cloned
locally or whether the plugin is listed in ``config/plugins.yml``: a plugin
declared in ``remote.yml`` must ALWAYS show up in the plugin listing
(``GET /api/plugins``), which is what the dashboard Platforms page renders.
Activating it there adds it to ``plugins.yml`` (unchanged behaviour).

Bug this test pins down: ``list_plugins`` only produced entries for
``plugins.yml`` entries plus discovered on-disk sources, so a plugin that
existed ONLY in ``remote.yml`` silently disappeared from the listing.

The test:
  1. injects a uniquely-named platform entry into ``config/remote.yml``
     (no matching ``plugins.yml`` entry, nothing cloned on disk),
  2. queries ``GET /api/plugins`` and asserts the entry IS listed with
     ``needs_download=true`` and the ``remote.yml`` path metadata,
  3. asserts the entry was NOT added to ``plugins.yml``,
  4. restores ``remote.yml`` / ``plugins.yml`` (finally).

It reproduces the bug (fails) on the broken build and passes on the fixed one.
No restart is needed: the plugin listing re-reads the YAML files on every call.

Running
-------
Against the live omnidev dev stack. Either from the host (docker CLI needed):

    python3 tests/test_remote_yml_source_of_truth.py            # OMNIDEV_CONTAINER auto
    OMNIDEV_CONTAINER=omnidev-omniagent-1 OMNIDEV_OMNI_DIR=/opt/workspace/omni-root \\
        python3 tests/test_remote_yml_source_of_truth.py

or from inside the agent container (curl + the bind-mounted data dir):

    docker exec -i omnidev-omniagent-1 python3 - \\
        < tests/test_remote_yml_source_of_truth.py

Exit code 0 = PASS, 1 = FAIL. Stdlib only.
"""

import json
import os
import shutil
import subprocess
import sys

# Unique on purpose: never collide with a real plugin.
NAME = "zz-ws1-remote-only-test"
SECTION = "platforms"
SUBPATH = "platforms/" + NAME
REMOTE_URL = "file:///tmp/omni-plugins"


def resolve_omni_dir():
    """Host data dir (omnidev) or the container's /opt/omni - same files."""
    env = os.environ.get("OMNIDEV_OMNI_DIR")
    if env:
        return env
    for cand in ("/opt/omni", "/opt/workspace/omni-root"):
        if os.path.isdir(os.path.join(cand, "config")):
            return cand
    raise RuntimeError("cannot locate the omni data dir (set OMNIDEV_OMNI_DIR)")


def api_get(path):
    """GET from the agent API: curl directly, or via docker exec when
    OMNIDEV_CONTAINER is set (host-side run)."""
    url = "http://localhost:8080" + path
    container = os.environ.get("OMNIDEV_CONTAINER")
    if container is None and os.path.isdir("/app"):  # running inside the agent container
        container = None
    cmd = ["docker", "exec", container, "curl", "-s", url] if container else ["curl", "-s", url]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError("API request failed: %s -- %s" % (" ".join(cmd), res.stderr.strip()))
    return res.stdout


def section_lines(lines, section):
    """True when a top-level `<section>:` header exists."""
    return any(line.rstrip("\n") == section + ":" for line in lines)


def add_remote_entry(path, section, name, url, subpath):
    """Insert `name` under the top-level `section:` mapping (append if absent).

    Text-level insertion keeps the file valid YAML (no duplicate top-level keys,
    which would make serde_yaml fall back to an empty store).
    """
    with open(path, encoding="utf-8") as handle:
        lines = handle.readlines()
    entry = ["  %s:\n" % name, "    url: %s\n" % url, "    path: %s\n" % subpath]
    out, inserted = [], False
    for line in lines:
        out.append(line)
        if not inserted and line.rstrip("\n") == section + ":":
            out.extend(entry)
            inserted = True
    if not inserted:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(section + ":\n")
        out.extend(entry)
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(out)


def main():
    omni_dir = resolve_omni_dir()
    remote_yml = os.path.join(omni_dir, "config", "remote.yml")
    plugins_yml = os.path.join(omni_dir, "config", "plugins.yml")
    for path in (remote_yml, plugins_yml):
        if not os.path.isfile(path):
            print("FAIL: %s not found (is the dev stack up?)" % path)
            return 1

    with open(remote_yml, encoding="utf-8") as handle:
        before_remote = handle.read()
    with open(plugins_yml, encoding="utf-8") as handle:
        before_plugins = handle.read()
    if NAME in before_remote or NAME in before_plugins:
        print("FAIL: stale test entry %r present - clean it up first" % NAME)
        return 1

    backup_remote = remote_yml + ".ws1test.bak"
    backup_plugins = plugins_yml + ".ws1test.bak"
    shutil.copy2(remote_yml, backup_remote)
    shutil.copy2(plugins_yml, backup_plugins)
    try:
        add_remote_entry(remote_yml, SECTION, NAME, REMOTE_URL, SUBPATH)
        with open(remote_yml, encoding="utf-8") as handle:
            after_remote = handle.read()
        assert NAME in after_remote, "test injection failed"

        body = api_get("/api/plugins")
        try:
            payload = json.loads(body)
        except ValueError:
            print("FAIL: /api/plugins did not return JSON: %r" % body[:200])
            return 1
        plugins = payload.get("data", payload if isinstance(payload, list) else [])
        hits = [p for p in plugins if p.get("name") == NAME]
        if not hits:
            print(
                "FAIL: %r is declared in config/remote.yml but is MISSING from "
                "GET /api/plugins -- remote.yml is not authoritative (the dashboard "
                "Platforms page would not show it)" % NAME
            )
            return 1
        hit = hits[0]
        if hit.get("needs_download") is not True:
            print("FAIL: %r listed but needs_download != true: %r" % (NAME, hit.get("needs_download")))
            return 1
        remote = hit.get("remote") or {}
        if remote.get("path") != SUBPATH or remote.get("url") != REMOTE_URL:
            print("FAIL: %r remote metadata mismatch: %r" % (NAME, remote))
            return 1

        with open(plugins_yml, encoding="utf-8") as handle:
            now_plugins = handle.read()
        if NAME in now_plugins:
            print("FAIL: listing a remote.yml-only plugin must not add it to plugins.yml")
            return 1
        print(
            "PASS: remote.yml-only plugin %r is listed (needs_download=true, "
            "remote=%s) without a plugins.yml entry" % (NAME, remote)
        )
        return 0
    finally:
        shutil.copy2(backup_remote, remote_yml)
        shutil.copy2(backup_plugins, plugins_yml)
        os.remove(backup_remote)
        os.remove(backup_plugins)
        with open(remote_yml, encoding="utf-8") as handle:
            restored_remote = handle.read()
        with open(plugins_yml, encoding="utf-8") as handle:
            restored_plugins = handle.read()
        assert restored_remote == before_remote, "remote.yml not restored"
        assert restored_plugins == before_plugins, "plugins.yml not restored"
        print("  [config/remote.yml + config/plugins.yml restored]")


if __name__ == "__main__":
    sys.exit(main())
