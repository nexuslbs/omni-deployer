# omni-deployer

Deployment orchestration and CI/CD for the OmniAgent stack.

## Usage

```bash
# Local development mode (builds images from source)
python3 deploy.py local

# CI mode (uses pre-built images - OMNIAGENT_IMAGE, DASHBOARD_IMAGE, TOOLBOX_IMAGE must be set)
WORKSPACE_DIR=/path/to/workspace python3 deploy.py ci

# Just run tests (stack must already be up)
python3 deploy.py test
```

The script generates `omni.env` with random passwords, starts services,
runs migrations, and executes the integration test suite twice.

## CI/CD

Single `publish.yml` workflow triggered on push to `stable` or `v*` tags:
1. Builds omniagent, omni-dashboard, and toolbox images
2. Runs unit tests + lint
3. Runs integration tests via `deploy.py ci`
4. Tags git repos (omni-stack, omniagent, omni-dashboard) with the release version
5. Publishes all three images to GHCR

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKSPACE_DIR` | `/opt/workspace` | Directory containing `omni-stack/`, `omniagent/`, `omni-dashboard/` |
