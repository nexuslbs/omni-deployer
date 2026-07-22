#!/usr/bin/env python3
"""
OmniAgent deployer — orchestration + integration tests.

Single entry point for deploying the OmniAgent stack and running the
full integration test suite. Handles env generation, Docker Compose
lifecycle, database setup, migrations, and test execution.

Usage:
    python3 deploy.py local     # Local dev mode (builds from source)
    python3 deploy.py ci        # CI mode (uses pre-built images)
    python3 deploy.py test      # Just run tests (stack must already be up)
"""

import argparse
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
import uuid


# ═══════════════════════════════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════════════════════════════

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", "/opt/workspace")
OMNI_STACK_DIR = os.path.join(WORKSPACE_DIR, "omni-stack")
OMNI_ENV_PATH = os.path.join(SCRIPT_DIR, "omni.env")
CONTAINER_NAME = "omnidev-omniagent-1"
TESTS_SCRIPT = os.path.join(SCRIPT_DIR, "scripts", "tests.py")


# ═══════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def compose_cmd(mode):
    cmd = ["docker", "compose", "-f", os.path.join(OMNI_STACK_DIR, "docker-compose.yml")]
    if mode == "local":
        cmd += ["-f", os.path.join(OMNI_STACK_DIR, "docker-compose.dev.yml")]
    return cmd


def run_compose(cmd_parts, *args):
    full = list(cmd_parts) + ["--env-file", OMNI_ENV_PATH] + list(args)
    return subprocess.run(full, capture_output=True, text=True)


def run_compose_check(cmd_parts, *args, label=""):
    r = run_compose(cmd_parts, *args)
    if r.returncode != 0:
        print(r.stdout[-1000:] if r.stdout else "")
        print(r.stderr[-1000:] if r.stderr else "")
        raise RuntimeError(f"{label or 'docker compose'} failed (exit={r.returncode})")
    return r


def wait_for_db(compose, service, user, db, label="db"):
    for i in range(30):
        r = run_compose(compose, "exec", "-T", service, "pg_isready", "-U", user, "-d", db)
        if r.returncode == 0:
            print(f"  {label} is healthy")
            return
        time.sleep(2)
    raise RuntimeError(f"{label} did not become healthy after 60s")


# ═══════════════════════════════════════════════════════════════════════
#  Orchestration
# ═══════════════════════════════════════════════════════════════════════

def generate_env(mode):
    p1 = os.urandom(24).hex()
    p2 = os.urandom(24).hex()

    with open(OMNI_ENV_PATH, "w") as f:
        f.write("COMPOSE_PROJECT_NAME=omnidev\n")
        f.write("COMPOSE_PROFILES=mattermost,noop\n")
        f.write("POSTGRES_PASSWORD=%s\n" % p1)
        f.write("MM_POSTGRES_PASSWORD=%s\n" % p2)

    if mode == "ci":
        for var in ["OMNIAGENT_IMAGE", "DASHBOARD_IMAGE", "TOOLBOX_IMAGE"]:
            val = os.environ.get(var)
            if not val:
                raise RuntimeError(f"CI mode requires {var} env var")
            f.write(f"{var}={val}\n")

    print(f"[deploy] Generated {OMNI_ENV_PATH}")


def deploy(mode):
    if not os.path.isdir(OMNI_STACK_DIR):
        raise RuntimeError(f"omni-stack not found at {OMNI_STACK_DIR}")

    generate_env(mode)
    compose = compose_cmd(mode)

    # Step 1: Stop + remove volumes
    print("[deploy] Stopping and removing volumes...")
    run_compose(compose, "down", "-v")

    # Step 2 (local): Build
    if mode == "local":
        print("[deploy] Building omniagent...")
        run_compose_check(compose, "build", "omniagent", label="omniagent build")
        print("[deploy] Building dashboard...")
        run_compose_check(compose, "build", "dashboard", label="dashboard build")

    # Step 3: Start DBs
    print("[deploy] Starting databases...")
    run_compose_check(compose, "up", "-d", "postgres", "mattermost-db", label="db start")

    # Step 4: Wait for DB health
    print("[deploy] Waiting for databases...")
    wait_for_db(compose, "postgres", "omniagent", "omniagent", "postgres")
    wait_for_db(compose, "mattermost-db", "mmuser", "mattermost", "mattermost-db")

    # Step 5: Migrate
    print("[deploy] Running migrations...")
    r = run_compose(compose, "run", "--rm", "omniagent", "test", "-f", "/app/target/release/db-migrations")
    if r.returncode == 0:
        run_compose_check(compose, "run", "--rm", "omniagent", "/app/target/release/db-migrations", label="migrations")
    else:
        run_compose_check(compose, "run", "--rm", "omniagent", "cargo", "run", "--release", "-p", "db-migrations", label="migrations (cargo)")

    # Step 6: Start all
    print("[deploy] Starting all services...")
    run_compose_check(compose, "up", "-d", label="services start")

    # Step 7: Wait for agent
    print("[deploy] Waiting for omniagent...")
    for i in range(60):
        try:
            r = urllib.request.urlopen("http://localhost:8080/api/health", timeout=5)
            if r.status == 200:
                print("  omniagent is ready")
                break
        except Exception:
            pass
        if i == 59:
            rc = run_compose(compose, "logs", "--tail=30", "omniagent")
            print(rc.stdout[-2000:])
            raise RuntimeError("omniagent did not become healthy")
        time.sleep(2)

    time.sleep(3)

    # Step 8: Tests (2 passes)
    for pass_num in [1, 2]:
        print(f"\n{'=' * 60}")
        print(f"  INTEGRATION TESTS — PASS {pass_num}")
        print(f"{'=' * 60}")
        run_tests()

    print(f"\n{'=' * 60}")
    print("  ALL TESTS PASSED")
    print(f"{'=' * 60}")


def run_tests():
    """Run integration tests via tests.py inside the omniagent container."""
    if not os.path.exists(TESTS_SCRIPT):
        raise RuntimeError(f"Tests script not found: {TESTS_SCRIPT}")

    print(f"  Running: docker exec {CONTAINER_NAME} python3 -u {TESTS_SCRIPT}")
    r = subprocess.run(
        ["docker", "exec", "-e", "PYTHONUNBUFFERED=1", CONTAINER_NAME, "python3", "-u", TESTS_SCRIPT],
    )
    if r.returncode != 0:
        raise RuntimeError(f"Tests failed (exit={r.returncode})")


# ═══════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="OmniAgent deployer")
    parser.add_argument(
        "mode",
        choices=["local", "ci", "test"],
        help="local=build from source, ci=use pre-built images, test=run tests only",
    )
    args = parser.parse_args()

    if args.mode == "test":
        run_tests()
    else:
        deploy(args.mode)


if __name__ == "__main__":
    main()
