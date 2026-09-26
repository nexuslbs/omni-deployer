#!/usr/bin/env bash
# efficiency-gate.sh - scripted verification gate for the agent EFFICIENCY contract
# (root-cause fix for the self-repetition / no-progress / ignored-stop-signal loop
#  class, incident thread 2874: 87 min, 14.7M prompt tokens, 247 LLM calls for a
#  ~3-edit change. Operator correction 2026-09-23: no read-only verification.)
#
# Runs with NO real LLM. Checks the four engine mechanisms (EFF-1..EFF-4), their
# unit tests, the C1/C3 removals, the agent-facing contract in the generated
# prompt, and the report surfacing.
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
DEV_OVERLAY="${DEV_OVERLAY:-/opt/omni/docker-compose.dev.yml}"
ENV_FILE="${ENV_FILE:-/opt/omni/.env}"
CONTAINER_SERVICE="${CONTAINER_SERVICE:-omniagent}"
SKIP_TESTS="${SKIP_TESTS:-0}"

pass=0; fail=0
ok()   { echo "PASS  $1"; pass=$((pass+1)); }
bad()  { echo "FAIL  $1"; fail=$((fail+1)); }
have() { grep -q -- "$2" "$1" 2>/dev/null; }
absent() { grep -qE -- "$2" "$1" 2>/dev/null && return 1 || return 0; }

echo "== EFFICIENCY GATE =="
echo "repo=$REPO omni_dir=$OMNI_DIR channel=$CHANNEL"
echo

# ---------------------------------------------------------------- EFF-1..4 markers
SRC="$REPO/src/agent/efficiency.rs"
LOOP="$REPO/src/agent/main_loop.rs"
[ -f "$SRC" ] || { bad "EFF: $SRC missing"; exit 1; }

for m in "pub struct CallLedger" "pub fn canonical_args" "pub fn duplicate_stub" \
         "pub fn progress_signal" "pub fn result_reports_state_change" \
         "pub fn strip_reserved" "pub fn classify_provider_error"; do
  have "$SRC" "$m" && ok "EFF module has: $m" || bad "EFF module missing: $m"
done

# EFF-1 duplicate guard wired into the tool dispatch path + persisted stub.
have "$LOOP" "duplicate invocation blocked" && ok "EFF-1 duplicate stub wired in the tool path" || bad "EFF-1 duplicate stub not wired"
have "$LOOP" "eff_ledger.observe" && ok "EFF-1 ledger consulted before dispatch" || bad "EFF-1 ledger not consulted"
have "$LOOP" '"eff": "duplicate-call"' && ok "EFF-1 duplicate stub persisted with a structured marker" || bad "EFF-1 stub not persisted"
# EFF-2 progress signal (duplicates only) and EFF-3 live interrupt.
have "$LOOP" "progress_signal"   && ok "EFF-2 progress signal wired" || bad "EFF-2 progress signal missing"
have "$LOOP" "Sub-Prompt"        && ok "EFF-3 marker 'Sub-Prompt' in main_loop" || bad "EFF-3 merged sub-prompt detection missing"
have "$LOOP" "LIVE INTERRUPT"    && ok "EFF-3 live-interrupt directive wired" || bad "EFF-3 live interrupt text missing"
have "$LOOP" "classify_provider_error" && ok "EFF-4 fast-fail wired before the retry counter" || bad "EFF-4 fast-fail not wired"
have "$LOOP" "metrics_summary"   && ok "counters surfaced through metrics_summary()" || bad "metrics_summary not logged"

# No hard iteration/token cap may be the remedy (explicit non-goal of the task).
if grep -qE "min\(base,\s*12\)" "$SRC" "$LOOP" 2>/dev/null; then
  bad "mechanical min(base,12)-style narrowing present (forbidden remedy)"
else
  ok "no min(base,12)-style mechanical narrowing"
fi

# ------------------------------------------------- C1: no read-only verification
# The whole read-only classification/verification path must be GONE from the
# agent loop and the registry. (`src/db/readonly.rs` is the unrelated SQL guard.)
C1_HITS="$(grep -rnE "read_only_streak|readonly_streak|read:write|read_write_ratio|read_only_ratio|guarded_read_only|is_guarded_read_only|repeat_guard|scope_for_invocation|scope_from_decl|pages_overlap|ReadScope" \
  "$REPO/src/agent" "$REPO/src/mcp" 2>/dev/null || true)"
if [ -z "$C1_HITS" ]; then
  ok "C1: no read-only verification/classification machinery in the agent loop or registry"
else
  bad "C1: read-only machinery still present:"; echo "$C1_HITS" | sed 's/^/      /'
fi
C1_LOOP="$(grep -rnE "read.?only" "$SRC" 2>/dev/null || true)"
if [ -z "$C1_LOOP" ]; then
  ok "C1: the efficiency module mentions no read-only concept"
else
  bad "C1: efficiency module still mentions read-only:"; echo "$C1_LOOP" | sed 's/^/      /'
fi
C1_PLUGINS="$(grep -rn "repeat_guard" "$REPO/plugins/tools" 2>/dev/null || true)"
if [ -z "$C1_PLUGINS" ]; then
  ok "C1: plugin manifests carry no repeat-guard declaration"
else
  bad "C1: plugin manifests still declare the removed guard:"; echo "$C1_PLUGINS" | sed 's/^/      /'
fi

# ------------------------------------------------- C3: genericity of the core
gen="$REPO/../omni-deployer/scripts/efficiency-genericity-gate.py"
if [ -f "$gen" ]; then
  if python3 "$gen" "$REPO" > /tmp/eff-genericity.log 2>&1; then
    ok "C3: core production diff carries no plugin/tool-name literal"
  else
    bad "C3: genericity gate failed - see /tmp/eff-genericity.log"
    tail -20 /tmp/eff-genericity.log | sed 's/^/      /'
  fi
fi
if grep -qE '"(docker|ssh|git|filesystem|search|notes|subtasks|skills|memory|tasks|workbench|fetch|prompt|plugin-manager|core)_' "$SRC"; then
  bad "C3: the efficiency module contains a hardcoded tool name"
else
  ok "C3: the efficiency module contains no hardcoded tool name"
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
      && ok "efficiency unit tests pass (ledger/genericity/invalidation/stub/402)" \
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
absent "$MEM" "read:write|readonly_streak|duplicate_reads" \
  && ok "MEMORY.md carries no read-only accounting vocabulary" \
  || bad "MEMORY.md still carries read-only accounting vocabulary"

if command -v curl >/dev/null 2>&1; then
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
  if have "$f" "duplicate_calls\|duplicate call"; then ok "template $t carries the duplicate-call gate"
  else bad "template $t has no duplicate-call gate"; fi
done
have "$OMNI_DIR/profiles/omni/templates/daily-report-template.md" "Step 6b" \
  && ok "daily report carries Step 6b loop-regression watch" \
  || bad "daily report has no efficiency/loop-regression section"

# ----------------------------------------------------------------- skill + wiki
[ -f "$OMNI_DIR/profiles/omni/skills/general/agent-efficiency-read-once/SKILL.md" ] \
  && ok "read-once skill present" || bad "read-once skill missing"
[ -f "$OMNI_DIR/wiki/Reference/Omniagent/Efficiency-Contract.md" ] \
  && ok "wiki Reference/Omniagent/Efficiency-Contract.md present" \
  || bad "efficiency contract wiki page missing"

# ------------------------------------------------------------------ metrics gate
M="$REPO/../omni-deployer/scripts/efficiency-metrics.py"
if [ -f "$M" ]; then
  if grep -q "duplicate_calls" "$M" && ! grep -qE "READ_ONLY|read_write_ratio" "$M"; then
    ok "metrics script reports duplicate_calls and carries no read-only ratio"
  else
    bad "metrics script still carries the removed read-only accounting"
  fi
fi

echo
echo "== RESULT: $pass passed, $fail failed =="
[ "$fail" -eq 0 ] || exit 1
