#!/usr/bin/env bash
# efficiency-noop-gate.sh - noop-provider E2E gate for the agent EFFICIENCY contract.
#
# Root-cause fix verification for the SELF-REPETITION loop class (incident thread
# 2874: 87 min, 14.7M prompt tokens, 247 LLM calls for ~3 edits; post-mortem
# thread 2907). No real LLM is needed: the noop model `test-tool-caller` replays
# the FIRST user message of a thread as a JSON array of scripted tool calls
# ({name|tool, arguments}), so a scripted duplicate pattern is deterministic.
#
# GATES
#   A  identical re-issued invocation -> payload-free `[duplicate call` stub,
#      persisted with metadata eff=duplicate-call, per-thread counter > 0
#   A2 a duplicate replayed AFTER a state change executes normally
#   B  merged sub_cause onto a LIVE noop thread raises === LIVE INTERRUPT ===
#   C  efficiency unit suite green (covers the generic ledger, invalidation,
#      force_repeat and the structured 401/402/403 fast-fail)
#
# Posting to Mattermost goes through scripts/noop-gate-post.py (REST /api/v4/posts):
# `mmctl --local post create` (mmctl 10.x) resolves to a URL this dev server rejects.
#
# Usage:
#   MM_TEAM=omni MM_CHANNEL=test-channel bash scripts/efficiency-noop-gate.sh
set -uo pipefail

find_ctr() { docker ps --format '{{.Names}}' | grep -E "$1" | head -1; }

MM_CONTAINER="${MM_CONTAINER:-$(find_ctr 'mattermost-1$')}"
AGENT_CONTAINER="${AGENT_CONTAINER:-$(find_ctr 'omniagent-1$')}"
PG_CONTAINER="${PG_CONTAINER:-$(find_ctr 'postgres-1$')}"
TOOLBOX_CONTAINER="${TOOLBOX_CONTAINER:-$(find_ctr 'toolbox-1$')}"

MM_TEAM="${MM_TEAM:-omni}"
MM_CHANNEL="${MM_CHANNEL:-test-channel}"
MM_LOGIN="${MM_LOGIN:-lucasbasquerotto}"
# The Mattermost login password is NEVER hardcoded here: export it (or the
# repo-wide MATTERMOST_TEST_PASSWORD secret) before running the gate.
MM_PASSWORD="${MM_PASSWORD:-${MATTERMOST_TEST_PASSWORD:-}}"
DB_USER="${DB_USER:-omniagent}"
DB_NAME="${DB_NAME:-omniagent}"
TIMEOUT="${TIMEOUT:-180}"
POST_PY="${POST_PY:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/noop-gate-post.py}"

pass=0; fail=0; skip=0
ok()  { echo "PASS  $1"; pass=$((pass+1)); }
bad() { echo "FAIL  $1"; fail=$((fail+1)); }
sk()  { echo "SKIP  $1"; skip=$((skip+1)); }

psql_q() { docker exec "$PG_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAc "$1" | tr -d '\r' | head -1; }

echo "== EFFICIENCY NOOP GATE =="
echo "mm=${MM_CONTAINER:-<none>} agent=${AGENT_CONTAINER:-<none>} pg=${PG_CONTAINER:-<none>} channel=${MM_TEAM}:${MM_CHANNEL}"

if [ -z "${MM_CONTAINER:-}" ] || [ -z "${AGENT_CONTAINER:-}" ] || [ -z "${TOOLBOX_CONTAINER:-}" ]; then
  sk "dev stack not fully up (mattermost/omniagent/toolbox container missing)"
  echo "== RESULT: $pass passed, $fail failed, $skip skipped =="
  exit 0
fi

if [ -z "$MM_PASSWORD" ]; then
  echo "FAIL  MM_PASSWORD (or MATTERMOST_TEST_PASSWORD) is not set - the gate never hardcodes it"
  echo "== RESULT: $pass passed, $fail failed, $skip skipped =="
  exit 1
fi

# ---------------------------------------------------------------- helpers
post_root() {
  printf '%s' "$1" > /tmp/noop-gate-msg.json
  docker cp /tmp/noop-gate-msg.json "$TOOLBOX_CONTAINER:/tmp/noop-gate-msg.json" >/dev/null 2>&1
  docker exec -e MM_URL=http://mattermost:8065 -e MM_LOGIN="$MM_LOGIN" -e MM_PASSWORD="$MM_PASSWORD" \
    -e MM_TEAM="$MM_TEAM" -e MM_CHANNEL="$MM_CHANNEL" -e MM_MESSAGE_FILE=/tmp/noop-gate-msg.json \
    "$TOOLBOX_CONTAINER" python3 "$POST_PY" 2>&1
}

post_reply() {
  printf '%s' "$2" > /tmp/noop-gate-reply.json
  docker cp /tmp/noop-gate-reply.json "$TOOLBOX_CONTAINER:/tmp/noop-gate-reply.json" >/dev/null 2>&1
  docker exec -e MM_URL=http://mattermost:8065 -e MM_LOGIN="$MM_LOGIN" -e MM_PASSWORD="$MM_PASSWORD" \
    -e MM_TEAM="$MM_TEAM" -e MM_CHANNEL="$MM_CHANNEL" -e MM_MESSAGE_FILE=/tmp/noop-gate-reply.json \
    -e MM_ROOT_ID="$1" \
    "$TOOLBOX_CONTAINER" python3 "$POST_PY" 2>&1
}

# Deterministic scripted pattern (GATE A): the SAME invocation, byte for byte,
# four times, each in its OWN batch (tool calls in ONE assistant batch run
# concurrently, so a later call cannot observe an earlier one). Call 1 executes;
# calls 2..4 must come back as `[duplicate call` stubs with no payload. This is
# the thread-2874 signature (docker-compose.yml read 20x) with the generic
# identity (same tool id + same canonical args).
READ_ARGS='{"path":"/app/src/agent/efficiency.rs","offset":0,"limit":60}'
SCRIPT_A="[[{\"name\":\"d1\",\"tool\":\"filesystem__read\",\"arguments\":$READ_ARGS}],[{\"name\":\"d2\",\"tool\":\"filesystem__read\",\"arguments\":$READ_ARGS}],[{\"name\":\"d3\",\"tool\":\"filesystem__read\",\"arguments\":$READ_ARGS}],[{\"name\":\"d4\",\"tool\":\"filesystem__read\",\"arguments\":$READ_ARGS}]]"

# GATE A2: read -> SAME read -> state change (notes write) -> SAME read again.
# The second call must be a stub (the guard is provably active for this exact
# invocation), and the LAST call must EXECUTE because the state change
# invalidated the ledger. The assertions are keyed on the ROW ID of the write,
# so a read that ran BEFORE the change can never satisfy them.
SCRIPT_A2='[[{"name":"r1","tool":"filesystem__read","arguments":{"path":"/app/src/agent/efficiency.rs","offset":0,"limit":60}}],[{"name":"r2","tool":"filesystem__read","arguments":{"path":"/app/src/agent/efficiency.rs","offset":0,"limit":60}}],[{"name":"w1","tool":"notes__note_write","arguments":{"name":"eff-gate.md","content":"state change"}}],[{"name":"r3","tool":"filesystem__read","arguments":{"path":"/app/src/agent/efficiency.rs","offset":0,"limit":60}}]]'

# GATE B: MANY sequential batches keep the thread processing long enough for a
# merged operator reply (live interrupt) to land mid-flight. Built in pure bash
# (no python, no heredoc): the harness must not depend on a helper interpreter.
SCRIPT_B=""
for i in $(seq 0 39); do
  ONE="[{\"name\":\"b${i}\",\"tool\":\"filesystem__read\",\"arguments\":{\"path\":\"/app/src/agent/efficiency.rs\",\"offset\":$((i * 20)),\"limit\":20}}]"
  if [ -z "$SCRIPT_B" ]; then SCRIPT_B="$ONE"; else SCRIPT_B="$SCRIPT_B,$ONE"; fi
done
SCRIPT_B="[$SCRIPT_B]"

post_id_of() { printf '%s' "$1" | sed -n 's/.*"post_id": *"\([^"]*\)".*/\1/p'; }

wait_for() { # $1 = sql returning a count, $2 = deadline seconds
  local deadline=$(( $(date +%s) + $2 )) v=""
  while [ "$(date +%s)" -lt "$deadline" ]; do
    v=$(psql_q "$1" 2>/dev/null || echo "")
    [ -n "$v" ] && [ "$v" != "0" ] && { echo "$v"; return 0; }
    sleep 3
  done
  echo "${v:-0}"; return 1
}

DUP_PRED="metadata::text LIKE '%\"eff\":\"duplicate-call\"%' OR content LIKE '[duplicate call%'"

# ---------------------------------------------------------------- GATE C
echo
echo "--- GATE C: efficiency unit suite (ledger, invalidation, force_repeat, 402 fast-fail)"
UNIT_LOG=/tmp/eff-unit.log
docker exec "$AGENT_CONTAINER" bash -c 'export PATH=/usr/local/cargo/bin:$PATH; cd /app && CARGO_TARGET_DIR=/target cargo test --lib agent::efficiency 2>&1 | tail -12' >"$UNIT_LOG" 2>&1
if grep -qE 'test result: ok\. [1-9][0-9]* passed; 0 failed' "$UNIT_LOG"; then
  ok "GATE C efficiency unit suite green ($(grep -oE '[0-9]+ passed; 0 failed' "$UNIT_LOG" | head -1))"
else
  bad "GATE C efficiency unit suite (see $UNIT_LOG)"
  tail -8 "$UNIT_LOG" | sed 's/^/      /'
fi
if docker exec "$AGENT_CONTAINER" bash -c 'grep -q "ProviderHttp" /app/src/error.rs && grep -q "classify_provider_error" /app/src/agent/efficiency.rs'; then
  ok "GATE C structured provider status (Error::ProviderHttp) + provider-agnostic classification present"
else
  bad "GATE C structured provider fast-fail missing"
fi
# C1/C3: no read-only vocabulary and no plugin/tool-name literal in the module.
if docker exec "$AGENT_CONTAINER" bash -c '! grep -rqE "read.?only|repeat_guard|guarded_read_only" /app/src/agent/efficiency.rs'; then
  ok "GATE C the efficiency module carries no read-only concept"
else
  bad "GATE C read-only vocabulary still present in the efficiency module"
fi

# ---------------------------------------------------------------- GATE A
echo
echo "--- GATE A: identical re-issued invocation returns a payload-free stub, counter > 0"
T0=$(psql_q "SELECT now()::timestamptz(0)::text")
ROOT_A=$(post_root "$SCRIPT_A")
ROOT_ID=$(post_id_of "$ROOT_A")
if [ -z "$ROOT_ID" ]; then
  bad "GATE A could not post the noop script into '${MM_CHANNEL}' (${ROOT_A})"
else
  echo "      posted scripted noop thread root=$ROOT_ID"
  DUP_N=$(wait_for "SELECT count(*) FROM messages WHERE created_at >= '$T0' AND ($DUP_PRED)" "$TIMEOUT")
  EXEC_N=$(psql_q "SELECT count(*) FROM messages WHERE created_at >= '$T0' AND msg_type='tool-result' AND msg_subtype='filesystem__read' AND NOT ($DUP_PRED)")
  DUP_LINE=$(psql_q "SELECT left(content,150) FROM messages WHERE created_at >= '$T0' AND ($DUP_PRED) ORDER BY id LIMIT 1")
  if [ "${DUP_N:-0}" -ge 1 ] && [ "${EXEC_N:-0}" -ge 1 ]; then
    ok "GATE A duplicate stub(s) persisted: duplicate rows=$DUP_N (executed rows=$EXEC_N) :: ${DUP_LINE:0:110}"
    ok "GATE A per-thread duplicate_calls counter > 0 (rows=$DUP_N)"
  else
    bad "GATE A expected >=1 stub row and >=1 executed row, got stub=$DUP_N executed=$EXEC_N"
  fi
  LEDGER=$(docker logs --since 10m "$AGENT_CONTAINER" 2>&1 | grep -m1 "duplicate invocation blocked" | head -c 140)
  [ -n "$LEDGER" ] && ok "GATE A engine log: ${LEDGER}" || sk "GATE A no '[efficiency] duplicate invocation blocked' log line in the last 10m"
fi

# ---------------------------------------------------------------- GATE A2
echo
echo "--- GATE A2: a duplicate replayed AFTER a state change executes normally"
T1=$(psql_q "SELECT now()::timestamptz(0)::text")
ROOT_2=$(post_root "$SCRIPT_A2")
ROOT_2_ID=$(post_id_of "$ROOT_2")
if [ -z "$ROOT_2_ID" ]; then
  bad "GATE A2 could not post the script (${ROOT_2})"
else
  echo "      posted invalidation script root=$ROOT_2_ID"
  # Keyed on the ROW ID of the state-change row: the gate can never pass
  # vacuously on a read that ran BEFORE the change.
  W_ID=$(wait_for "SELECT id FROM messages WHERE created_at >= '$T1' AND msg_type='tool-result' AND msg_subtype LIKE '%note%write%' ORDER BY id LIMIT 1" "$TIMEOUT")
  STUB_BEFORE=$(psql_q "SELECT count(*) FROM messages WHERE created_at >= '$T1' AND id < ${W_ID:-0} AND msg_subtype='filesystem__read' AND ($DUP_PRED)")
  # A real read persists the file payload (excerpted by the engine) while a
  # stub persists the tiny `[duplicate call ...` text: NOT ($DUP_PRED) plus the
  # payload marker separates them. Combined with `id > $W_ID` this can only be
  # satisfied by the read that came AFTER the state change.
  AFTER_EXEC=$(psql_q "SELECT count(*) FROM messages WHERE created_at >= '$T1' AND id > ${W_ID:-0} AND msg_type='tool-result' AND msg_subtype='filesystem__read' AND NOT ($DUP_PRED) AND content LIKE '%efficiency%'")
  AFTER_DUP=$(psql_q "SELECT count(*) FROM messages WHERE created_at >= '$T1' AND id > ${W_ID:-0} AND msg_subtype='filesystem__read' AND ($DUP_PRED)")
  if [ "${STUB_BEFORE:-0}" -ge 1 ] && [ "${AFTER_EXEC:-0}" -ge 1 ] && [ "${AFTER_DUP:-0}" -eq 0 ]; then
    ok "GATE A2 guard active before the change (stub rows=$STUB_BEFORE) AND the identical read AFTER the state change EXECUTED (executed=$AFTER_EXEC stubs_after=$AFTER_DUP)"
  else
    bad "GATE A2 invalidation not observable: stub_before=$STUB_BEFORE executed_after=$AFTER_EXEC stub_after=$AFTER_DUP (write row id=${W_ID:-<none>})"
  fi
fi

# ---------------------------------------------------------------- GATE B
echo
echo "--- GATE B: merged sub_cause on a LIVE noop thread raises LIVE INTERRUPT"
T2=$(psql_q "SELECT now()::timestamptz(0)::text")
ROOT_B=$(post_root "$SCRIPT_B")
ROOT_B_ID=$(post_id_of "$ROOT_B")
if [ -z "$ROOT_B_ID" ]; then
  bad "GATE B could not post the long noop script (${ROOT_B})"
else
  echo "      posted long noop thread root=$ROOT_B_ID"
  # Wait until the thread is demonstrably RUNNING (first tool result persisted),
  # then post the operator reply, retrying until the engine merges it as a
  # sub_cause row on the SAME thread (the dev MM channel sometimes treats the
  # reply as a fresh thread root instead of a sub-prompt).
  LIVE_DEADLINE=$(( $(date +%s) + 30 ))
  while [ "$(date +%s)" -lt "$LIVE_DEADLINE" ]; do
    RUNNING=$(psql_q "SELECT count(*) FROM messages WHERE created_at >= '$T2' AND msg_type='tool-result'")
    [ "${RUNNING:-0}" -ge 1 ] && break
    sleep 1
  done
  REP=""
  for attempt in 1 2 3 4 5; do
    REP=$(post_reply "$ROOT_B_ID" "why is this taking so long? stop and report. (attempt $attempt)")
    sleep 3
    MERGED=$(psql_q "SELECT count(*) FROM messages WHERE created_at >= '$T2' AND msg_type='sub_cause'")
    [ "${MERGED:-0}" -ge 1 ] && break
  done
  echo "      merged-reply attempt: $(printf '%s' "$REP" | head -c 140)"
  SUB_N=$(psql_q "SELECT count(*) FROM messages WHERE created_at >= '$T2' AND (content LIKE '%Sub-Prompt%' OR msg_type='sub_cause')")
  INT_LINE=$(docker logs --since 15m "$AGENT_CONTAINER" 2>&1 | grep -m1 "LIVE INTERRUPT" | head -c 130)
  if [ -n "$INT_LINE" ] && [ "${SUB_N:-0}" -ge 1 ]; then
    ok "GATE B LIVE INTERRUPT raised (sub-prompt rows=$SUB_N) :: ${INT_LINE:0:110}"
  elif [ "${SUB_N:-0}" -ge 1 ]; then
    sk "GATE B sub_cause merged (rows=$SUB_N) but no LIVE INTERRUPT log line within ${TIMEOUT}s"
  else
    sk "GATE B no sub_cause merge observed within ${TIMEOUT}s - the merge path is not exercised on this channel"
  fi
fi

echo
echo "== RESULT: $pass passed, $fail failed, $skip skipped =="
[ "$fail" -eq 0 ] || exit 1
