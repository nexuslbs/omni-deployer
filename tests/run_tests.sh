#!/bin/sh
# Vector log-pipeline verification for the omni-stack vector service
# (services/vector: sources/transforms/sinks.toml).
#
# 1. Validates the real pipeline config (sources/transforms/sinks.toml).
# 2. Concatenates the REAL transforms.toml with the test fragment
#    (level_token_test.toml, next to this script) into one combined config and
#    runs `vector test` against it - so the suite exercises the shipped
#    transform, never a copy.
#
# The configs under test live in the omni-stack vector service
# (services/vector/*.toml); this harness lives in omni-deployer/tests/.
# Point VECTOR_SRC at a checkout containing services/vector
# (default: ../omni-stack sibling of this repo, else /opt/workspace/omni-stack).
#
# Run inside a container that has the `vector` binary (override with VECTOR_BIN,
# default /usr/bin/vector), e.g.:
#   VECTOR_SRC=/opt/workspace/omni-stack/services/vector sh tests/run_tests.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -z "${VECTOR_SRC:-}" ]; then
  if [ -d "$DIR/../omni-stack/services/vector" ]; then
    VECTOR_SRC="$(cd "$DIR/../omni-stack/services/vector" && pwd)"
  else
    VECTOR_SRC="/opt/workspace/omni-stack/services/vector"
  fi
fi
VECTOR_BIN="${VECTOR_BIN:-/usr/bin/vector}"

echo "==> Validating pipeline config (sources/transforms/sinks.toml)"
"$VECTOR_BIN" validate --no-environment \
  "$VECTOR_SRC/sources.toml" "$VECTOR_SRC/transforms.toml" "$VECTOR_SRC/sinks.toml"

COMBINED="$(mktemp /tmp/vector-level-test.XXXXXX.toml)"
trap 'rm -f "$COMBINED"' EXIT
cat "$VECTOR_SRC/transforms.toml" "$DIR/level_token_test.toml" > "$COMBINED"

echo "==> Running vector tests (combined config: transforms.toml + level_token_test.toml)"
"$VECTOR_BIN" test "$COMBINED"
echo "==> All vector tests passed"
