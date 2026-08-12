#!/usr/bin/env python3
"""Driver: run ONLY the GROUP 27 hooks tests against the live omnidev API.

tests.py cannot be imported (module-level code references _args defined only
under __main__) and `python3 tests.py --group 27` aborts in the main body at
GROUP 12 (_check_mm_container expects compose project 'omnideploy', but the
dev stack runs as 'omnidev'). Instead, extract the GROUP 27 block verbatim
from tests.py and exec it with the shared test() harness + BASE. Runs only the
five 27_* tests — no other group's setup side effects."""
import os
import sys
import time
import json

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

# ── extract GROUP 27 block verbatim from tests.py ─────────────────────
src = open("/opt/workspace/omni-deployer/scripts/tests.py", encoding="utf-8").read()
start = src.index("# ═══════════════════════════════════════════════════════════════════════\n#  GROUP 27: Event-driven Hooks system")
end = src.index('print("TEST SUMMARY")')
block = src[start:end]

ns = dict(globals())
exec(compile(block, "tests.py[GROUP27]", "exec"), ns)

print("\n" + "=" * 60)
print(f"GROUP 27 SUMMARY: run={tests_run} pass={tests_pass} fail={tests_fail}")
print("=" * 60)
sys.exit(1 if tests_fail else 0)
