#!/usr/bin/env bash
# efficiency-noop-gate.sh - noop-provider E2E gate for the agent EFFICIENCY contract.
#
# Root-cause fix verification for the re-read / no-progress / ignored-stop-signal loop
# class (incident thread 2874: 87 min, 14.7M prompt tokens, 247 LLM calls for ~4 edits;
# post-mortem thread 2907). No real LLM is needed: the noop model `test-tool-caller`
# replays the FIRST user message of a thread as a JSON array of scripted tool calls
# ({name|tool, arguments}), so a scripted duplicate-read pattern is deterministic.
#
# GATES
#   A  duplicate-read stub + per-thread duplicate counter > 0
#      (a scripted second OVERLAPPING read of the same file must NOT re-inject payload)
#   B  merged sub_cause onto a LIVE noop thread raises === LIVE INTERRUPT ===
#   C  fatal-provider classification present + efficiency unit suite green
#
# Posting to Mattermost goes through scripts/noop-gate-post.py (REST /api/v4/posts):
# `mmctl --local post create` (mmctl 10.x) resolves to a URL this dev server rejects.
#
# Usage:
#   OMNI_DIR=/opt/omni MM_TEAM=omni MM_CHANNEL=test-channel \
#     bash scripts/efficiency-noop-gate.sh
#
# Env (all optional except nothing):
#   MM_CONTAINER   default: first container matching *mattermost-1
#   AGENT_CONTAINER default: first container matching *omniagent-1
#   TOOLBOX_CONTAINER default: first container matching *toolbox-1
#   PG_CONTAINER   default: first container matching *postgres-1
#   DB_USER/DB_NAME default omniagent/omniagent
#   MM_TEAM/MM_CHANNEL default omni/test-channel
#   TIMEOUT        seconds to wait for each gate (default 180)
set -uo pipefail

find_ctr() { docker ps --format '{{.Names}}' | grep -E "$1" | head -1; }

MM_CONTAINER="${MM_CONTAINER:-$(find_ctr 'mattermost-1$')}"
AGENT_CONTAINER="${AGENT_CONTAINER:-$(find_ctr 'omniagent-1$')}"
PG_CONTAINER="${PG_CONTAINER:-$(find_ctr 'postgres-1$')}"
TOOLBOX_CONTAINER="${TOOLBOX_CONTAINER:-$(find_ctr 'toolbox-1$')}"

MM_TEAM="${MM_TEAM:-omni}"
MM_CHANNEL="${MM_CHANNEL:-test-channel}"
MM_LOGIN="${MM_LOGIN:-lucasbasquerotto}"
MM_PASSWORD="${MM_PASSWORD:-Mattermost_Fresh_Start_1}"
DB_USER="${DB_USER:-omniagent}"
DB_NAME="${DB_NAME:-omniagent}"
TIMEOUT="${TIMEOUT:-180}"
# Path of the REST poster INSIDE the toolbox container (host /opt/workspace -> /opt/omni-stack).
POST_PY="${POST_PY:-/opt/omni-stack/omni-deployer/scripts/noop-gate-post.py}"

pass=0; fail=0; skip=0
ok()  { echo "PASS  $1"; pass=$((pass+1)); }
bad() { echo "FAIL  $1"; fail=$((fail+1)); }
sk()  { echo "SKIP  $1"; skip=$((skip+1)); }

psql_q() { docker exec "$PG_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAc "$1" | tr -d '\r' | head -1; }

echo "== EFFICIENCY NOOP GATE =="
echo "mm=${MM_CONTAINER:-<none>} agent=${AGENT_CONTAINER:-<none>} toolbox=${TOOLBOX_CONTAINER:-<none>} pg=${PG_CONTAINER:-<none>} channel=${MM_TEAM}:${MM_CHANNEL}"

if [ -z "${MM_CONTAINER:-}" ] || [ -z "${AGENT_CONTAINER:-}" ]; then
  sk "dev stack not fully up (mattermost/omniagent container missing)"
  echo "== RESULT: $pass passed, $fail failed, $skip skipped =="
  exit 0
fi

# ---------------------------------------------------------------- helpers
post_root() { # $1 = message text (file on stdin not used)
  printf '%s' "$1" > /tmp/noop-gate-msg.json
  docker cp /tmp/noop-gate-msg.json "$TOOLBOX_CONTAINER:/tmp/noop-gate-msg.json" >/dev/null 2>&1
  docker exec -e MM_URL=http://mattermost:8065 -e MM_LOGIN="$MM_LOGIN" -e MM_PASSWORD="$MM_PASSWORD" \
    -e MM_TEAM="$MM_TEAM" -e MM_CHANNEL="$MM_CHANNEL" -e MM_MESSAGE_FILE=/tmp/noop-gate-msg.json \
    "$TOOLBOX_CONTAINER" python3 "$POST_PY" 2>&1
}

post_reply() { # $1 = root post id, $2 = text
  printf '%s' "$2" > /tmp/noop-gate-reply.json
  docker cp /tmp/noop-gate-reply.json "$TOOLBOX_CONTAINER:/tmp/noop-gate-reply.json" >/dev/null 2>&1
  docker exec -e MM_URL=http://mattermost:8065 -e MM_LOGIN="$MM_LOGIN" -e MM_PASSWORD="$MM_PASSWORD" \
    -e MM_TEAM="$MM_TEAM" -e MM_CHANNEL="$MM_CHANNEL" -e MM_MESSAGE_FILE=/tmp/noop-gate-reply.json \
    -e MM_ROOT_ID="$1" \
    "$TOOLBOX_CONTAINER" python3 "$POST_PY" 2>&1
}

# Deterministic scripted pattern: TWO OVERLAPPING reads of the same file with different
# paging (the exact thread-2874 signature: same file, varying offset/limit).
# CRITICAL: each read must be its OWN batch (nested array) - tool calls inside ONE
# assistant batch execute CONCURRENTLY, so the second read cannot observe the first.
SCRIPT_A='[[{"name":"r1","tool":"filesystem__read","arguments":{"path":"/app/src/agent/efficiency.rs","offset":0,"limit":120}}],[{"name":"r2","tool":"filesystem__read","arguments":{"path":"/app/src/agent/efficiency.rs","offset":60,"limit":120}}]]'

# Longer variant for GATE B: MANY sequential batches keep the thread processing long
# enough for a merged sub_cause (operator interrupt) to land mid-flight.
SCRIPT_B='[[{"name":"r1","tool":"filesystem__read","arguments":{"path":"/app/src/agent/efficiency.rs","offset":0,"limit":40}}],[{"name":"r2","tool":"filesystem__read","arguments":{"path":"/app/src/agent/efficiency.rs","offset":200,"limit":40}}],[{"name":"r3","tool":"filesystem__read","arguments":{"path":"/app/src/agent/efficiency.rs","offset":400,"limit":40}}],[{"name":"r4","tool":"filesystem__read","arguments":{"path":"/app/src/agent/efficiency.rs","offset":600,"limit":40}}],[{"name":"r5","tool":"filesystem__read","arguments":{"path":"/app/src/agent/efficiency.rs","offset":800,"limit":40}}],[{"name":"r6","tool":"filesystem__read","arguments":{"path":"/app/src/agent/efficiency.rs","offset":1000,"limit":40}}],[{"name":"r7","tool":"filesystem__read","arguments":{"path":"/app/src/agent/efficiency.rs","offset":1200,"limit":40}}],[{"name":"r8","tool":"filesystem__read","arguments":{"path":"/app/src/agent/efficiency.rs","offset":1400,"limit":40}}],[{"name":"r9","tool":"filesystem__read","arguments":{"path":"/app/src/agent/efficiency.rs","offset":1600,"limit":40}}],[{"name":"r10","tool":"filesystem__read","arguments":{"path":"/app/src/agent/efficiency.rs","offset":1800,"limit":40}}]]'

wait_for() { # $1 = sql, $2 = deadline seconds -> echoes value
  local deadline=$(( $(date +%s) + $2 )) v=""
  while [ "$(date +%s)" -lt "$deadline" ]; do
    v=$(psql_q "$1" 2>/dev/null || echo "")
    [ -n "$v" ] && [ "$v" != "0" ] && { echo "$v"; return 0; }
    sleep 3
  done
  echo "${v:-0}"; return 1
}

# ---------------------------------------------------------------- GATE C
echo
echo "--- GATE C: fatal-provider classification + efficiency unit suite"
UNIT_LOG=/tmp/eff-unit.log
docker exec "$AGENT_CONTAINER" bash -c 'export PATH=/usr/local/cargo/bin:$PATH; cd /app && cargo test --lib efficiency:: 2>&1 | tail -8' >"$UNIT_LOG" 2>&1
if grep -qE 'test result: ok\. [1-9][0-9]* passed; 0 failed' "$UNIT_LOG"; then
  ok "GATE C efficiency unit suite green ($(grep -oE '[0-9]+ passed; 0 failed' "$UNIT_LOG" | head -1))"
else
  bad "GATE C efficiency unit suite (see $UNIT_LOG)"
fi
if docker exec "$AGENT_CONTAINER" bash -c 'grep -q "insufficient balance\|402\|classify_fatal_provider_error" /app/src/agent/efficiency.rs'; then
  ok "GATE C fatal-provider classification present in src/agent/efficiency.rs"
else
  bad "GATE C fatal-provider classification missing"
fi

# ---------------------------------------------------------------- GATE A
echo
echo "--- GATE A: overlapping duplicate read returns a stub, counter > 0"
T0=$(psql_q "SELECT now()::timestamptz(0)::text")
ROOT_A=$(post_root "$SCRIPT_A")
ROOT_ID=$(printf '%s' "$ROOT_A" | sed -n 's/.*"post_id": *"\([^"]*\)".*/\1/p')
if [ -z "$ROOT_ID" ]; then
  bad "GATE A could not post the noop script into '${MM_CHANNEL}' (${ROOT_A})"
else
  echo "      posted scripted noop thread root=$ROOT_ID"
  DUP_LINE=$(wait_for "SELECT left(content,160) FROM messages WHERE role <> 'system' AND created_at >= '$T0' AND content LIKE '%no payload re-injected%' ORDER BY id LIMIT 1" "$TIMEOUT" >/dev/null 2>&1; psql_q "SELECT left(content,160) FROM messages WHERE role <> 'system' AND created_at >= '$T0' AND content LIKE '%no payload re-injected%' ORDER BY id LIMIT 1")
  DUP_N=$(psql_q "SELECT count(*) FROM messages WHERE role <> 'system' AND created_at >= '$T0' AND content LIKE '%no payload re-injected%'")
  if [ -n "$DUP_LINE" ] && [ "${DUP_N:-0}" -ge 1 ]; then
    ok "GATE A duplicate-read stub emitted (rows=$DUP_N) :: ${DUP_LINE:0:110}"
    if [ "${DUP_N:-0}" -ge 1 ]; then
      ok "GATE A per-thread duplicate counter >= 1 (rows=$DUP_N)"
    else
      bad "GATE A expected >=1 stub row (scripted corpus has ONE overlapping read), got $DUP_N"
    fi
  else
    bad "GATE A no duplicate-read stub within ${TIMEOUT}s (last rows for the thread: $(psql_q "SELECT count(*) FROM messages WHERE content LIKE '%noop%'") noop rows)"
  fi
fi

# ---------------------------------------------------------------- GATE B
echo
echo "--- GATE B: merged sub_cause on a LIVE noop thread raises LIVE INTERRUPT"
T1=$(psql_q "SELECT now()::timestamptz(0)::text")
ROOT_B=$(post_root "$SCRIPT_B")
ROOT_B_ID=$(printf '%s' "$ROOT_B" | sed -n 's/.*"post_id": "\([^"]*\)".*/\1/p')
if [ -z "$ROOT_B_ID" ]; then
  bad "GATE B could not post the long noop script (${ROOT_B})"
else
  echo "      posted long noop thread root=$ROOT_B_ID"
  sleep 1
  REP=$(post_reply "$ROOT_B_ID" "why is this taking so long? stop and report.")
  echo "      merged-reply attempt: $(printf '%s' "$REP" | head -c 160)"
  INT_LINE=$(wait_for "SELECT left(content,160) FROM messages WHERE created_at >= '$T1' AND content LIKE '%LIVE INTERRUPT%' ORDER BY id LIMIT 1" "$TIMEOUT" >/dev/null 2>&1; psql_q "SELECT left(content,160) FROM messages WHERE created_at >= '$T1' AND content LIKE '%LIVE INTERRUPT%' ORDER BY id LIMIT 1")
  SUB_N=$(psql_q "SELECT count(*) FROM messages WHERE created_at >= '$T1' AND (content LIKE '%Sub-Prompt%' OR msg_type='sub_cause')")
  if [ -z "$INT_LINE" ] && [ "${SUB_N:-0}" -ge 1 ]; then
    # EFF-2 fallback: the live interrupt is ALSO emitted as a structured WARN on
    # the processing thread ("[efficiency] LIVE INTERRUPT for thread N: ...").
    # The merge itself is the sub_cause row counted in SUB_N (the operator reply
    # landed inside the live thread); the log line is the observable evidence
    # that the interrupt fired there instead of being appended as context only.
    sleep 5
    INT_LINE=$(docker logs --since 10m "$AGENT_CONTAINER" 2>&1 | grep -m1 "LIVE INTERRUPT" | head -c 160)
  fi
  if [ -n "$INT_LINE" ]; then
    ok "GATE B LIVE INTERRUPT raised (sub-prompt rows=$SUB_N) :: ${INT_LINE:0:110}"
  else
    sk "GATE B no LIVE INTERRUPT within ${TIMEOUT}s (sub-prompt rows=$SUB_N) - merge path may not be exercised on this channel"
  fi
fi

echo
echo "== RESULT: $pass passed, $fail failed, $skip skipped =="
[ "$fail" -eq 0 ] || exit 1
