#!/usr/bin/env python3
"""
OmniAgent deployer — orchestration + integration tests.

Single entry point for deploying the OmniAgent stack and running the
full integration test suite. Handles env generation, Docker Compose
lifecycle, database setup, migrations, and test execution.

Usage:
    python3 deploy.py dev       # Dev mode (builds from source + shared tool tests)
    python3 deploy.py ci        # CI mode (uses pre-built images)
    python3 deploy.py test      # Just run tests (stack must already be up)
"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid

# Import shared.py for Phase 1 + Phase 2 tool tests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shared


# ═══════════════════════════════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════════════════════════════

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", "/opt/workspace")
OMNI_STACK_DIR = os.path.join(WORKSPACE_DIR, "omni-stack")
OMNI_ENV_PATH = os.path.join(SCRIPT_DIR, "omni.env")
TESTS_SCRIPT = os.path.join(SCRIPT_DIR, "scripts", "tests.py")
OMNIAGENT_DIR = os.path.join(WORKSPACE_DIR, "omniagent")
REMOTE_REPO = os.path.join(WORKSPACE_DIR, "omni-plugins")


# ═══════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def compose_cmd(mode):
    cmd = ["docker", "compose", "-f", os.path.join(OMNI_STACK_DIR, "docker-compose.yml")]
    if mode == "dev":
        cmd += ["-f", os.path.join(OMNI_STACK_DIR, "docker-compose.dev.yml")]
    # hybrid and ci use no dev overlay — just base compose
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
    In dev mode, cargo runs inside the dev container (via docker compose run).
    """
    docker_mode = mode  # for compose run; only dev uses the dev overlay
    compose = compose_cmd(docker_mode)

    print("=" * 60)
    print("  PRETESTS")
    print("=" * 60)

    if mode == "hybrid":
        # Hybrid: no separate pretests — the production Dockerfile's builder
        # stage runs fmt, check, clippy, and unit tests during `docker build`.
        print("\n[pretests] Skipping (run via production Dockerfile build)...")
        return

    if mode == "dev":
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
        # CI mode: cargo runs directly on the host (has cargo in CI environment)
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

    # 4. cargo test --release (unit tests only; integration tests are #[ignore] and run later)
    print("\n[pretests] Running cargo test --release...")
    check_cargo(["cargo", "test", "--release"], label="cargo test --release")
    print("  ✓ Unit tests passed")


def run_rust_integration_tests(compose, mode="dev"):
    """Run api_tests and plugin_tests via cargo test.

    Only works in dev mode where the dev image has the Rust toolchain
    and all build dependencies. In CI/hybrid mode, the production image
    is too minimal (no cargo, no libssl-dev, no pkg-config) to compile
    Rust code — Python integration tests cover end-to-end flows instead.
    """
    if mode != "dev":
        print("[integration] Skipping Rust integration tests (dev mode only — "
              "production image lacks build toolchain)")
        return

    print("\n[integration] Running Rust integration tests (api_tests, plugin_tests)...")

    for test_file in ["api_tests", "plugin_tests"]:
        print(f"\n  Running {test_file}...")
        extra_args = []
        if test_file == "plugin_tests":
            # Run sequentially to avoid parallel test interference:
            # multiple tests share the same "test-rust-tool" fixture
            extra_args = ["--test-threads=1"]
        r = run_compose(
            compose, "exec", "-T", "omniagent",
            "cargo", "test", "--release", "--test", test_file, "--", "--ignored",
            *extra_args,
        )

        # Print last 30 lines of output
        if r.stdout:
            lines = r.stdout.splitlines()
            print("\n".join(lines[-30:]))
        if r.returncode != 0:
            if r.stderr:
                lines = r.stderr.splitlines()
                print("\n".join(lines[-30:]), file=sys.stderr)
            raise RuntimeError(f"Rust integration test '{test_file}' failed (exit={r.returncode})")
        print(f"  ✓ {test_file} passed")


# ═══════════════════════════════════════════════════════════════════════
#  Orchestration
# ═══════════════════════════════════════════════════════════════════════

def generate_env(mode):
    p1 = os.urandom(24).hex()
    p2 = os.urandom(24).hex()

    with open(OMNI_ENV_PATH, "w") as f:
        f.write("COMPOSE_PROJECT_NAME=omnideploy\n")
        f.write("COMPOSE_PROFILES=mattermost,noop\n")
        f.write(f"POSTGRES_PASSWORD={p1}\n")
        f.write(f"MM_POSTGRES_PASSWORD={p2}\n")

        if mode == "ci":
            for var in ["OMNIAGENT_IMAGE", "DASHBOARD_IMAGE", "TOOLBOX_IMAGE"]:
                val = os.environ.get(var)
                if not val:
                    raise RuntimeError(f"CI mode requires {var} env var")
                f.write(f"{var}={val}\n")
        elif mode == "hybrid":
            f.write("OMNIAGENT_IMAGE=local/omniagent:latest\n")
            f.write("DASHBOARD_IMAGE=local/omni-dashboard:latest\n")
            f.write("TOOLBOX_IMAGE=local/omni-toolbox:latest\n")

    print(f"[deploy] Generated {OMNI_ENV_PATH}")

    # Seed remote.yml for test-rust-tool plugin (required by plugin_tests)
    remote_yml_path = os.path.join(OMNI_STACK_DIR, "remote.yml")
    remote_yml_content = """tools:
  test-rust-tool:
    url: https://github.com/nexuslbs/omni-plugins.git
    path: tools/test-rust-tool
"""
    # Ensure .remote/ directories are clean before seeding (prevents stale git state)
    for subdir in ["tools", "providers"]:
        remote_dir = os.path.join(OMNI_STACK_DIR, "plugins", subdir, ".remote")
        if os.path.isdir(remote_dir):
            subprocess.run(["rm", "-rf", remote_dir], capture_output=True)
    existing = ""
    if os.path.exists(remote_yml_path):
        r = subprocess.run(["cat", remote_yml_path], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            existing = r.stdout
    if existing.strip() != remote_yml_content.strip():
        import tempfile
        tmp = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".yml")
        tmp.write(remote_yml_content)
        tmp_path = tmp.name
        tmp.close()
        subprocess.run(["sudo", "cp", tmp_path, remote_yml_path], check=True)
        os.unlink(tmp_path)
        print(f"[deploy] Seeded {remote_yml_path}")
    else:
        print(f"[deploy] {remote_yml_path} unchanged")


def deploy(mode):
    if not os.path.isdir(OMNI_STACK_DIR):
        raise RuntimeError(f"omni-stack not found at {OMNI_STACK_DIR}")

    generate_env(mode)
    compose = compose_cmd(mode)

    # Step 0.5: Check for unstaged changes in omni-stack ──────────
    # The stack dir is bind-mounted into the container. Any files the
    # container writes (remote.yml, plugins.yml, etc.) persist on the
    # host. This catches unintended state leaks before they accumulate.
    # Auto-revert ALL known transient test artifacts:
    #   - plugins.yml, remote.yml (YAML state written by plugin API)
    #   - plugins/tools/, plugins/platforms/, plugins/providers/
    #     (test scripts may copytree() bundled plugins into these dirs)
    #   - actions.yml (dynamic action registration during tests)
    #   - any .remote/ directories (git clones for remote plugin tests)
    sh("cd /opt/workspace/omni-stack && "
       "sudo rm -rf plugins/tools/ plugins/platforms/ plugins/providers/ 2>/dev/null; "
       "git checkout HEAD -- . 2>/dev/null; "
       "true")
    r = sh("cd /opt/workspace/omni-stack && git status --porcelain")
    if r.stdout.strip():
        raise RuntimeError(
            "Unstaged changes detected in omni-stack after auto-cleanup. "
            "Review and commit or revert them before re-deploying.\n"
            + r.stdout
        )

    # ── Step 0 (hybrid): Stop old containers first ────────────────
    if mode == "hybrid":
        print("\n[deploy] Stopping old services...")
        run_compose(compose, "down")
        print("[deploy] Removing data volumes...")
        for vol in ["postgres_data", "mm-db", "mm-config", "mm-data", "mm-logs", "mm-plugins"]:
            subprocess.run(["docker", "volume", "rm", "-f", f"omnideploy_{vol}"], capture_output=True)

    # ── Step 0: Pretests ───────────────────────────────────────────
    # Run fmt, clippy, unit tests, build test binaries BEFORE deploy.
    # In local mode, runs inside the dev container.
    # In CI mode, runs directly on the host runner.
    # In hybrid mode, skipped — production Dockerfile handles them.
    run_pretests(mode)

    # Step 0b (hybrid): Build images like CI would
    if mode == "hybrid":
        print("\n[deploy] Building omniagent image (production Dockerfile)...")
        r = subprocess.run(
            ["docker", "build", "-t", "local/omniagent:latest",
             "-f", os.path.join(OMNIAGENT_DIR, "Dockerfile"),
             OMNIAGENT_DIR],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(r.stdout[-1000:] if r.stdout else "")
            print(r.stderr[-1000:] if r.stderr else "")
            raise RuntimeError("omniagent image build failed")

        print("[deploy] Building dashboard image...")
        dashboard_dir = os.path.join(WORKSPACE_DIR, "omni-dashboard")
        r = subprocess.run(
            ["docker", "build", "-t", "local/omni-dashboard:latest", dashboard_dir],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(r.stdout[-1000:] if r.stdout else "")
            print(r.stderr[-1000:] if r.stderr else "")
            raise RuntimeError("dashboard image build failed")

        print("[deploy] Building toolbox image...")
        toolbox_dir = os.path.join(OMNI_STACK_DIR, "services", "toolbox")
        r = subprocess.run(
            ["docker", "build", "-t", "local/omni-toolbox:latest", toolbox_dir],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(r.stdout[-1000:] if r.stdout else "")
            print(r.stderr[-1000:] if r.stderr else "")
            raise RuntimeError("toolbox image build failed")

    # Step 1: Stop containers (don't use -v to preserve cargo build cache)
    print("\n[deploy] Stopping services...")
    run_compose(compose, "down")

    # Remove only data volumes, preserving build cache volumes
    print("[deploy] Removing data volumes...")
    for vol in ["postgres_data", "mm-db", "mm-config", "mm-data", "mm-logs", "mm-plugins"]:
        subprocess.run(["docker", "volume", "rm", "-f", f"omnideploy_{vol}"], capture_output=True)

    # Step 2 (dev): Build images
    if mode == "dev":
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

    # Step 5 (dev): Build all binaries via build.py
    # build.py auto-discovers all workspace members from Cargo.toml
    # and builds everything — omniagent, db-migrations, and all plugin
    # binaries (platforms + tools). No hardcoded package lists.
    if mode == "dev":
        print("\n[deploy] Building all binaries...")
        run_compose_check(
            compose, "run", "--rm", "-e", "SQLX_OFFLINE=true", "omniagent",
            "python3", "/app/scripts/build.py",
            label="build all binaries",
        )

    # Step 6: Run migrations
    print("\n[deploy] Running migrations...")
    if mode in ("ci", "hybrid"):
        # CI/hybrid: production image has db-migrations at /usr/local/bin/
        run_compose_check(compose, "run", "--rm", "omniagent",
                          "db-migrations", label="migrations")
    else:
        # Dev: binary at /target/release/ (built with CARGO_TARGET_DIR=/target)
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

    # Step 8a: Register remote noop provider (needs omniagent running)
    # so remote.yml has the entry needed by test_fn_9b (provider source-awareness).
    print("[deploy] Registering remote noop provider...")
    # Two-tier URL strategy:
    #   1. file:// with container path (fast, offline, no auth — needs bind-mount)
    #   2. HTTPS GitHub URL (works everywhere but needs internet)
    # We try file:// first if the repo exists ON THE HOST.  If file:// fails,
    # switch to HTTPS.  HTTPS retries are persistent.  If BOTH fail, we log a
    # warning and continue — test_fn_9b will attempt its own registration later.
    CONTAINER_REMOTE = "/opt/workspace/omni-plugins"
    HTTPS_URL = "https://github.com/nexuslbs/omni-plugins.git"
    if os.path.isdir(REMOTE_REPO):
        install_url = f"file://{CONTAINER_REMOTE}"
        print(f"  [local repo found, using file:// first]")
    else:
        install_url = HTTPS_URL
        print(f"  [no local repo, using HTTPS]")
    noop_registered = False
    # Track whether we've already tried (or are currently on) HTTPS
    using_https = (install_url == HTTPS_URL)
    for attempt in range(30):
        payload = json.dumps({"url": install_url, "name": "noop", "path": "providers/noop-full"})
        r = run_compose(compose, "exec", "-T", "omniagent",
                        "curl", "-sSf", "-X", "POST",
                        "-H", "Content-Type: application/json",
                        "-d", payload,
                        "http://localhost:8080/api/plugins/install-git")
        if r.returncode == 0:
            resp_text = r.stdout.strip()
            try:
                resp_data = json.loads(resp_text) if resp_text else {}
                if resp_data.get("error"):
                    # file:// succeeded HTTP-wise but git failed inside container
                    if not using_https:
                        # First time: switch to HTTPS
                        print(f"\n  [file:// failed, falling back to HTTPS...]")
                        install_url = HTTPS_URL
                        using_https = True
                        continue
                    # HTTPS is also failing — keep retrying (it may be a
                    # transient issue like network hiccup)
                    if attempt >= 25:
                        print(f"  [HTTPS still failing after many retries: {resp_data['error'][:80]}]")
            except json.JSONDecodeError:
                pass
            print(f"  [registered remote noop: {resp_text[:120]}]")
            noop_registered = True
            break
        else:
            combined = (r.stdout + r.stderr).lower()
            if "already" in combined:
                print("  [remote noop already registered, skipping]")
                noop_registered = True
                break
            # file:// API unreachable (container not ready or file:// curl error)
            if not using_https:
                print(f"\n  [file:// unavailable, falling back to HTTPS...]")
                install_url = HTTPS_URL
                using_https = True
                continue
            if attempt == 0:
                print(f"  [waiting for HTTPS ready...]", end="", flush=True)
            print(".", end="", flush=True)
        time.sleep(3)
    if not noop_registered:
        print("  [WARNING: could not register remote noop — test_fn_9b will retry on its own]")
        print("  [This is non-fatal: the deploy continues and the test handles its own setup]")
    print()
    time.sleep(2)

    # Step 8b: Wait for dashboard to start serving (max 2 minutes)
    print("[deploy] Waiting for dashboard...")
    for i in range(60):
        r = run_compose(compose, "exec", "-T", "omniagent",
                        "curl", "-sf", "http://dashboard:3001/")
        if r.returncode == 0:
            print("  dashboard is ready")
            break
        if i % 15 == 0 and i > 0:
            print(f"  still waiting ({i * 2}s)...")
        if i == 59:
            rc = run_compose(compose, "logs", "--tail=30", "dashboard")
            print(rc.stdout[-2000:])
            raise RuntimeError("dashboard did not become healthy")
        time.sleep(2)

    # Step 9: Rust integration tests (api_tests, plugin_tests)
    run_rust_integration_tests(compose, mode)

    # Step 10: Python integration tests (2 passes, no retry — tests must be robust)
    for pass_num in [1, 2]:
        # Before each tests.py invocation, clean transient artifacts
        # (plugins.yml, remote.yml) from the bind-mounted omni-stack
        # directory so check_git_clean() never fails on retries.
        r = sh("cd /opt/workspace/omni-stack && git checkout HEAD -- plugins.yml remote.yml 2>/dev/null; true")
        print(f"\n{'=' * 60}")
        print(f"  INTEGRATION TESTS — PASS {pass_num}")
        print(f"{'=' * 60}")
        run_tests(compose)

    print(f"\n{'=' * 60}")
    print("  ALL TESTS PASSED")
    print(f"{'=' * 60}")

    # Step 11: Shared tool tests (Phase 1 + Phase 2) — dev mode only
    if mode == "dev":
        print(f"\n{'=' * 60}")
        print("  SHARED TOOL TESTS (Phase 1 + Phase 2)")
        print(f"{'=' * 60}")
        shared_settings = shared.Settings(
            env_path=OMNI_ENV_PATH,
            compose_file=os.path.join(OMNI_STACK_DIR, "docker-compose.yml"),
            dev_overlay=os.path.join(OMNI_STACK_DIR, "docker-compose.dev.yml"),
            project_name="omnideploy",
            container="omnideploy-omniagent-1",
            setup_channel="setup",
            omni_stack_dir=OMNI_STACK_DIR,
            workspace_dir=WORKSPACE_DIR,
            script_dir=SCRIPT_DIR,
            use_api=False,
        )
        shared.init(shared_settings)
        shared.run_tests()
        print(f"\n{'=' * 60}")
        print("  ALL TESTS PASSED (including shared tool tests)")
        print(f"{'=' * 60}")


def run_tests(compose=None):
    """Run integration tests via tests.py piped into the omniagent container."""
    if not os.path.exists(TESTS_SCRIPT):
        raise RuntimeError(f"Tests script not found: {TESTS_SCRIPT}")

    if compose is None:
        compose = compose_cmd("dev")

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
        choices=["dev", "ci", "hybrid", "test"],
        help="dev=build from source + shared tool tests, ci=use pre-built images, hybrid=build images+run like CI, test=run tests only",
    )
    args = parser.parse_args()

    if args.mode == "test":
        run_tests()
    else:
        deploy(args.mode)


if __name__ == "__main__":
    main()
