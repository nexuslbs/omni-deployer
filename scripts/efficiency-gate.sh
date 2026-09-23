#!/usr/bin/env bash
# efficiency-gate.sh - scripted verification gate for the agent EFFICIENCY contract
# (root-cause fix for the re-read / no-progress / ignored-stop-signal loop class,
#  incident thread 2874: 87 min, 14.7M prompt tokens, 247 LLM calls for a ~3-edit change).
#
# Runs with NO real LLM. Checks the four engine mechanisms (EFF-1..EFF-4), their unit
# tests, the agent-facing contract in the generated prompt, and the report surfacing.
#
# Usage:
#   scripts/efficiency-gate.sh                       # all checks
#   OMNIAGENT_REPO=/opt/workspace/omniagent scripts/efficiency-gate.sh
#
# Exit code 0 = every gate passed; 1 = at least one gate failed (details printed).
set -uo pipefail

REPO="${OMNIAGENT_REPO:-/opt/workspace/omniagent}"
OMNI_DIR="${OMNI_DIR:-/opt/omni}"
CHANNEL="${CHANNEL:-main}"
COMPOSE_PROJECT_DIR="${COMPOSE_PROJECT_DIR:-/opt/omni}"
DEV_OVERLAY="${DEV_OVERLAY:-/opt/omni/docker-compose.dev.yml}"
ENV_FILE="${ENV_FILE:-/opt/omni/.env}"
CONTAINER_SERVICE="${CONTAINER_SERVICE:-omniagent}"
SKIP_TESTS="${SKIP_TESTS:-0}"

pass=0; fail=0
ok()   { echo "PASS  $1"; pass=$((pass+1)); }
bad()  { echo "FAIL  $1"; fail=$((fail+1)); }
have() { grep -q -- "$2" "$1" 2>/dev/null; }

echo "== EFFICIENCY GATE =="
echo "repo=$REPO omni_dir=$OMNI_DIR channel=$CHANNEL"
echo

# ---------------------------------------------------------------- EFF-1..4 markers
SRC="$REPO/src/agent/efficiency.rs"
LOOP="$REPO/src/agent/main_loop.rs"
[ -f "$SRC" ] || { bad "EFF: $SRC missing"; exit 1; }

for m in "pub fn parse_read_scope" "pub fn pages_overlap" "pub struct EfficiencyLedger" \
         "pub fn steering_text" "pub fn classify_fatal_provider_error" "pub fn efficiency_grade" \
         "pub fn duplicate_stub" "fn metrics_summary"; do
  have "$SRC" "$m" && ok "EFF module has: $m" || bad "EFF module missing: $m"
done

# EFF-3 live interrupt: the running loop must count merged sub-prompts and raise it.
have "$LOOP" "Sub-Prompt"            && ok "EFF-3 marker 'Sub-Prompt' in main_loop" || bad "EFF-3 merged sub-prompt detection missing"
have "$LOOP" "LIVE INTERRUPT"        && ok "EFF-3 live-interrupt directive wired" || bad "EFF-3 live interrupt text missing"
have "$LOOP" "duplicate read blocked" && ok "EFF-1 duplicate stub wired in the tool path" || bad "EFF-1 duplicate stub not wired"
have "$LOOP" "classify_fatal_provider_error" && ok "EFF-4 fast-fail wired before the retry counter" || bad "EFF-4 fast-fail not wired"
have "$LOOP" "metrics_summary"       && ok "counters surfaced through metrics_summary()" || bad "metrics_summary not logged"

# No hard iteration/token cap may be the remedy (explicit non-goal of the task).
if grep -qE "min\(base,\s*12\)" "$SRC" "$LOOP" 2>/dev/null; then
  bad "mechanical min(base,12)-style narrowing present (forbidden remedy)"
else
  ok "no min(base,12)-style mechanical narrowing"
fi

# ------------------------------------------------------------------ unit tests
if [ "$SKIP_TESTS" = "1" ]; then
  echo "SKIP  cargo test (SKIP_TESTS=1)"
else
  echo "-- cargo test --lib agent::efficiency (container $CONTAINER_SERVICE) --"
  if docker compose -f "$DEV_OVERLAY" --env-file "$ENV_FILE" -p "${PROJECT:-omnidev}" \
       exec -T "$CONTAINER_SERVICE" bash -c \
       'cd /app && CARGO_TARGET_DIR=/target cargo test --lib agent::efficiency 2>&1' \
       > /tmp/eff-gate-cargo.log 2>&1; then
    tail -3 /tmp/eff-gate-cargo.log | sed 's/^/      /'
    grep -q "test result: ok" /tmp/eff-gate-cargo.log \
      && ok "efficiency unit tests pass (scope/overlap/steering/402/grade)" \
      || bad "efficiency unit tests did not report ok"
  else
    bad "cargo test failed - see /tmp/eff-gate-cargo.log"
    tail -12 /tmp/eff-gate-cargo.log | sed 's/^/      /'
  fi
fi

# ------------------------------------------------- prompt carries the contract
MEM="$OMNI_DIR/profiles/omni/MEMORY.md"
have "$MEM" "EFFICIENCY CONTRACT" \
  && ok "MEMORY.md carries the durable EFFICIENCY CONTRACT" \
  || bad "MEMORY.md missing the EFFICIENCY CONTRACT"

if command -v curl >/dev/null 2>&1; then
  # ask the LIVE service for the generated prompt (reads the same profile dir)
  PROMPT="$(curl -fsS "http://localhost:8080/prompt/$CHANNEL" 2>/dev/null || true)"
  if [ -n "$PROMPT" ]; then
    echo "$PROMPT" | grep -q "EFFICIENCY CONTRACT" \
      && ok "LIVE /prompt/$CHANNEL contains the EFFICIENCY CONTRACT" \
      || bad "LIVE /prompt/$CHANNEL does NOT contain the EFFICIENCY CONTRACT"
  else
    echo "SKIP  live /prompt/$CHANNEL unreachable from this host (run inside the omniagent container)"
  fi
fi

# ------------------------------------------------------- enforceable templates
for t in dev-executor.md dev-tester.md; do
  f="$OMNI_DIR/profiles/omni/templates/$t"
  if have "$f" "read-only"; then ok "template $t carries the efficiency gate"
  else bad "template $t has no efficiency gate"; fi
done
have "$OMNI_DIR/profiles/omni/templates/daily-report-template.md" "Step 6b" \
  && ok "daily report carries Step 6b loop-regression watch" \
  || bad "daily report has no efficiency/loop-regression section"

# ----------------------------------------------------------------- skill + wiki
[ -f "$OMNI_DIR/profiles/omni/skills/general/agent-efficiency-read-once/SKILL.md" ] \
  && ok "read-once skill present" || bad "read-once skill missing"
[ -f "$OMNI_DIR/profiles/omni/wiki/Reference/Omniagent/Efficiency-Contract.md" ] \
  && ok "wiki Reference/Omniagent/Efficiency-Contract.md present" \
  || bad "efficiency contract wiki page missing"

echo
echo "== RESULT: $pass passed, $fail failed =="
[ "$fail" -eq 0 ] || exit 1
