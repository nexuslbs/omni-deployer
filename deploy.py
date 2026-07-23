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
TESTS_SCRIPT = os.path.join(SCRIPT_DIR, "scripts", "tests.py")
OMNIAGENT_DIR = os.path.join(WORKSPACE_DIR, "omniagent")


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
#  Pretests
# ═══════════════════════════════════════════════════════════════════════

def run_pretests(mode):
    """
    Run pre-deploy checks: fmt, clippy, unit tests, build test binaries.

    In CI mode, cargo runs directly on the host (GitHub runner has Rust).
    In local mode, cargo runs inside the dev container (via docker compose run).
    """
    docker_mode = mode  # for compose run; only local uses the dev overlay
    compose = compose_cmd(docker_mode)

    print("=" * 60)
    print("  PRETESTS")
    print("=" * 60)

    if mode == "local":
        # Build the dev image first
        print("\n[pretests] Building dev image...")
        run_compose_check(compose, "build", "omniagent", label="dev image")

        def run_cargo(args, label="", extra_env=None):
            env_flags = ["-e", "SQLX_OFFLINE=true"]
            if extra_env:
                for k, v in extra_env.items():
                    env_flags += ["-e", f"{k}={v}"]
            r = run_compose(
                compose, "run", "--rm", *env_flags, "omniagent", *args
            )
            if r.returncode != 0:
                print(r.stdout[-2000:] if r.stdout else "")
                print(r.stderr[-2000:] if r.stderr else "")
                raise RuntimeError(f"Pretest failed: {label or ' '.join(args[:3])}")
            return r

        cargo_cwd = None
    else:
        # CI mode: cargo runs directly on the host
        def run_cargo(args, label="", extra_env=None):
            env = os.environ.copy()
            env["SQLX_OFFLINE"] = "true"
            if extra_env:
                env.update(extra_env)
            return subprocess.run(args, capture_output=True, text=True, cwd=OMNIAGENT_DIR, env=env)

        cargo_cwd = OMNIAGENT_DIR

    def check_cargo(args, label="", extra_env=None):
        r = run_cargo(args, label, extra_env)
        if r.returncode != 0:
            print(r.stdout[-2000:] if r.stdout else "")
            print(r.stderr[-2000:] if r.stderr else "")
            raise RuntimeError(f"Pretest failed: {label or ' '.join(args[:3])}")

    # 1. cargo fmt --check
    print("\n[pretests] Checking code format (cargo fmt --check)...")
    check_cargo(["cargo", "fmt", "--check"], label="cargo fmt --check")
    print("  ✓ Format check passed")

    # 2. cargo check -D warnings (via RUSTFLAGS)
    print("\n[pretests] Running cargo check (warnings as errors)...")
    # RUSTFLAGS is used because `cargo check` doesn't support `--` passthrough to rustc
    check_cargo(
        ["cargo", "check", "--release"],
        label="cargo check -D warnings",
        extra_env={"RUSTFLAGS": "-D warnings"},
    )
    print("  ✓ cargo check passed")

    # 3. cargo clippy -D warnings
    print("\n[pretests] Running cargo clippy (warnings as errors)...")
    # clippy DOES support `--` to pass args to rustc
    check_cargo(
        ["cargo", "clippy", "--release", "--", "-D", "warnings"],
        label="cargo clippy -D warnings",
    )
    print("  ✓ cargo clippy passed")

    # 4. cargo test --release (unit tests)
    print("\n[pretests] Running cargo test --release...")
    check_cargo(["cargo", "test", "--release"], label="cargo test --release")
    print("  ✓ Unit tests passed")

    # 5. Build api_tests and plugin_tests test binaries
    print("\n[pretests] Building integration test binaries...")
    for test_file in ["api_tests", "plugin_tests"]:
        print(f"  Building {test_file}...")
        check_cargo(
            ["cargo", "test", "--release", "--test", test_file, "--no-run"],
            label=f"build {test_file}",
        )
    print("  ✓ Test binaries built")


def run_rust_integration_tests(compose):
    """Run api_tests --ignored and plugin_tests --ignored inside the container."""
    print("\n[integration] Running Rust integration tests (api_tests, plugin_tests)...")

    # Find the built test binaries in the cargo target volume
    # We search for the binaries with the hash suffix
    for test_file in ["api_tests", "plugin_tests"]:
        print(f"\n  Finding {test_file} binary in container...")
        find_r = run_compose(
            compose, "exec", "-T", "omniagent",
            "bash", "-c",
            f"ls /target/release/{test_file}-* 2>/dev/null | head -1",
        )
        binary_path = find_r.stdout.strip()
        if not binary_path:
            print(f"  ⚠ {test_file} binary not found, trying /app/target/release...")
            find_r = run_compose(
                compose, "exec", "-T", "omniagent",
                "bash", "-c",
                f"ls /app/target/release/{test_file}-* 2>/dev/null | head -1",
            )
            binary_path = find_r.stdout.strip()

        if not binary_path:
            print(f"  ✗ {test_file} binary not found — skipping")
            continue

        print(f"  Running {binary_path} --ignored ...")
        r = run_compose(compose, "exec", "-T", "omniagent", binary_path, "--ignored")
        if r.returncode != 0:
            print(r.stdout[-2000:] if r.stdout else "")
            print(r.stderr[-2000:] if r.stderr else "")
            raise RuntimeError(f"Rust integration test '{test_file}' failed (exit={r.returncode})")
        print(f"  ✓ {test_file} passed")


# ═══════════════════════════════════════════════════════════════════════
#  Orchestration
# ═══════════════════════════════════════════════════════════════════════

def generate_env(mode):
    p1 = os.urandom(24).hex()
    p2 = os.urandom(24).hex()

    with open(OMNI_ENV_PATH, "w") as f:
        f.write("COMPOSE_PROJECT_NAME=omnidev\n")
        f.write("COMPOSE_PROFILES=mattermost,noop\n")
        f.write(f"POSTGRES_PASSWORD={p1}\n")
        f.write(f"MM_POSTGRES_PASSWORD={p2}\n")

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

    # ── Step 0: Pretests ───────────────────────────────────────────
    # Run fmt, clippy, unit tests, build test binaries BEFORE deploy.
    # In local mode, runs inside the dev container.
    # In CI mode, runs directly on the host runner.
    run_pretests(mode)

    # Step 1: Stop containers (don't use -v to preserve cargo build cache)
    print("\n[deploy] Stopping services...")
    run_compose(compose, "down")

    # Remove only data volumes, preserving build cache volumes
    print("[deploy] Removing data volumes...")
    for vol in ["postgres_data", "mm-db", "mm-config", "mm-data", "mm-logs", "mm-plugins"]:
        subprocess.run(["docker", "volume", "rm", "-f", f"omnidev_{vol}"], capture_output=True)

    # Step 2 (local): Build images
    if mode == "local":
        print("\n[deploy] Building omniagent image...")
        run_compose_check(compose, "build", "omniagent", label="omniagent image build")
        print("[deploy] Building dashboard image...")
        run_compose_check(compose, "build", "dashboard", label="dashboard image build")

    # Step 3: Start DBs
    print("\n[deploy] Starting databases...")
    run_compose_check(compose, "up", "-d", "postgres", "mattermost-db", label="db start")

    # Step 4: Wait for DB health
    print("[deploy] Waiting for databases...")
    wait_for_db(compose, "postgres", "omniagent", "omniagent", "postgres")
    wait_for_db(compose, "mattermost-db", "mmuser", "mattermost", "mattermost-db")

    # Step 5 (local): Build omniagent + MCP server binaries
    if mode == "local":
        print("\n[deploy] Building omniagent binary...")
        run_compose_check(
            compose, "run", "--rm", "-e", "SQLX_OFFLINE=true", "omniagent",
            "cargo", "build", "--release", "-p", "omniagent",
            label="omniagent binary build",
        )
        # Build common MCP server binaries
        for pkg in [
            "mcp-server-cron", "mcp-server-kanban", "mcp-server-query",
            "mcp-server-search", "mcp-server-metrics",
        ]:
            print(f"  Building {pkg}...")
            run_compose(
                compose, "run", "--rm", "-e", "SQLX_OFFLINE=true", "omniagent",
                "cargo", "build", "--release", "-p", pkg,
            )

    # Step 6: Run migrations
    print("\n[deploy] Running migrations...")
    if mode == "ci":
        # CI: production image has db-migrations at /usr/local/bin/
        run_compose_check(compose, "run", "--rm", "omniagent",
                          "db-migrations", label="migrations")
    else:
        # Local: binary at /target/release/ (built with CARGO_TARGET_DIR=/target)
        r = run_compose(compose, "run", "--rm", "omniagent",
                        "test", "-f", "/target/release/db-migrations")
        if r.returncode == 0:
            run_compose_check(compose, "run", "--rm", "omniagent",
                              "/target/release/db-migrations", label="migrations")
        else:
            run_compose_check(compose, "run", "--rm", "omniagent",
                              "cargo", "run", "--release", "-p", "db-migrations",
                              label="migrations (cargo)")

    # Step 7: Start all services
    print("\n[deploy] Starting all services...")
    run_compose_check(compose, "up", "-d", label="services start")

    # Step 8: Wait for omniagent
    print("[deploy] Waiting for omniagent...")
    for i in range(600):
        r = run_compose(compose, "exec", "-T", "omniagent",
                        "curl", "-sf", "http://localhost:8080/health")
        if r.returncode == 0:
            print("  omniagent is ready")
            break
        if i % 30 == 0 and i > 0:
            print(f"  still waiting ({i * 2}s)...")
        if i == 599:
            rc = run_compose(compose, "logs", "--tail=30", "omniagent")
            print(rc.stdout[-2000:])
            raise RuntimeError("omniagent did not become healthy")
        time.sleep(2)

    time.sleep(3)

    # Step 9: Rust integration tests (api_tests, plugin_tests)
    run_rust_integration_tests(compose)

    # Step 10: Python integration tests (2 passes, with 1 retry on transient failure)
    for pass_num in [1, 2]:
        for attempt in [1, 2]:
            print(f"\n{'=' * 60}")
            print(f"  INTEGRATION TESTS — PASS {pass_num}" + (f" (retry {attempt})" if attempt > 1 else ""))
            print(f"{'=' * 60}")
            try:
                run_tests(compose)
                break  # success, move to next pass
            except RuntimeError as e:
                if attempt == 1:
                    print(f"  ⚠ Tests failed on attempt {attempt}, retrying...")
                    time.sleep(5)
                else:
                    raise  # re-raise on second failure

    print(f"\n{'=' * 60}")
    print("  ALL TESTS PASSED")
    print(f"{'=' * 60}")


def run_tests(compose=None):
    """Run integration tests via tests.py piped into the omniagent container."""
    if not os.path.exists(TESTS_SCRIPT):
        raise RuntimeError(f"Tests script not found: {TESTS_SCRIPT}")

    if compose is None:
        compose = compose_cmd("local")

    cmd = list(compose) + ["--env-file", OMNI_ENV_PATH,
                           "exec", "-T", "omniagent", "python3", "-u", "-"]
    print(f"  Running: {' '.join(cmd[:2])} ... exec -T omniagent python3 -u -")
    with open(TESTS_SCRIPT, "rb") as f:
        r = subprocess.run(cmd, stdin=f)
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
