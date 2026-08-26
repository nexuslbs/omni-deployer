#!/usr/bin/env bash
# Launch the deploy-runner container using the EXISTING `omniagent` service
# from the omni-stack root docker-compose.yml - NO separate compose file
# (user rule 2026-08-22: omni-deployer has no compose files, and the runner
# should reuse the stack's own service definition).
#
# Why a DEDICATED PROJECT name ("deployrunner") even though the service is
# shared: deploy.py dev tears down the omnideploy project (down + volume
# wipes + rebuild) and stop_other_stacks stops omnidev/omnistable. The
# runner must live in its OWN compose project so the deploy never kills the
# container it runs from.
#
# Usage:
#   scripts/run-deploy.sh            # start the runner (idempotent, detached)
#   scripts/run-deploy.sh stop       # remove the runner container
#
# Then run the deploy inside it:
#   docker exec deployrunner-deployer-1 bash -c \
#     "cd /opt/workspace/omni-deployer && python3 deploy.py dev"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOYER_DIR="$(dirname "$SCRIPT_DIR")"
STACK_DIR="${OMNI_STACK_DIR:-/opt/workspace/omni-stack}"
NAME="deployrunner-deployer-1"

COMPOSE=(docker compose -p deployrunner)
COMPOSE+=(-f "$STACK_DIR/docker-compose.yml" -f "$STACK_DIR/docker-compose.dev.yml")
if [ -f "$DEPLOYER_DIR/omni.env" ]; then
  COMPOSE+=(--env-file "$DEPLOYER_DIR/omni.env")
fi

if [ "${1:-}" = "stop" ]; then
  docker rm -f "$NAME" 2>/dev/null || true
  echo "runner removed: $NAME"
  exit 0
fi

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "runner already up: $NAME"
else
  # --no-deps: never start postgres/qdrant - the runner only needs the image
  # + mounts (docker.sock, /opt/workspace). --entrypoint tail keeps it alive
  # for `docker exec` (the kanban executor + manual runs use it).
  "${COMPOSE[@]}" run -d --no-deps --name "$NAME" --entrypoint tail omniagent -f /dev/null
  echo "runner started: $NAME (project=deployrunner, image=$(docker inspect -f '{{.Config.Image}}' "$NAME"))"
fi

echo
echo "run deploy:  docker exec $NAME bash -c 'cd /opt/workspace/omni-deployer && python3 deploy.py dev'"
echo "run chain:   docker exec $NAME bash -c 'cd /opt/workspace/omni-deployer && python3 omnistable.py setup'"
