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
| omni-plugins | `/opt/workspace/omni-plugins` | Plugin-less provider definitions (root `models.yml`) |

## Usage

```bash
# Dev mode: builds images from source + runs cargo gates + full integration suite
python3 deploy.py dev

# CI mode: uses pre-built images (OMNIAGENT_IMAGE, DASHBOARD_IMAGE, TOOLBOX_IMAGE must be set)
WORKSPACE_DIR=/path/to/workspace python3 deploy.py ci

# Hybrid mode: builds the production Dockerfile (its builder stage runs the quality gates)
python3 deploy.py hybrid

# Just run tests (stack must already be up)
python3 deploy.py test
```

The script generates `omni.env` with random passwords, starts services, runs
migrations, and executes the integration test suite (`scripts/tests.py`) twice.

## Dev vs Stable stacks

- **`omnidev.py` / `omnistable.py`** manage the two long-lived compose projects
  (`omnidev`, `omnistable`). `omnidev.py setup` / `omnistable.py setup` call
  `shared.setup()`, the ONLY caller that writes real LLM key refs
  (`$secret:DEEPSEEK_API_KEY` etc. into `plugins.yml`) from `secrets.env`.
- **`deploy.py` is a self-contained test harness** that must NEVER use real LLM keys —
  see AGENTS.md for the hard rule. It calls only `shared.init()` + `shared.run_tests()`.

## Integration suite (`scripts/tests.py`)

Groups 1–49 cover: dashboard page loading, plugin lifecycle (install/enable/remove/
update), kanban CRUD + dispatch, cron schedules, channel/board/workflow resolution,
and the kanban-workflow feature groups:

| Group | Coverage |
|-------|----------|
| 40 | Workflow role mode (`agent`/`action`) + `auto_approve` + `review_on_fail` |
| 41 | Fail-thread routing (`review_on_fail`) + double-normalization fix |
| 42 | Plugins `omni_dir` config field (no hardcoded `/opt/omni` fallbacks) |
| 43 | Sub-prompts — pending user prompts appended to running thread |
| 44 | Builtin `omniagent-api` via test-tool-caller + fetch method gating |
| 45 | Wiki data source skill (Karpathy + Obsidian + filesystem examples) |
| 46 | models.yml provider/model overrides (CRUD API + plugin-less + absent-file + refresh upsert) |
| 47 | Resolve fallback fields ONCE at load — kanban task defaults (task → board → channel → global) |
| 48 | Single-instance advisory lock + CLI arg handling |
| 49 | omni-dashboard UI/UX fixes regression (DB page, custom selects, workflow defaults, hooks, templates, red cancel, plugin remove, git box) |

## CI/CD

Single `publish.yml` workflow triggered on push to `stable` or `v*` tags:
1. Builds omniagent, omni-dashboard, and toolbox images
2. Runs unit tests + lint
3. Runs integration tests via `deploy.py ci`
4. Tags git repos (omni-stack, omniagent, omni-dashboard) with the release version
5. Pushes + tags **nexuslbs/omni-plugins** — its root `models.yml` provides the
   plugin-less provider definitions used by the release (the noop test provider is
   sourced from omni-plugins, not the omniagent image)
6. Publishes all three images to GHCR

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKSPACE_DIR` | `/opt/workspace` | Directory containing `omni-stack/`, `omniagent/`, `omni-dashboard/` |
| `OMNIAGENT_IMAGE` | (required for ci) | Pre-built omniagent image reference |
| `DASHBOARD_IMAGE` | (required for ci) | Pre-built dashboard image reference |
| `TOOLBOX_IMAGE` | (required for ci) | Pre-built toolbox image reference |

Release-loop convention: kanban tasks → `deploy.py dev` verification → push `main` →
promote to `stable` (triggers publish). Doc/config-only changes push straight to `main`.
