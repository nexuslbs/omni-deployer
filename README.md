# omni-deployer

Deployment orchestration and CI/CD for the OmniAgent stack.

This repo contains:
- **`deploy.sh`** — Orchestration script for local dev and CI runs
- **`scripts/tests.py`** — Integration tests for plugin lifecycle
- **`.github/workflows/`** — CI workflows (build, test, publish)

## Usage

```bash
# Local development mode (uses .dev.yml overrides, builds images from source)
bash deploy.sh local

# CI mode (uses pre-built images, OMNIAGENT_IMAGE, DASHBOARD_IMAGE, TOOLBOX_IMAGE must be set)
WORKSPACE_DIR=/path/to/workspace bash deploy.sh ci
```

The script:
1. Generates `omni.env` with random passwords
2. Stops all services and removes volumes
3. (local mode) Builds omniagent and dashboard from source
4. Starts database services, waits for health
5. Runs database migrations
6. Starts all services
7. Runs integration tests **twice**

## Environment

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKSPACE_DIR` | `/opt/workspace` | Directory containing `omni-stack/`, `omniagent/`, `omni-dashboard/` |
