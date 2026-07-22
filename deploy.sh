#!/usr/bin/env bash
set -euo pipefail

MODE="${1:?Usage: $0 <local|ci>}"
if [[ "$MODE" != "local" && "$MODE" != "ci" ]]; then
    echo "ERROR: mode must be 'local' or 'ci', got: $MODE" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE_DIR="${WORKSPACE_DIR:-/opt/workspace}"
OMNI_STACK_DIR="$WORKSPACE_DIR/omni-stack"

# Validate workspace layout
for d in omni-stack; do
    if [[ ! -d "$WORKSPACE_DIR/$d" ]]; then
        echo "ERROR: $WORKSPACE_DIR/$d not found" >&2
        exit 1
    fi
done

# Generate omni.env
OMNI_ENV="$SCRIPT_DIR/omni.env"
P1=$(openssl rand -base64 32 2>/dev/null | tr -dc 'a-zA-Z0-9' | head -c 32)
P2=$(openssl rand -base64 32 2>/dev/null | tr -dc 'a-zA-Z0-9' | head -c 32)

cat > "$OMNI_ENV" <<OMNIEOF
COMPOSE_PROJECT_NAME=omnidev
COMPOSE_PROFILES=mattermost,noop
POSTGRES_PASSWORD=$P1
MM_POSTGRES_PASSWORD=$P2
OMNIEOF

if [[ "$MODE" == "ci" ]]; then
    cat >> "$OMNI_ENV" <<CIEOF
OMNIAGENT_IMAGE=${OMNIAGENT_IMAGE:?}
DASHBOARD_IMAGE=${DASHBOARD_IMAGE:?}
TOOLBOX_IMAGE=${TOOLBOX_IMAGE:?}
CIEOF
fi

echo "=== Generated $OMNI_ENV ==="
head -4 "$OMNI_ENV"

# Build compose file list
COMPOSE_FILES=("-f" "$OMNI_STACK_DIR/docker-compose.yml")
if [[ "$MODE" == "local" ]]; then
    COMPOSE_FILES+=("-f" "$OMNI_STACK_DIR/docker-compose.dev.yml")
fi

# Step 1: Stop everything, remove all volumes
echo "=== Stopping services and removing volumes ==="
docker compose "${COMPOSE_FILES[@]}" --env-file "$OMNI_ENV" down -v 2>&1 || true

# Step 2 (local only): Build images
if [[ "$MODE" == "local" ]]; then
    echo "=== Building omniagent (dev mode) ==="
    docker compose "${COMPOSE_FILES[@]}" --env-file "$OMNI_ENV" build omniagent 2>&1

    echo "=== Building dashboard (dev mode) ==="
    docker compose "${COMPOSE_FILES[@]}" --env-file "$OMNI_ENV" build dashboard 2>&1
fi

# Step 3: Start only DB services
echo "=== Starting database services ==="
docker compose "${COMPOSE_FILES[@]}" --env-file "$OMNI_ENV" up -d postgres mattermost-db 2>&1

# Step 4: Wait for databases to be healthy
echo "=== Waiting for databases... ==="
for i in $(seq 1 30); do
    if docker compose "${COMPOSE_FILES[@]}" --env-file "$OMNI_ENV" exec -T postgres pg_isready -U omniagent -d omniagent 2>/dev/null; then
        echo "postgres is healthy"
        break
    fi
    if [[ $i -eq 30 ]]; then
        echo "ERROR: postgres did not become healthy" >&2
        exit 1
    fi
    sleep 2
done

for i in $(seq 1 30); do
    if docker compose "${COMPOSE_FILES[@]}" --env-file "$OMNI_ENV" exec -T mattermost-db pg_isready -U mmuser -d mattermost 2>/dev/null; then
        echo "mattermost-db is healthy"
        break
    fi
    if [[ $i -eq 30 ]]; then
        echo "ERROR: mattermost-db did not become healthy" >&2
        exit 1
    fi
    sleep 2
done

# Step 5: Run migrations
echo "=== Running migrations ==="
if docker compose "${COMPOSE_FILES[@]}" --env-file "$OMNI_ENV" run --rm omniagent test -f /app/target/release/db-migrations 2>/dev/null; then
    docker compose "${COMPOSE_FILES[@]}" --env-file "$OMNI_ENV" run --rm omniagent /app/target/release/db-migrations 2>&1
else
    docker compose "${COMPOSE_FILES[@]}" --env-file "$OMNI_ENV" run --rm omniagent cargo run --release -p db-migrations 2>&1
fi

# Step 6: Start all services
echo "=== Starting all services ==="
docker compose "${COMPOSE_FILES[@]}" --env-file "$OMNI_ENV" up -d 2>&1

# Step 7: Wait for omniagent
echo "=== Waiting for omniagent... ==="
for i in $(seq 1 60); do
    if curl -sf http://localhost:8080/api/health 2>/dev/null; then
        echo "omniagent is ready"
        break
    fi
    if [[ $i -eq 60 ]]; then
        echo "ERROR: omniagent did not become healthy" >&2
        docker compose "${COMPOSE_FILES[@]}" --env-file "$OMNI_ENV" logs --tail=30 omniagent 2>&1
        exit 1
    fi
    sleep 2
done

# Extra wait for dashboard
sleep 3

# Step 8: Run tests twice
CONTAINER_NAME="omnidev-omniagent-1"
TESTS_PATH="/opt/workspace/omni-deployer/scripts/tests.py"

echo ""
echo "================================================"
echo "  TEST PASS 1"
echo "================================================"
docker exec -e PYTHONUNBUFFERED=1 "$CONTAINER_NAME" python3 -u "$TESTS_PATH"

echo ""
echo "================================================"
echo "  TEST PASS 2"
echo "================================================"
docker exec -e PYTHONUNBUFFERED=1 "$CONTAINER_NAME" python3 -u "$TESTS_PATH"

echo ""
echo "================================================"
echo "  ALL TESTS PASSED"
echo "================================================"
