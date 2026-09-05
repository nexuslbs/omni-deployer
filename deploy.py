#!/usr/bin/env python3
"""
OmniAgent deployer - orchestration + integration tests.

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
import shutil
import subprocess
import sys
import tempfile
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


def patch_deploy_channels_noop():
    """Pin the deploy environment's system channels to the noop provider.

    The deploy runs on a FRESH database with NO LLM secrets (never seeds
    secrets.env - that is omnidev/omnistable-only). But channels.yml is the
    shared bind-mounted config, and the `omni` profile pins
    provider=deepseek, so any thread on a channel WITHOUT an explicit
    provider (e.g. the `cron` channel used by the hooks/schedule tests)
    falls through to deepseek and 401s: "Config ref $secret:DEEPSEEK_API_KEY
    not found in secrets table".

    The deploy must therefore run with noop/test-tool-caller as the only
    provider. The wf-test channel is already pinned noop by the tests; the
    `cron` channel is the one system channel the tests actually execute
    threads on. Patching it here (deploy-only) is safe for the LIVE
    omnistable stack: its tasks.yml has no schedules/hooks on `cron`, so no
    live thread ever runs on that channel, and the final seed restore
    reverts channels.yml to HEAD at the end of the run anyway.

    Idempotent: no-op when the cron channel already carries provider/model.
    """
    path = os.path.join(OMNI_STACK_DIR, "config", "channels.yml")
    with open(path) as f:
        content = f.read()
    # The cron channel block in the committed channels.yml (HEAD) is:
    #   cron:
    #     resource_identifier: cron
    #     cause: system
    #     profile: omni
    # Match the block, insert provider/model after the `profile:` line.
    import re
    m = re.search(r"(?ms)^  cron:\n(?:    [^\n]*\n)*", content)
    if not m:
        print("  [WARNING: no cron channel block found in channels.yml - noop pin skipped]")
        return
    block = m.group(0)
    if re.search(r"(?m)^    provider:", block):
        print("  ✓ cron channel already pinned (noop) - skipping")
        return
    if not re.search(r"(?m)^    profile:", block):
        print("  [WARNING: cron channel block has no profile line - noop pin skipped]")
        return
    new_block = re.sub(
        r"(?m)^    profile: [^\n]*$",
        lambda m: m.group(0) + "\n    provider: noop\n    model: test-tool-caller",
        block,
        count=1,
    )
    content = content[: m.start()] + new_block + content[m.end():]
    # The bind-mounted config files are root-owned (the omniagent container
    # writes them as root); write via a temp file + sudo mv like every other
    # config mutation in this script.
    tmp_path = path + ".deploy-noop"
    with open(tmp_path, "w") as f:
        f.write(content)
    sh(f"sudo mv -f {tmp_path} {path}")
    print("  ✓ patched channels.yml: cron channel pinned to noop/test-tool-caller (deploy-only)")


def clear_deploy_tasks():
    """Clear schedules/hooks from tasks.yml so the deploy never spawns real-LLM threads.

    The seeded tasks.yml (HEAD) carries live hooks (wiki-maintenance,
    channel-summaries) that fire on `thread_finished` events. The deploy DB
    has NO LLM secrets - the `omni` profile pins deepseek, so any hook thread
    on the `hooks` channel 401s with "api key invalid" (the key resolves to a
    variable NAME, not a value). Those failures are parallel background noise
    that pollutes logs and has produced 401-class flakes.

    The deploy must therefore run with ZERO tasks: no schedules, no hooks.
    tasks.yml is git-tracked; the final seed restore (restore_seed_config,
    run in a finally) reverts it to HEAD when the deploy ends, and Step 0.5
    self-heals it on the next run if a hard kill skips the finally.

    Idempotent: rewrites the file with empty sections every time.
    """
    path = os.path.join(OMNI_STACK_DIR, "config", "tasks.yml")
    content = "schedules: {}\nhooks: {}\n"
    tmp_path = path + ".deploy-clear"
    with open(tmp_path, "w") as f:
        f.write(content)
    sh(f"sudo mv -f {tmp_path} {path}")
    print("  ✓ cleared tasks.yml: schedules + hooks emptied (deploy-only, no real-LLM threads)")


# ═══════════════════════════════════════════════════════════════════════
#  sudo compat
# ═══════════════════════════════════════════════════════════════════════
# The deploy scripts use `sudo` throughout so they also work when invoked
# under sudo on a dev host. Some container images (notably the production
# omniagent image) run as root with NO sudo binary, which made every
# `sudo ...` shell command fail silently (2>/dev/null + `; true`) and turned
# the pre-flight restore / final restore into no-ops. When sudo is absent we
# are already root, so install a tiny PATH shim that makes `sudo <cmd>`
# transparently execute `<cmd>`.
if shutil.which("sudo") is None:
    _sudo_shim_dir = tempfile.mkdtemp(prefix="sudo-shim-")
    with open(os.path.join(_sudo_shim_dir, "sudo"), "w") as _f:
        _f.write("#!/bin/sh\nexec \"$@\"\n")
    os.chmod(os.path.join(_sudo_shim_dir, "sudo"), 0o755)
    os.environ["PATH"] = _sudo_shim_dir + os.pathsep + os.environ["PATH"]
    print(f"[deploy] sudo not found (running as root) - installed no-op sudo shim")


# ═══════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def ensure_git_safe_dirs():
    """git refuses to operate on repos owned by another user (dubious
    ownership) - when deploy.py runs under sudo, `git clean`/`git checkout`
    on the hermes-owned workspace repos fail silently unless the repos are
    whitelisted. Register them so the sweep and final restore actually run."""
    for d in [OMNI_STACK_DIR, SCRIPT_DIR, OMNIAGENT_DIR]:
        sh(f"sudo git config --global --add safe.directory {d} 2>/dev/null; true")


ensure_git_safe_dirs()

# shared.py helpers (cleanup_runtime_state / verify_runtime_clean /
# ensure_seed_config / run_tests) all need initialized settings. Init once at
# module level with the deploy's Settings; run_tests() below reuses this.
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


def compose_cmd(mode):
    cmd = ["docker", "compose", "-f", os.path.join(OMNI_STACK_DIR, "docker-compose.yml")]
    if mode == "dev":
        cmd += ["-f", os.path.join(OMNI_STACK_DIR, "docker-compose.dev.yml")]
    # Local S3 (MinIO) service for the S3 backup/restore/checkpoint test -
    # every deploy mode carries it so the S3 test can round-trip locally.
    # The overlay lives in THIS repo (omni-deployer), resolved via SCRIPT_DIR.
    # Guarded: an omni-deployer checkout without the overlay still deploys;
    # the S3 test then skips (see test_s3_backup_restore).
    if os.path.exists(os.path.join(SCRIPT_DIR, "docker-compose.minio.yml")):
        cmd += ["-f", os.path.join(SCRIPT_DIR, "docker-compose.minio.yml")]
    # hybrid and ci use no overlay - base docker-compose.yml + omni.env.
    # The base compose is image-only (no build sections): hybrid builds the
    # three images locally with the omni.env tags BEFORE `up` (see Step 0b),
    # ci pulls pre-built images, omnistable pulls GHCR. run/exec/up all go
    # through docker compose.
    return cmd


def run_compose(cmd_parts, *args):
    full = list(cmd_parts) + ["--env-file", OMNI_ENV_PATH] + list(args)
    # Compose interpolation precedence is shell env > --env-file > .env, so
    # S3_*/MINIO_* exported in the launcher shell (real B2 creds for
    # omnistable backups) would override the deploy's freshly generated MinIO
    # creds in omni.env. Strip them from the subprocess env so the deploy's
    # own S3 endpoint/creds are authoritative (the S3 test round-trips
    # against local MinIO, never the production bucket).
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("S3_") and not k.startswith("MINIO_")}
    return subprocess.run(full, capture_output=True, text=True, env=env)


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

    In dev mode, cargo runs inside the dev container (via docker compose run).
    In ci/hybrid modes pretests are SKIPPED: the production Dockerfile's
    builder stage runs the identical gates during the image build (the CI
    build job builds the images, so re-running them on the host with a cold
    cargo cache would duplicate work and blow the runner's time budget).
    """
    docker_mode = mode  # for compose run; only dev uses the dev overlay
    compose = compose_cmd(docker_mode)

    print("=" * 60)
    print("  PRETESTS")
    print("=" * 60)

    if mode in ("ci", "hybrid"):
        # Hybrid + CI: no separate host pretests - the production Dockerfile's
        # builder stage runs the identical gates (fmt --check, check -D
        # warnings, clippy -D warnings, cargo test --release) during
        # `docker build`. In hybrid the omniagent image is built with
        # --no-cache (Step 0b) so those builder RUN gates ALWAYS re-execute
        # fresh, matching CI's cold builder on a fresh runner - a warm local
        # layer cache used to mask a flaky test that CI then hit. Re-running
        # the same four cargo commands on the host as well, with a cold cargo
        # cache on the 2-core runner, ballooned as the workspace grew
        # (spill/compact/goal/cost/code-exec/prompt-sections) and pushed the
        # job past the runner's time budget (>1h), so the builder-stage run is
        # the single gate in both modes.
        print("\n[pretests] Skipping (run via production Dockerfile build)...")
        return

    if mode == "dev":
        print("\n[pretests] Building dev image...")
        run_compose_check(compose, "build", "omniagent", label="dev image")

        def run_cargo(args, label="", extra_env=None):
            # Dev mode NEVER uses SQLX_OFFLINE=true: the dev overlay
            # (docker-compose.dev.yml) sets SQLX_OFFLINE=false so builds
            # validate queries against the live migrated DB. Passing
            # -e SQLX_OFFLINE=true here would defeat the dev overlay and
            # require a stale committed .sqlx cache. Only CI mode (host
            # cargo) uses the committed offline cache.
            env_flags = []
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

    # 1. cargo fmt --check (workspace-wide: core + all plugins)
    print("\n[pretests] Checking code format (cargo fmt --check)...")
    check_cargo(["cargo", "fmt", "--all", "--check"], label="cargo fmt --check")
    print("  ✓ Format check passed")

    # 2. cargo check -D warnings (via RUSTFLAGS) - whole workspace, all targets
    print("\n[pretests] Running cargo check (warnings as errors, workspace all targets)...")
    # RUSTFLAGS is used because `cargo check` doesn't support `--` passthrough to rustc
    # --workspace --all-targets: lints core AND every plugin crate (incl. tests/benches)
    check_cargo(
        ["cargo", "check", "--workspace", "--all-targets", "--release"],
        label="cargo check -D warnings",
        extra_env={"RUSTFLAGS": "-D warnings"},
    )
    print("  ✓ cargo check passed")

    # 3. cargo clippy -D warnings - whole workspace, all targets
    print("\n[pretests] Running cargo clippy (warnings as errors, workspace all targets)...")
    # clippy DOES support `--` to pass args to rustc
    check_cargo(
        ["cargo", "clippy", "--workspace", "--all-targets", "--release", "--", "-D", "warnings"],
        label="cargo clippy -D warnings",
    )
    print("  ✓ cargo clippy passed")

    # 4. cargo test --release --workspace (unit tests for core + all plugins;
    #    integration tests are #[ignore] and run later)
    print("\n[pretests] Running cargo test --workspace --release...")
    check_cargo(["cargo", "test", "--workspace", "--release"], label="cargo test --workspace --release")
    print("  ✓ Unit tests passed")


def run_rust_integration_tests(compose, mode="dev"):
    """Run api_tests and plugin_tests via cargo test.

    Only works in dev mode where the dev image has the Rust toolchain
    and all build dependencies. In CI/hybrid mode, the production image
    is too minimal (no cargo, no libssl-dev, no pkg-config) to compile
    Rust code - Python integration tests cover end-to-end flows instead.
    """
    if mode != "dev":
        print("[integration] Skipping Rust integration tests (dev mode only - "
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
    # Local S3 (MinIO) credentials - generated at the START of the deploy so
    # the S3 backup/restore/checkpoint test round-trips against a local MinIO
    # instead of real object storage. The SAME pair is used for the MinIO
    # root creds (MINIO_ROOT_USER/PASSWORD) and the toolbox rclone client
    # (S3_ACCESS_KEY/S3_SECRET_KEY), so the backup scripts authenticate
    # against the local minio with the root identity. S3_ENDPOINT is the
    # in-network service name (minio:9000) as seen from the toolbox.
    minio_user = os.urandom(12).hex()
    minio_pass = os.urandom(24).hex()
    s3_endpoint = "http://minio:9000"
    s3_region = "us-east-1"
    s3_bucket = "omni-backups"
    s3_path = "omni"

    with open(OMNI_ENV_PATH, "w") as f:
        f.write("COMPOSE_PROJECT_NAME=omnideploy\n")
        f.write("COMPOSE_PROFILES=mattermost,noop\n")
        # omnideploy (deploy/ci/hybrid) binds the omni-stack checkout at
        # /opt/omni - the compose mount interpolates HOST_OMNI_DIR. This is
        # the ONE launcher that maps to omni-stack; omnidev/omnistable map to
        # omni-root (see shared.generate_env).
        f.write(f"HOST_OMNI_DIR={OMNI_STACK_DIR}\n")
        f.write(f"POSTGRES_PASSWORD={p1}\n")
        f.write(f"MM_POSTGRES_PASSWORD={p2}\n")
        # Local S3 (MinIO) service + S3 client creds (docker-compose.minio.yml
        # in omni-deployer and the toolbox rclone config both interpolate these).
        f.write(f"MINIO_ROOT_USER={minio_user}\n")
        f.write(f"MINIO_ROOT_PASSWORD={minio_pass}\n")
        f.write(f"S3_ACCESS_KEY={minio_user}\n")
        f.write(f"S3_SECRET_KEY={minio_pass}\n")
        f.write(f"S3_ENDPOINT={s3_endpoint}\n")
        f.write(f"S3_REGION={s3_region}\n")
        f.write(f"S3_BUCKET={s3_bucket}\n")
        f.write(f"S3_PATH={s3_path}\n")

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

    # The toolbox backup scripts REQUIRE {OMNI_DIR}/.env to exist (backup.sh
    # Step 1 copies it to data/credentials/.env under `set -euo pipefail`)
    # and to carry POSTGRES_PASSWORD for the omniagent pg_dump - the toolbox
    # container env only exposes PGPASSWORD. Write the S3 + DB creds there so
    # the local S3 test can back up and restore the omniagent DB. Removed at
    # deploy end (restore_seed_config) to keep the seed checkout pristine.
    stack_env = os.path.join(OMNI_STACK_DIR, ".env")
    # The stack .env is inside the bind-mounted checkout; after a container
    # run backup.sh restores it as root (cp inside the toolbox), so write it
    # via tmp + sudo mv (same pattern as remote.yml below).
    tmp = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".env")
    tmp.write(f"S3_ACCESS_KEY={minio_user}\n")
    tmp.write(f"S3_SECRET_KEY={minio_pass}\n")
    tmp.write(f"S3_ENDPOINT={s3_endpoint}\n")
    tmp.write(f"S3_REGION={s3_region}\n")
    tmp.write(f"S3_BUCKET={s3_bucket}\n")
    tmp.write(f"S3_PATH={s3_path}\n")
    tmp.write(f"POSTGRES_PASSWORD={p1}\n")
    tmp.write(f"MM_POSTGRES_PASSWORD={p2}\n")
    tmp.write(f"COMPOSE_PROFILES=mattermost,noop\n")
    tmp_path = tmp.name
    tmp.close()
    subprocess.run(["sudo", "cp", tmp_path, stack_env], check=True)
    os.unlink(tmp_path)
    print(f"[deploy] Generated {stack_env} (local MinIO S3 creds)")

    # Seed remote.yml from the tracked seed - the FULL remote plugin manifest
    # (actions, hindsight, paperclip, telegram, test-rust-tool, ...). The
    # bind-mounted plugins.yml (also from seed) enables actions/telegram/etc.
    # as source: remote, so the deploy env must carry the same remote.yml a
    # real deployment has - otherwise those plugins resolve to
    # status=not_found and the live-plugin tests (GROUP 37/40/41, t6 platform)
    # fail. plugin_tests also needs test-rust-tool registered.
    # config/ is runtime-only now (not gitignored - seed model); the seed lives in
    # omni-deployer/seed/config (see shared.ensure_seed_config).
    remote_yml_path = os.path.join(OMNI_STACK_DIR, "config", "remote.yml")
    # Ensure .remote/ directories are clean before seeding (prevents stale git state)
    for subdir in ["tools", "platforms", "providers"]:
        remote_dir = os.path.join(OMNI_STACK_DIR, "plugins", subdir, ".remote")
        if os.path.isdir(remote_dir):
            subprocess.run(["rm", "-rf", remote_dir], capture_output=True)
    r = subprocess.run(["cat", os.path.join(shared.seed_config_dir(), "remote.yml")],
                       capture_output=True, text=True, timeout=10)
    remote_yml_content = r.stdout if r.returncode == 0 else None
    if remote_yml_content is None:
        print("[deploy] WARNING: could not read seed config/remote.yml - leaving remote.yml untouched")
    existing = ""
    if remote_yml_content is not None and os.path.exists(remote_yml_path):
        r = subprocess.run(["cat", remote_yml_path], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            existing = r.stdout
    if remote_yml_content is not None and existing.strip() != remote_yml_content.strip():
        # tempfile is imported at module level (line 21) - do NOT re-import
        # here: a local `import tempfile` would shadow the module binding and
        # break the earlier NamedTemporaryFile call in generate_env.
        tmp = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".yml")
        tmp.write(remote_yml_content)
        tmp_path = tmp.name
        tmp.close()
        subprocess.run(["sudo", "cp", tmp_path, remote_yml_path], check=True)
        os.unlink(tmp_path)
        print(f"[deploy] Seeded {remote_yml_path} from omni-stack HEAD")
    else:
        print(f"[deploy] {remote_yml_path} unchanged")


# Data volumes that get wiped for a fresh start (like CI). Build-cache
# volumes (cargo-registry, cargo-target, omniagent-target) are preserved.
# ⚠️ SAFETY: only volumes whose name starts with the deploy project prefix
# ("omnideploy_") are ever deleted. The compose project is forced to
# "omnideploy" via COMPOSE_PROJECT_NAME in omni.env - this guard makes that
# explicit so a future project-name change can never make deploy wipe the
# wrong project's data (e.g. the "omni" project volumes from omni-stack).
DATA_VOLUMES = ["postgres_data", "mm-db", "mm-config", "mm-data", "mm-logs", "mm-plugins",
                "minio-data"]
DEPLOY_VOLUME_PREFIX = "omnideploy_"


def remove_data_volumes():
    listed = subprocess.run(
        ["docker", "volume", "ls", "-q", "--filter", f"name={DEPLOY_VOLUME_PREFIX}"],
        capture_output=True, text=True,
    ).stdout.split()
    removed = []
    for vol in listed:
        # Hard guard: never touch a volume that isn't ours.
        if not vol.startswith(DEPLOY_VOLUME_PREFIX):
            continue
        suffix = vol[len(DEPLOY_VOLUME_PREFIX):]
        if suffix not in DATA_VOLUMES:
            continue  # build cache / other data - preserved
        subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True)
        removed.append(vol)
    if removed:
        print(f"[deploy] Removed data volumes: {', '.join(removed)}")


def _deploy(mode):
    """Internal deploy body - wrapped by deploy() which guarantees the seed
    restore runs in a finally on BOTH success and failure paths."""
    if not os.path.isdir(OMNI_STACK_DIR):
        raise RuntimeError(f"omni-stack not found at {OMNI_STACK_DIR}")
    # Step 0.4 (hybrid): CI-consistency preflight. Hybrid must test EXACTLY the
    # repo state CI (publish.yml) checks out: origin/main HEAD of omni-stack,
    # omniagent, omni-dashboard, omni-plugins. If local checkouts differ
    # (unpushed commits, uncommitted changes), hybrid tests different code than
    # CI and a test can pass in one but fail in the other (the 49-E source
    # audit drift). Fail fast: commit/push first, then verify.
    if mode == "hybrid":
        for _repo_dir in (OMNI_STACK_DIR, OMNIAGENT_DIR,
                          os.path.join(WORKSPACE_DIR, "omni-dashboard"),
                          REMOTE_REPO):
            _repo = os.path.basename(_repo_dir.rstrip("/"))
            subprocess.run(["git", "-C", _repo_dir, "fetch", "origin", "main"],
                           capture_output=True, text=True)
            _head = subprocess.run(["git", "-C", _repo_dir, "rev-parse", "HEAD"],
                                   capture_output=True, text=True).stdout.strip()
            _origin = subprocess.run(["git", "-C", _repo_dir, "rev-parse", "origin/main"],
                                     capture_output=True, text=True).stdout.strip()
            _dirty = subprocess.run(["git", "-C", _repo_dir, "status", "--porcelain"],
                                    capture_output=True, text=True).stdout.strip()
            if _head != _origin:
                raise RuntimeError(
                    f"[hybrid] {_repo} local HEAD {_head[:10]} != origin/main {_origin[:10]}: "
                    f"hybrid must test the same repo state as CI. Push {_repo} to origin/main first."
                )
            if _dirty:
                raise RuntimeError(
                    f"[hybrid] {_repo} working tree not clean (git status --porcelain non-empty): "
                    f"hybrid must test the same repo state as CI. Commit or stash the changes first."
                )
            print(f"  [hybrid] preflight ok: {_repo} @ origin/main {_origin[:10]} (clean)")


    # Step 0.5: Verify omni-stack is clean BEFORE touching anything ──
    # The stack dir is bind-mounted into the container, so state persists
    # on the host. NEVER auto-discard user changes - `git checkout HEAD -- .`
    # is forbidden here: it silently reverts uncommitted work (e.g. an env
    # or compose edit). Instead, fail fast and let the user discard, stage,
    # or commit their changes first. This check must run before generate_env()
    # because that function writes remote.yml back to the tracked file.
    #
    # EXCEPTION - untracked test residue: a run that dies mid-tests (OOM,
    # SIGKILL, Ctrl-C) never reaches the final cleanup step, leaving the bind
    # mount dirty and blocking the NEXT run's Step 0.5. profiles/omni is no
    # longer tracked (the runtime data dir materializes it), so there is no
    # known TRACKED residue to auto-restore - any tracked dirt fails fast.
    # Untracked plugins/ entries are test-created plugin residue that the
    # post-clean already removes; sweeping ONLY those is safe: it cannot
    # touch user edits, which are anything NOT under plugins/.
    r = sh("cd /opt/workspace/omni-stack && git status --porcelain")
    dirty_lines = r.stdout.splitlines()
    if dirty_lines:
        tracked_dirty = [ln for ln in dirty_lines if not ln.startswith("??")]
        untracked = [ln[3:].strip() for ln in dirty_lines if ln.startswith("??")]
        if tracked_dirty:
            raise RuntimeError(
                "Uncommitted changes detected in omni-stack. deploy.py will NOT "
                "discard them automatically - discard, stage, or commit them "
                "first, then re-run deploy.\n\n"
                + "\n".join(tracked_dirty)
            )
        print("[deploy] Detected untracked test residue in omni-stack - auto-restoring...")
        # Untracked plugins/ residue is test-created (seed tracks zero plugins);
        # the same sweep the post-clean runs, so a fresh run starts like CI.
        for u in untracked:
            if u.startswith("plugins"):
                sh(f"cd /opt/workspace/omni-stack && sudo rm -rf -- {u} 2>/dev/null; true")
        r = sh("cd /opt/workspace/omni-stack && git status --porcelain")
        # Untracked files are the LIVE agent's own data-dir artifacts (wiki
        # pages, config symlinks / plugins.yml at the data-dir root, the
        # auto-created profiles/<default>/config.json) - NOT deploy residue, so
        # they must not block the run. Only tracked files count as dirt (same
        # rule as the end-of-run check below).
        tracked_dirty = [ln for ln in r.stdout.splitlines() if not ln.startswith("??")]
        if tracked_dirty:
            raise RuntimeError(
                "omni-stack still dirty after known-residue restore. "
                f"Refusing to continue:\n{r.stdout}"
            )
        print("  ✓ omni-stack clean after residue restore")

    # Repo is verified clean, so it is now safe to remove root-owned,
    # gitignored build/test residue (target/, .remote/ clones, test-* tools)
    # that the container wrote into the bind mount. `git clean -fdX` removes
    # ONLY ignored files (`.remote/` clones) and `git clean -fd` removes
    # untracked test tools - tracked files are never touched, so nothing needs
    # restoring and user work can never be discarded. omni-stack is a seed and
    # tracks zero plugins, so there is no bundled plugin to preserve. Removing
    # the residue keeps local runs as fresh as a CI checkout.
    sh("cd /opt/workspace/omni-stack && "
       "sudo git clean -fdX -- plugins/tools plugins/platforms plugins/providers 2>/dev/null; "
       "sudo git clean -fd -- plugins/tools plugins/platforms plugins/providers 2>/dev/null; "
       "true")

    # ── Runtime-state gate (user rule 2026-08-22) ──────────────────────────
    # omni-stack is a SEED checkout: config/, plugins/ and any profiles/ dir
    # are runtime-only state - never part of the repo. dev
    # regenerates everything from the tracked seed + the plugin API (temporary
    # during the deploy, removed at the end); hybrid/ci require a pristine
    # checkout and fail fast otherwise.
    if mode == "dev":
        print("\n[deploy] Cleaning runtime state before dev deploy...")
        shared.cleanup_runtime_state(OMNI_STACK_DIR)
    else:
        print("\n[deploy] Verifying checkout is pristine (hybrid/ci)...")
        shared.verify_runtime_clean(OMNI_STACK_DIR)
    # Seed the runtime config/ from the tracked seed (omni-deployer/seed/config).
    print("\n[deploy] Seeding config/ from tracked seed...")
    shared.ensure_seed_config(OMNI_STACK_DIR)

    # deploy.py (project "omnideploy") tears down the launcher stacks
    # BEFORE starting its own - CI wants a clean slate (fresh runner, nothing
    # running to preserve). In DEV mode ONLY omnidev is stopped: omnistable is
    # the agent's own live runtime and must never be torn down while the
    # deploy is running. In HYBRID mode NEITHER omnidev NOR omnistable is
    # stopped: hybrid runs from a launcher stack too and manages only its own
    # omnideploy containers (see MODE_STOP_EXCLUDE in shared.py).
    # (omnidev and omnistable do NOT tear down each other: they run
    # side-by-side since host ports are dev-overlay-only.)
    shared.stop_other_stacks("omnideploy", mode=mode)

    generate_env(mode)
    compose = compose_cmd(mode)

    # Step 0.6: Pin the deploy environment to the noop provider. The deploy
    # DB is fresh with NO LLM secrets (secrets.env is omnidev/omnistable-only
    # and is never read here); the shared omni profile pins deepseek, so any
    # thread on a channel without an explicit provider (cron/hooks/schedules)
    # would 401. Channels.yml is restored to HEAD by the final seed restore.
    print("\n[deploy] Pinning system channels to noop provider (deploy-only)...")
    patch_deploy_channels_noop()

    # Step 0.6b: Clear tasks.yml so no seeded hook/schedule thread can spawn
    # during the deploy (they'd 401 - deploy DB has no LLM secrets). Restored
    # to HEAD by restore_seed_config() in the finally below.
    print("\n[deploy] Clearing tasks.yml (deploy-only, no real-LLM threads)...")
    clear_deploy_tasks()

    # ── Step 0 (hybrid): Stop old containers first ────────────────
    if mode == "hybrid":
        print("\n[deploy] Stopping old services...")
        run_compose(compose, "down")
        print("[deploy] Removing data volumes...")
        remove_data_volumes()

    # Step 1: Stop containers (don't use -v to preserve cargo build cache)
    print("\n[deploy] Stopping services...")
    run_compose(compose, "down")

    # Remove only data volumes, preserving build cache volumes
    print("[deploy] Removing data volumes...")
    remove_data_volumes()

    # Step 0b (hybrid): Build the images locally like CI would, tagged with
    # the exact names omni.env references (local/omniagent:latest,
    # local/omni-dashboard:latest, local/omni-toolbox:latest). The base
    # compose is image-only - services consume pre-built images by tag, so
    # the images MUST exist before `up`. All three are built with plain
    # `docker build -t <tag>` (no compose build sections in the base
    # compose; source builds live in the dev overlay). The omniagent build is
    # done with --no-cache (see build_image): its builder-stage RUN gates must
    # re-execute fresh, exactly like CI's cold builder - a warm cached builder
    # layer would mask a flaky test that CI then hits (2026-08-30 incident).
    # the production Dockerfile whose builder stage runs fmt/check/clippy/
    # test offline against the committed .sqlx cache (the hybrid pretest
    # gate). The image is built with OMNIAGENT_BUILD_MODE=release below,
    # exactly like CI's publish build, so it auto-applies the idempotent
    # schema against the harness's own isolated project DB on start.
    # No env override exists anymore.
    if mode == "hybrid":
        def build_image(tag, dockerfile=None, context=None, service=None, no_cache=False, build_args=None):
            if service is not None:
                # Service has a build section in the compose file - let
                # compose build it and tag per the service image:.
                print(f"\n[deploy] Building {service} (docker compose build)...")
                run_compose_check(compose, "build", service, label=f"{service} image build")
                return
            cmd = ["docker", "build", "-t", tag]
            if no_cache:
                # HYBRID/CI CONSISTENCY (fix 2026-08-30): CI builds on a fresh
                # runner with a COLD builder cache, so the builder stage's gates
                # (fmt --check, check -D warnings, clippy -D warnings, cargo
                # test --workspace --release) ALWAYS re-execute there. Hybrid
                # previously reused the warm local layer cache: unchanged source
                # hit the cached builder RUN layer and the tests did NOT re-run,
                # so a flaky/timing-dependent test could pass in hybrid and fail
                # in CI. --no-cache makes hybrid exercise EXACTLY the same
                # fresh-builder gates as CI - a failure now appears in BOTH
                # hybrid and CI, or in NEITHER.
                cmd += ["--no-cache"]
            if dockerfile:
                cmd += ["-f", dockerfile]
            for ba in (build_args or []):
                cmd += ["--build-arg", ba]
            cmd.append(context)
            print(f"\n[deploy] Building {tag} (docker build)...")
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                print(r.stdout[-1000:] if r.stdout else "")
                print(r.stderr[-1000:] if r.stderr else "")
                raise RuntimeError(f"image build failed for {tag}")

        build_image("local/omniagent:latest",
                    dockerfile=os.path.join(OMNIAGENT_DIR, "Dockerfile"),
                    context=OMNIAGENT_DIR,
                    no_cache=True,
                    build_args=["OMNIAGENT_BUILD_MODE=release"])
        build_image("local/omni-dashboard:latest",
                    context=os.path.join(WORKSPACE_DIR, "omni-dashboard"))
        build_image("local/omni-toolbox:latest",
                    context=os.path.join(OMNI_STACK_DIR, "services", "toolbox"))

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

    # Step 5 (dev): Build db-migrations binary, run migrations, THEN run
    # pretests. Dev builds use SQLX_OFFLINE=false (dev overlay compose env) so
    # sqlx validates queries against the live migrated DB at compile time -
    # the schema must exist before any workspace build/check/clippy/test.
    # db-migrations has no compile-time sqlx macros, so it builds fine against
    # an empty DB.
    if mode == "dev":
        print("\n[deploy] Building db-migrations binary...")
        run_compose_check(
            compose, "run", "--rm", "omniagent",
            "cargo", "build", "--release", "-p", "db-migrations",
            label="build db-migrations",
        )
        print("[deploy] Running migrations...")
        run_compose_check(
            compose, "run", "--rm", "omniagent",
            "/target/release/db-migrations", label="migrations",
        )

    # Step 0: Pretests - AFTER the DB is migrated (dev) so SQLX_OFFLINE=false
    # validates against the live schema. In dev mode cargo runs inside the dev
    # container; in CI mode cargo runs on the host (uses committed .sqlx cache).
    run_pretests(mode)

    # Step 5b (dev): prepare.py + build all binaries (after pretests)
    if mode == "dev":
        # prepare.py: cargo fmt + cargo sqlx prepare --workspace - formats the
        # Rust sources and regenerates the offline .sqlx query cache against
        # the live migrated DB, so committed caches stay fresh for
        # SQLX_OFFLINE=true (stable/CI) builds.
        # The script lives in the omni-deployer repo (deploy tooling), so it
        # is bind-mounted read-only into the container; --root /app points it
        # at the omniagent workspace mounted at /app.
        print("\n[deploy] Running prepare.py (cargo fmt + sqlx prepare)...")
        prepare_script = os.path.join(SCRIPT_DIR, "scripts", "prepare.py")
        run_compose_check(
            compose, "run", "--rm",
            "-v", f"{prepare_script}:/tmp/prepare.py:ro",
            "omniagent",
            "python3", "/tmp/prepare.py", "--root", "/app",
            label="prepare (fmt + sqlx offline cache)",
        )

        print("\n[deploy] Building all binaries...")
        run_compose_check(
            compose, "run", "--rm", "omniagent",
            "python3", "/app/scripts/build.py",
            label="build all binaries",
        )

    # Step 6: Run migrations (ci/hybrid: production image has db-migrations)
    if mode in ("ci", "hybrid"):
        print("\n[deploy] Running migrations...")
        run_compose_check(compose, "run", "--rm", "omniagent",
                          "db-migrations", label="migrations")

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
    #   1. file:// with container path (fast, offline, no auth - needs bind-mount)
    #   2. HTTPS GitHub URL (works everywhere but needs internet)
    # We try file:// first if the repo exists ON THE HOST.  If file:// fails,
    # switch to HTTPS.  HTTPS retries are persistent.  If BOTH fail, we log a
    # warning and continue - test_fn_9b will attempt its own registration later.
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
                    # HTTPS is also failing - keep retrying (it may be a
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
        print("  [WARNING: could not register remote noop - test_fn_9b will retry on its own]")
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

    # Step 10: Python integration tests (2 passes, no retry - tests must be
    # robust). CI runs a SINGLE pass: the GitHub-hosted runner died after
    # >1h ("lost communication with the server") - the double pass plus the
    # build pushed it over the runner's time budget. dev/hybrid keep the
    # double pass for extra confidence.
    passes = [1] if mode == "ci" else [1, 2]
    for pass_num in passes:
        # Before each tests.py invocation, re-assert the SEED content of the
        # transient config files (plugins.yml, remote.yml, actions.yml,
        # settings.yml, workflows.yml) in the bind-mounted omni-stack config/
        # so a re-run starts from a known state (config/ is runtime-only now -
        # nothing is git-restored; the tracked seed is the source of truth).
        # tasks.yml is intentionally NOT re-seeded here: clear_deploy_tasks()
        # emptied it at deploy start so no real-LLM hook/schedule thread can
        # spawn during tests; re-seeding would re-arm the hooks mid-deploy.
        shared.ensure_seed_config(
            OMNI_STACK_DIR,
            overwrite_files=["plugins.yml", "remote.yml", "actions.yml",
                             "settings.yml", "workflows.yml"],
        )
        # Re-assert the empty tasks.yml (tests.py's own hook tests may have
        # added entries during the previous pass - they clean up, but the
        # deploy contract is ZERO tasks at all times).
        clear_deploy_tasks()
        print(f"\n{'=' * 60}")
        print(f"  INTEGRATION TESTS - PASS {pass_num}")
        print(f"{'=' * 60}")
        run_tests(compose)

    # Clean test-created plugin residue from omni-stack (seed rule: test
    # artifacts are removed after the run, not gitignored). The same sweep
    # also runs inside shared.run_tests() below; doing it here too keeps the
    # bind mount clean even if the shared phase is skipped or fails early.
    print("\n[Cleaning test-created plugin residue from omni-stack...]")
    sh("cd /opt/workspace/omni-stack && "
       "sudo git clean -fdX -- plugins 2>/dev/null; "
       "sudo git clean -fd -- plugins 2>/dev/null; "
       "rmdir plugins/tools plugins/platforms plugins/providers plugins 2>/dev/null; "
       "sudo rm -f config/workflows.yml; "
       "true")

    print(f"\n{'=' * 60}")
    print("  ALL TESTS PASSED")
    print(f"{'=' * 60}")

    # Step 11: Shared tool tests (Phase 1 + Phase 2) - all modes
    print(f"\n{'=' * 60}")
    print("  SHARED TOOL TESTS (Phase 1 + Phase 2)")
    print(f"{'=' * 60}")
    # shared_settings was initialized at module level; only the dev overlay
    # is mode-dependent.
    shared_settings.dev_overlay = (
        os.path.join(OMNI_STACK_DIR, "docker-compose.dev.yml") if mode == "dev" else None
    )
    shared.run_tests()

    print(f"\n{'=' * 60}")
    print("  ALL TESTS PASSED (including shared tool tests)")
    print(f"{'=' * 60}")

    # Step 11b: Local S3 (MinIO) backup/restore/checkpoint test - runs LAST
    # because restore_backup/restore_checkpoint drop + recreate the omniagent
    # DB, which would invalidate any subsequent test's data.
    test_s3_backup_restore(compose)


# Live runtime config files whose content (platform secret refs, remote
# plugin manifest, tool settings) must SURVIVE a dev/hybrid deploy cycle.
# deploy.py treats the omni-stack dir as a seed checkout and re-seeds config/
# from omni-deployer/seed/config during the run. Without a backup/restore, a
# deploy would wipe the live platform's plugins.yml (telegram bot_token
# $secret ref etc.) and inbound would stay disabled until manual re-entry
# (incident 2026-09-05: post-deploy restart lost bot_token + $secret refs).
LIVE_RUNTIME_CONFIG_FILES = [
    "plugins.yml", "remote.yml", "actions.yml", "settings.yml", "workflows.yml",
]
RUNTIME_CONFIG_BACKUP_DIR = os.path.join(tempfile.gettempdir(), "omni-deployer-live-config-backup")


def preserve_runtime_config():
    """Back up existing live runtime config files before a dev/hybrid deploy.

    CI runs against a throwaway checkout and keeps pristine seed semantics, so
    this is only invoked for dev/hybrid. restore_runtime_config puts the files
    back after the deploy ends (including after the final cleanup_runtime_state).
    """
    src_dir = os.path.join(OMNI_STACK_DIR, "config")
    if not os.path.isdir(src_dir):
        print("  [preserve_runtime_config: no existing config/ - nothing to back up]")
        return
    sh(f"rm -rf {RUNTIME_CONFIG_BACKUP_DIR}")
    os.makedirs(RUNTIME_CONFIG_BACKUP_DIR, exist_ok=True)
    n = 0
    for name in LIVE_RUNTIME_CONFIG_FILES:
        src = os.path.join(src_dir, name)
        if os.path.isfile(src):
            sh(f"cp -f {src} {os.path.join(RUNTIME_CONFIG_BACKUP_DIR, name)}")
            n += 1
    print(f"  [preserve_runtime_config: backed up {n} live config file(s)]")


def restore_runtime_config(mode):
    """Restore the live runtime config files preserved before the deploy.

    Runs in the deploy() finally AFTER cleanup_runtime_state, then restarts the
    omniagent container so it loads the restored plugins.yml (its secret refs
    resolve against the DB with no manual re-entry).
    """
    if not os.path.isdir(RUNTIME_CONFIG_BACKUP_DIR):
        return
    target = os.path.join(OMNI_STACK_DIR, "config")
    os.makedirs(target, exist_ok=True)
    restored = []
    for name in LIVE_RUNTIME_CONFIG_FILES:
        src = os.path.join(RUNTIME_CONFIG_BACKUP_DIR, name)
        if os.path.isfile(src):
            tmp = os.path.join(target, name + ".restore-tmp")
            sh(f"cp -f {src} {tmp}")
            sh(f"sudo mv -f {tmp} {os.path.join(target, name)}")
            restored.append(name)
    sh(f"rm -rf {RUNTIME_CONFIG_BACKUP_DIR}")
    if not restored:
        return
    print(f"  [restore_runtime_config: restored live config: {', '.join(restored)}]")
    compose = compose_cmd(mode)
    print("  [restore_runtime_config: restarting omniagent to load the restored config...]")
    run_compose(compose, "restart", "omniagent")
    for i in range(30):
        r = run_compose(compose, "exec", "-T", "omniagent",
                        "curl", "-sf", "http://localhost:8080/health")
        if r.returncode == 0:
            print("  ✓ omniagent healthy after restart")
            return
        time.sleep(2)
    print("  [WARNING: omniagent did not become healthy after restart]")


def restore_seed_config():
    """Revert omni-stack TRACKED files to HEAD after a deploy run.

    The tree was verified clean at Step 0.5. profiles/omni is no longer
    tracked (the runtime data dir materializes profiles/<default>/config.json
    at startup); config/*, hindsight_watermark.json and plugins/* are
    runtime-only (NOT gitignored - seed model: tracked-able, no files in the
    seed) - they are removed by cleanup_runtime_state(),
    never git-restored.

    Runs on BOTH success and failure paths (deploy() wraps its body in a
    finally that calls this), so a mid-deploy crash never leaves the bind
    mount dirty for the next run.
    """
    print("\n[Restoring omni-stack tracked config to HEAD...]")
    # Remove the deploy-generated stack .env (local MinIO S3 creds). It is
    # gitignored so git status stays clean either way, but the deploy leaves
    # a pristine seed checkout - the creds were generated for THIS run only.
    sh("sudo rm -f /opt/workspace/omni-stack/.env 2>/dev/null; true")

    # Fail loudly if the restore did not actually work (e.g. git dubious
    # ownership under sudo, which the 2>/dev/null above would otherwise
    # swallow and leave a dirty tree that blocks the NEXT run's Step 0.5).
    r = sh("cd /opt/workspace/omni-stack && git status --porcelain")
    dirty = [ln for ln in r.stdout.splitlines() if not ln.startswith("??")]
    if dirty:
        raise RuntimeError(
            "omni-stack not clean after test restore - Step 0.5 would fail "
            f"on the next run. Dirty entries:\n" + "\n".join(dirty[:10])
        )
    print("  ✓ omni-stack clean after restore")


def deploy(mode):
    """Run the deploy, ALWAYS restoring the seed config when it ends.

    deploy.py creates runtime-only state in omni-stack (config/, plugins/,
    profiles/) as a side effect of testing. The runtime cleanup must therefore
    run whether the deploy succeeded or crashed mid-way - otherwise the bind
    mount stays dirty and the NEXT run's Step 0.5 fails. A hard kill
    (SIGKILL/OOM) can still skip this finally; Step 0.5's untracked-residue
    sweep covers that case.
    """
    # dev/hybrid run against the live data dir: preserve its runtime config so
    # a deploy cycle never wipes platform secret refs (telegram bot_token,
    # mattermost $secret refs). ci is a throwaway checkout - seed semantics.
    preserve_live_config = mode in ("dev", "hybrid")
    if preserve_live_config:
        preserve_runtime_config()
    try:
        _deploy(mode)
    finally:
        try:
            restore_seed_config()
        except Exception as e:
            # The deploy body's own error takes precedence; never mask it
            # with a restore failure. The next run's Step 0.5 will surface
            # any remaining dirt with a clear message.
            print(f"  [WARNING: seed restore failed: {e}]")
        try:
            # Per user rule: ALL deploy modes end with a pristine seed
            # checkout - config/, plugins/ and profiles/ are runtime-only
            # and removed when the deploy ends.
            shared.cleanup_runtime_state(OMNI_STACK_DIR)
        except Exception as e:
            print(f"  [WARNING: runtime-state cleanup failed: {e}]")
        if preserve_live_config:
            try:
                restore_runtime_config(mode)
            except Exception as e:
                print(f"  [WARNING: live config restore failed: {e}]")
            try:
                shared.verify_platform_inbound()
            except Exception as e:
                print(f"  [WARNING: post-deploy inbound verification failed: {e}]")


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


def test_s3_backup_restore(compose):
    """Local S3 (MinIO) backup/restore/checkpoint test.

    Exercises the toolbox backup scripts (backup.sh / restore_backup.sh /
    checkpoint.sh / restore_checkpoint.sh) against the local MinIO service
    from docker-compose.minio.yml (omni-deployer repo), using the omniagent
    secrets table as the canary:

      backup flow:   add secret-01 → backup → add secret-02 → restore
                     → DB last secret must roll back to secret-01
      checkpoint:    add secret-03 → checkpoint x2 (same YYYYMMDD path)
                     → add secret-04 → restore_checkpoint YYYYMMDD
                     → DB last secret must roll back to secret-03

    The MinIO credentials are generated at deploy start (generate_env) and
    written to both omni.env (compose interpolation) and the stack .env
    (toolbox backup scripts source it), so the S3 client (rclone in the
    toolbox container) connects to the local minio.
    """
    print("\n" + "=" * 60)
    print("  S3 BACKUP/RESTORE/CHECKPOINT TEST (local MinIO)")
    print("=" * 60)

    if not os.path.exists(os.path.join(SCRIPT_DIR, "docker-compose.minio.yml")):
        print("  ⏭  SKIPPED: docker-compose.minio.yml not present in omni-deployer")
        return

    def env_val(name):
        with open(OMNI_ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1]
        return None

    s3_bucket = env_val("S3_BUCKET") or "omni-backups"
    s3_path = env_val("S3_PATH") or "omni"

    # ── 1. Wait for minio health ──────────────────────────────────────────
    print("\n  [S3] Waiting for minio health...")
    ok = False
    for i in range(60):
        r = run_compose(compose, "exec", "-T", "minio",
                        "curl", "-sf", "http://localhost:9000/minio/health/live")
        if r.returncode == 0:
            ok = True
            break
        time.sleep(2)
    if not ok:
        raise RuntimeError("minio did not become healthy")
    print("  ✓ minio healthy")

    # ── 2. Ensure the bucket exists ───────────────────────────────────────
    r = run_compose(compose, "exec", "-T", "toolbox", "sh", "-c",
                    f"rclone --config /etc/rclone/rclone.conf mkdir s3-backup:{s3_bucket}")
    if r.returncode != 0:
        raise RuntimeError(f"bucket mkdir failed: {r.stdout[-300:]} {r.stderr[-300:]}")

    # ── helpers ───────────────────────────────────────────────────────────
    def db_last_secret():
        r = run_compose(compose, "exec", "-T", "postgres", "psql", "-U", "omniagent",
                        "-d", "omniagent", "-tAc",
                        "SELECT name FROM secrets ORDER BY id DESC LIMIT 1")
        return r.stdout.strip()

    def add_secret(name, value):
        body = json.dumps({"name": name, "fieldType": "text", "value": value})
        r = run_compose(compose, "exec", "-T", "omniagent", "curl", "-sSf",
                        "-X", "POST", "-H", "Content-Type: application/json",
                        "-d", body, "http://localhost:8080/secrets")
        if r.returncode != 0:
            raise RuntimeError(
                f"add secret {name} failed: {r.stdout[-300:]} {r.stderr[-300:]}")

    def wait_omniagent():
        for i in range(120):
            r = run_compose(compose, "exec", "-T", "omniagent",
                            "curl", "-sf", "http://localhost:8080/health")
            if r.returncode == 0:
                return
            time.sleep(2)
        raise RuntimeError("omniagent not healthy after S3 restore")

    def assert_last(name, stage):
        got = db_last_secret()
        if got != name:
            raise RuntimeError(f"{stage}: expected last secret {name!r}, got {got!r}")
        print(f"  ✓ {stage}: DB last secret = {name}")

    # Clean slate - the deploy DB may carry secrets created by earlier tests
    # (plugin configs). Deterministic assertions need an empty secrets table.
    run_compose(compose, "exec", "-T", "postgres", "psql", "-U", "omniagent",
                "-d", "omniagent", "-c", "DELETE FROM secrets;")

    # ── 3. Backup flow ────────────────────────────────────────────────────
    add_secret("secret-01", "value-01")
    assert_last("secret-01", "after add secret-01")

    print("  [S3] running backup.sh → local minio...")
    r = run_compose(compose, "exec", "-T", "toolbox", "/usr/bin/backup")
    if r.returncode != 0:
        raise RuntimeError(f"backup.sh failed: {r.stdout[-500:]} {r.stderr[-500:]}")

    add_secret("secret-02", "value-02")
    assert_last("secret-02", "after add secret-02")

    print("  [S3] running restore_backup.sh ← local minio...")
    r = run_compose(compose, "exec", "-T", "toolbox", "/usr/bin/restore_backup")
    if r.returncode != 0:
        raise RuntimeError(f"restore_backup.sh failed: {r.stdout[-500:]} {r.stderr[-500:]}")
    wait_omniagent()
    assert_last("secret-01", "after restore_backup")

    # ── 4. Checkpoint flow ────────────────────────────────────────────────
    add_secret("secret-03", "value-03")
    assert_last("secret-03", "after add secret-03")

    print("  [S3] running checkpoint.sh (x2) → local minio...")
    for i in (1, 2):
        r = run_compose(compose, "exec", "-T", "toolbox", "/usr/bin/checkpoint")
        if r.returncode != 0:
            raise RuntimeError(
                f"checkpoint.sh (run {i}) failed: {r.stdout[-500:]} {r.stderr[-500:]}")

    # Both checkpoints must land in the SAME YYYYMMDD date path.
    r = run_compose(compose, "exec", "-T", "toolbox", "sh", "-c",
                    f"rclone --config /etc/rclone/rclone.conf "
                    f"lsd s3-backup:{s3_bucket}/{s3_path}/checkpoint/")
    if r.returncode != 0:
        raise RuntimeError(f"checkpoint lsd failed: {r.stdout[-300:]} {r.stderr[-300:]}")
    date_dirs = [ln.split()[-1] for ln in r.stdout.splitlines() if ln.strip()]
    if len(date_dirs) != 1 or len(date_dirs[0]) != 8 or not date_dirs[0].isdigit():
        raise RuntimeError(f"expected exactly one YYYYMMDD checkpoint dir, got {date_dirs}")
    ckpt_date = date_dirs[0]
    print(f"  ✓ two checkpoints share the same date path: {ckpt_date}")

    add_secret("secret-04", "value-04")
    assert_last("secret-04", "after add secret-04")

    print(f"  [S3] running restore_checkpoint.sh {ckpt_date} ← local minio...")
    # restore_checkpoint.sh drops/recreates the omniagent DB but - unlike
    # restore_backup.sh - does NOT stop the live omniagent first. The agent's
    # pool reconnects between the script's pg_terminate_backend and the DROP,
    # so the DROP fails with 'database "omniagent" is being accessed by other
    # users' (observed on a clean run). Stop omniagent for the checkpoint
    # restore; it is restarted + health-checked below.
    run_compose_check(compose, "stop", "omniagent",
                      label="stop omniagent for checkpoint restore")
    r = run_compose(compose, "exec", "-T", "toolbox", "/usr/bin/restore_checkpoint", ckpt_date)
    if r.returncode != 0:
        raise RuntimeError(
            f"restore_checkpoint.sh failed: {r.stdout[-500:]} {r.stderr[-500:]}")
    # omniagent was stopped above for the DB drop - restart it so its pool
    # reconnects against the restored schema.
    run_compose_check(compose, "restart", "omniagent",
                      label="omniagent restart after checkpoint restore")
    wait_omniagent()
    assert_last("secret-03", "after restore_checkpoint")

    print("\n  ✓ S3 backup/restore/checkpoint test PASSED")


# ═══════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="OmniAgent deployer")
    parser.add_argument(
        "mode",
        choices=["dev", "ci", "hybrid", "test", "verify-inbound"],
        help="dev=build from source + shared tool tests, ci=use pre-built images, hybrid=build images+run like CI, test=run tests only, verify-inbound=check every enabled platform resolves its secrets and inbound is active",
    )
    args = parser.parse_args()

    if args.mode == "test":
        run_tests()
    elif args.mode == "verify-inbound":
        code = shared.verify_platform_inbound()
        print(f"[deploy] verify-inbound exit={code}")
        sys.exit(code)
    else:
        deploy(args.mode)


if __name__ == "__main__":
    main()
