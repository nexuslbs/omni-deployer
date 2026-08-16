#!/usr/bin/env python3
"""Driver: run ONLY the GROUP 34 ssh-plugin tests against the live omnidev stack.

tests.py cannot be imported (module-level code references _args defined only
under __main__) and `python3 tests.py --group 34` aborts in the main body at
GROUP 12 (_check_mm_container expects compose project 'omnideploy', but the
dev stack runs as 'omnidev'). Instead, extract the GROUP 34 block verbatim
from tests.py and exec it with the shared test() harness + WORKSPACE. Runs
only the three 34_* tests — no other group's setup side effects.

The GROUP 34 block spawns the builtin mcp-server-ssh binary directly and
drives it over MCP JSON-RPC stdio against a LOCAL throwaway sshd on
127.0.0.1:<port> (or a fake ssh/scp shim when openssh-server is absent)."""
import os
import sys
import time
import json
import uuid
import urllib.request
import subprocess

# ── harness (same semantics as tests.py's test()) ─────────────────────
tests_run = 0
tests_pass = 0
tests_fail = 0
test_timings = []


def test(fn):
    global tests_run, tests_pass, tests_fail
    tests_run += 1
    name = fn.__name__.replace("test_", "Test ").replace("_", " ")
    print(f"\n--- {name} ", end="", flush=True)
    t0 = time.time()
    try:
        fn()
        print(f"✓ PASS ({time.time() - t0:.1f}s)", flush=True)
        tests_pass += 1
        test_timings.append((name, time.time() - t0))
    except Exception as e:
        print(f"✗ FAIL ({time.time() - t0:.1f}s): {e}", flush=True)
        import traceback
        traceback.print_exc()
        tests_fail += 1
        test_timings.append((name, time.time() - t0))


BASE = "http://localhost:8080"
WORKSPACE = "/opt/workspace/omni-stack"
REMOTE_REPO = "/opt/workspace/omni-plugins"

# ── extract GROUP 34 block verbatim from tests.py ─────────────────────
src = open("/opt/workspace/omni-deployer/scripts/tests.py", encoding="utf-8").read()
start = src.index("# ── GROUP 34:")
end = src.index("sys.exit(0 if tests_fail == 0 else 1)", start)
block = src[start:end]

ns = dict(globals())
exec(compile(block, "tests.py[GROUP34]", "exec"), ns)

print("\n" + "=" * 60)
print(f"GROUP 34 SUMMARY: run={tests_run} pass={tests_pass} fail={tests_fail}")
print("=" * 60)
sys.exit(1 if tests_fail else 0)
