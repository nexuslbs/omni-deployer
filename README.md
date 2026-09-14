# omni-deployer

Deployment orchestration and CI/CD for the OmniAgent stack: `deploy.py` (self-contained
test harness), `omnidev.py` / `omnistable.py` (dev/stable stack lifecycle),
`shared.py` (shared setup/test helpers), and `scripts/tests.py` (integration suite).

## Repositories

| Repo | Path | Description |
|------|------|-------------|
| omniagent | `/opt/workspace/omniagent` | Rust agent core (engine, plugins, MCP tools, API) |
| omni-dashboard | `/opt/workspace/omni-dashboard` | Web dashboard (Vite + TypeScript) |
| omni-stack | `/opt/workspace/omni-stack` | Docker Compose stack + OMNI_DIR config (`config/*.yml`) |
| omni-plugins | `/opt/workspace/omni-plugins` | Remote-installable plugins + plugin-less provider definitions (root `models.yml`) |

## Usage

```bash
# Dev mode: builds images from source + runs cargo gates + full integration suite
python3 deploy.py dev

# CI mode: uses pre-built images (OMNIAGENT_IMAGE, DASHBOARD_IMAGE, TOOLBOX_IMAGE must be set)
# Pretests are skipped - the production Dockerfile builder already ran fmt/check/clippy/unit during the image build.
WORKSPACE_DIR=/path/to/workspace python3 deploy.py ci

# Hybrid mode: builds the production Dockerfile (its builder stage runs the quality gates)
python3 deploy.py hybrid

# Just run tests (stack must already be up)
python3 deploy.py test
```

The script generates `omni.env` with random passwords, starts services, runs
migrations, and executes the integration test suite (`scripts/tests.py`) twice
(single pass in `ci` mode - the GitHub-hosted runner's 1h budget can't fit
the double pass; dev/hybrid keep 2 passes for extra confidence).

## Dev vs Stable stacks

- **`omnidev.py` / `omnistable.py`** manage the two long-lived compose projects
  (`omnidev`, `omnistable`). `omnidev.py setup` / `omnistable.py setup` call
  `shared.setup()`, the ONLY caller that writes real LLM key refs
  (`$secret:DEEPSEEK_API_KEY` etc. into `plugins.yml`) from `secrets.env`.
- **`deploy.py` is a self-contained test harness** that must NEVER use real LLM keys -
  see AGENTS.md for the hard rule. It calls only `shared.init()` + `shared.run_tests()`.

## Development (docker-compose.dev.yml / omnidev)

The dev overlay (`docker-compose.dev.yml` in omni-stack/omni-root) provides a
live-development stack managed as the `omnidev` compose project:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml --project-name omnidev up -d
```

The overlay adds:
- **Local image builds** instead of pulling from GHCR (`omniagent-dev:latest` from `Dockerfile.dev`)
- **Mounted source directories** for live development (`/opt/workspace/omniagent:/app`, `/opt/workspace/omni-dashboard:/opt/repo`)
- **Dev-only host ports** (never in the base compose): dashboard `12345:3001`, mattermost `12346:8065`, paperclip `3101:3100`
- `SQLX_OFFLINE=false` - dev builds validate queries against the live DB so code + migrations can change without a stale `.sqlx` cache

The `omnidev` compose project is the development environment for the kanban dev workflows
(`dev-executor`, `omniagent-dev`): tasks on the `omnidev` board resolve to the
`omniagent-dev` workflow (executor → reviewer → tester) via `boards.yml`. `omnidev.py`
manages this project's lifecycle (setup, restart, stop).

## Integration suite (`scripts/tests.py`)

Groups 1–50 cover: dashboard page loading, plugin lifecycle (install/enable/remove/
update), kanban CRUD + dispatch, cron schedules, channel/board/workflow resolution,
single-instance lock, and the kanban-workflow feature groups:

| Group | Coverage |
|-------|----------|
| 40 | Workflow role mode (`agent`/`action`) + `auto_approve` + `review_on_fail` |
| 41 | Fail-thread routing (`review_on_fail`) + double-normalization fix |
| 42 | Plugins `omni_dir` config field (no hardcoded `/opt/omni` fallbacks) |
| 43 | Sub-prompts - pending user prompts appended to running thread |
| 44 | Builtin `omniagent-api` via test-tool-caller + fetch method gating |
| 45 | Wiki data source skill (Karpathy + Obsidian + filesystem examples) |
| 46 | models.yml provider/model overrides (CRUD API + plugin-less + absent-file + refresh upsert) |
| 47 | Resolve fallback fields ONCE at load - kanban task defaults (task → board → channel → global) |
| 48 | Single-instance advisory lock + CLI arg handling |
| 49 | omni-dashboard UI/UX fixes regression (DB page, custom selects, workflow defaults, hooks, templates, red cancel, plugin remove, git box) |
| 50 | Release push-tag version verification + dashboard Connected version |

### Group isolation and failure resume (`--group`, `--from-group`)

`scripts/tests.py` is a **group-isolated harness**: every group is a registered
segment, executed as setup → run → verify → cleanup inside its own scope, so any
single group can run alone in a fresh interpreter. `--list` prints all 59
segments with a `standalone` flag and the prerequisites a group declares.

| Flag | Meaning |
|------|---------|
| `--group N` | run EXACTLY group N (isolation; the group re-establishes its own preconditions) |
| `--from-group N` (alias `--start-group N`) | run every group from N onward to the end |
| `--list` | list every group segment, its standalone flag and declared prerequisites |
| `--with-prereqs` | with `--group N`, also run the groups N declares as prerequisites |
| `--json-report PATH` | write the per-group machine report (also echoed as one `OMNIAGENT_TESTS_REPORT {...}` line) |
| `--verify-only` | re-run the suite against the already-built stack, no rebuild |

Invocations (from the host, against the running dev stack):

    # one group, in isolation
    python3 deploy.py test --group 37
    # from group 37 to the end
    python3 deploy.py test --from-group 37
    # dev pipeline whose suite resumes at group 37 (no rebuild, no teardown)
    python3 deploy.py dev --from-group 37

Same directly inside the agent container (useful when debugging a group live):

    docker exec omnidev-omniagent-1 python3 -u \
        /opt/workspace/omni-deployer/scripts/tests.py --group 37

`deploy.py dev` drives this automatically: the suite reports its first failing
group, the deploy re-invokes it **from that group onward** without an image
rebuild or volume teardown (max `--group-retries` attempts per group), and a
run with no failure costs exactly one pass. The resume-on-failure loop is
therefore: fix the root cause of the failing group, re-run
`deploy.py dev --from-group N`, repeat until the end, then make one final
complete `deploy.py dev` run.

`deploy.py test` (with `--group N` or `--from-group N`) re-asserts the dev prep
BEFORE the selection runs, so an isolated run is reproducible on a stack left
behind by a previous (possibly failed) run, with no manual prep step: the
tracked seed config (`config/plugins.yml`, `channels.yml`, `remote.yml`,
`actions.yml`, `settings.yml`, `workflows.yml`) is re-seeded, the deploy-only
`cron`/`kanban`/`hooks` channel pins are applied, deploy tasks are cleared, the
agent container is restarted and `/health` is polled. Groups also re-establish
their own preconditions at the start of the group (provider plugin
directories/files, channels, profiles, wiki dirs), which is what makes a bare
`--group N` selection pass on a from-scratch stack.

## CI/CD

Single `publish.yml` workflow triggered on push to `stable` or `v*` tags.
It builds and publishes Docker images to GHCR:
- **Push to `stable`**: tags `omniagent:latest`, `omni-dashboard:latest`, `toolbox:latest`
- **Push to `v*` tags**: tags each image with the semver tag (e.g., `omniagent:1.2.3`)

Parallel jobs build:
1. **omniagent**: from [nexuslbs/omniagent](https://github.com/nexuslbs/omniagent) (multi-stage Rust build)
2. **omni-dashboard**: from [nexuslbs/omni-dashboard](https://github.com/nexuslbs/omni-dashboard) (Vite + TypeScript)
3. **toolbox**: from omni-stack/omni-root (`services/toolbox/Dockerfile`, alpine-based maintenance container)

Then:
1. Runs unit tests + lint
2. Runs integration tests via `deploy.py ci`
3. Tags git repos (omni-stack, omniagent, omni-dashboard) with the release version
4. Pushes + tags **nexuslbs/omni-plugins** - its root `models.yml` provides the
   plugin-less provider definitions used by the release (the noop test provider is
   sourced from omni-plugins, not the omniagent image)
5. Publishes all three images to GHCR

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKSPACE_DIR` | `/opt/workspace` | Directory containing `omni-stack/`, `omniagent/`, `omni-dashboard/` |
| `OMNIAGENT_IMAGE` | (required for ci) | Pre-built omniagent image reference |
| `DASHBOARD_IMAGE` | (required for ci) | Pre-built dashboard image reference |
| `TOOLBOX_IMAGE` | (required for ci) | Pre-built toolbox image reference |

Release-loop convention: kanban tasks → `deploy.py dev` verification → push `main` →
promote to `stable` (triggers publish). Doc/config-only changes push straight to `main`.
