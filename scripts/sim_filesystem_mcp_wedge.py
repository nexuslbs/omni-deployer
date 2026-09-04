#!/usr/bin/env python3
"""sim_filesystem_mcp_wedge.py - filesystem MCP whole-plugin wedge simulation + regression guard.

Reproduces (and, after the fix, guards against) the Sep 2026 outage where a
single UNBOUNDED recursive filesystem walk (filesystem_search / filesystem_grep
over a huge tree, e.g. path="/") wedged the WHOLE filesystem MCP server: every
subsequent filesystem call (read/list/info/write) timed out at 60s for entire
threads (threads 884/888, 2026-09-04).

How it works (matches the production trigger):
  1. Warmup: a normal filesystem_list + filesystem_read must succeed quickly.
  2. Pathological call: filesystem_search {path: "/", pattern: "**/zz_no_match_<pid>"}
     (a no-match glob forces the walk to scan the ENTIRE tree; pre-fix it never
     terminates and wedges the plugin; post-fix the walk is hard-bounded and
     returns quickly with a truncated note).
  3. Post-check: a normal filesystem_list must STILL succeed quickly (pre-fix it
     times out because the server is wedged; post-fix the server stays up).

Exit codes: 0 = healthy (post-fix expectation); 1 = wedge reproduced or any
check failed. Use --expect-wedge to assert the failure mode (pre-fix proof).

Call path: direct HTTP POST {BASE}/mcp/execute (same omniagent API the agent
threads use for external MCP tools; deterministic and CI-safe, no noop/mattermost
channel needed - mirrors G17c). BASE defaults to http://localhost:8080.
"""

import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("BASE", "http://localhost:8080")
WARM_TIMEOUT = 15   # normal calls must complete well under this
POST_TIMEOUT = 15   # post-check call must complete under this
PATHO_TIMEOUT = 20  # pathological call client cap (pre-fix it hangs past this)


def execute(name, arguments, timeout):
    d = json.dumps({"name": name, "arguments": arguments}).encode()
    req = urllib.request.Request(
        "{}/mcp/execute".format(BASE), data=d, method="POST",
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        ok = bool(resp.get("success")) and not resp.get("is_error", False)
        return (time.time() - t0, ok, str(resp)[:200])
    except Exception as e:
        return (time.time() - t0, False, str(e)[:200])


def main():
    expect_wedge = "--expect-wedge" in sys.argv
    pid = os.getpid()

    # 1. Warmup: normal calls must work.
    t, ok, detail = execute("filesystem_list", {"path": "/opt/workspace"}, WARM_TIMEOUT)
    print("[warmup filesystem_list] {:.1f}s ok={} {}".format(t, ok, detail))
    if not ok:
        print("WARMUP FAILED - filesystem tools unavailable (server down?): {}".format(detail))
        return 2
    t, ok, detail = execute("filesystem_read", {"path": "/opt/workspace/omni-deployer/omnidev.py", "limit": 300}, WARM_TIMEOUT)
    print("[warmup filesystem_read] {:.1f}s ok={} {}".format(t, ok, detail))
    if not ok:
        print("WARMUP READ FAILED: {}".format(detail))
        return 2

    # 2. Pathological: unbounded no-match glob over the whole filesystem.
    pat = "**/zz_no_match_{}".format(pid)
    t, ok, detail = execute("filesystem_search", {"path": "/", "pattern": pat}, PATHO_TIMEOUT)
    print("[pathological filesystem_search /] {:.1f}s ok={} {}".format(t, ok, detail))

    # 3. Post-check: normal call must still succeed (server not wedged).
    t, ok, detail = execute("filesystem_list", {"path": "/opt/workspace"}, POST_TIMEOUT)
    print("[post filesystem_list] {:.1f}s ok={} {}".format(t, ok, detail))

    if expect_wedge:
        # Pre-fix proof mode: assert the pathological call wedged the server.
        if not ok:
            print("WEDGE REPRODUCED (post-call failed/timed out after pathological search)")
            return 1
        print("NO WEDGE (post-call succeeded) - fix is effective")
        return 0

    # Regression-guard mode (runs in deploy integration): must be healthy.
    if not ok:
        print("REGRESSION FAIL: filesystem server wedged by one unbounded walk")
        return 1
    print("REGRESSION PASS: filesystem calls work, no whole-plugin wedge")
    return 0


if __name__ == "__main__":
    sys.exit(main())
