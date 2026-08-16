#!/usr/bin/env python3
"""Driver: run ONLY the GROUP 33 python-telegram-platform tests (mock Bot API)
against the live omnidev stack.

tests.py cannot be imported (module-level code references _args defined only
under __main__) and `python3 tests.py --group 33` aborts in the main body at
GROUP 12 (_check_mm_container expects compose project 'omnideploy', but the
dev stack runs as 'omnidev'). Instead, extract the GROUP 33 block verbatim
from tests.py and exec it with the shared test() harness + REMOTE_REPO. Runs
only the three 33_* tests — no other group's setup side effects.

The GROUP 33 block boots the omni-plugins python telegram platform
(platforms/telegram/platform.py) against the bundled MOCK Telegram Bot API
(platforms/telegram/tests/mock_telegram_api.py) via the api_base_url config
override — NO real bot token is used anywhere."""
import os
import sys
import time
import json
import uuid
import urllib.request
import subprocess  # G33 block references plain `subprocess.PIPE`

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
REMOTE_REPO = "/opt/workspace/omni-plugins"  # G33 block's TG_DIR root

# ── extract GROUP 33 block verbatim from tests.py ─────────────────────
src = open("/opt/workspace/omni-deployer/scripts/tests.py", encoding="utf-8").read()
start = src.index("# ── GROUP 33: Python Telegram Platform Plugin")
end = src.index("sys.exit(0 if tests_fail == 0 else 1)", start)
block = src[start:end]

ns = dict(globals())
exec(compile(block, "tests.py[GROUP33]", "exec"), ns)

print("\n" + "=" * 60)
print(f"GROUP 33 SUMMARY: run={tests_run} pass={tests_pass} fail={tests_fail}")
print("=" * 60)
sys.exit(1 if tests_fail else 0)
