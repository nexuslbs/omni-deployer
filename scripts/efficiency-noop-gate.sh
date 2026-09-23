#!/usr/bin/env bash
# efficiency-noop-gate.sh - noop-provider E2E gate for the agent EFFICIENCY contract.
#
# Root-cause fix verification for the re-read / no-progress / ignored-stop-signal loop
# class (incident thread 2874: 87 min, 14.7M prompt tokens, 247 LLM calls for ~4 edits;
# post-mortem thread 2907). No real LLM is needed: the noop model `test-tool-caller`
# replays the FIRST user message as a JSON array of tool calls, so the exact thread-2874
# duplicate-read pattern can be scripted.
#
# GATES
#   A duplicate-read stub + counter : the SAME file read 4x with OVERLAPPING (not
#     byte-identical) ranges must yield "[duplicate read" stubs and a thread
#     duplicate-read counter > 0.
#   B merged sub_cause on a LIVE thread : a reply posted into the still-processing
#     thread must raise the "=== LIVE INTERRUPT ===" marker (no 35-min silence).
#   C fatal provider error fast-fail : `cargo test --lib efficiency::` (402 /
#     insufficient-balance / auth classification, single-attempt path).
#
# Needs the dev stack UP (noop + mattermost + omniagent). Uses the DEDICATED
# noop/test-tool-caller channel (mmctl --local, server-side, no bot token needed).
set -uo pipefail

OMNI_DIR="${OMNI_DIR:-/opt/omni}"
PG_CONTAINER="${PG_CONTAINER:-omnidev-postgres}"
MM_CONTAINER="${MM_CONTAINER:-$(docker ps --format '{{.Names}}' | grep -E 'mattermost' | grep -viE 'db|postgres' | head -1)}"
AGENT_CONTAINER="${AGENT_CONTAINER:-$(docker ps --format '{{.Names}}' | grep -E 'omniagent' | head -1)}"
MM_CHANNEL_NAME="${MM_CHANNEL_NAME:-test-channel}"
DB_USER="${DB_USER:-omniagent}"
DB_NAME="${DB_NAME:-omniagent}"
TIMEOUT="${TIMEOUT:-120}"

pass=0; fail=0; skip=0
ok()  { echo "PASS  $1"; pass=$((pass+1)); }
bad() { echo "FAIL  $1"; fail=$((fail+1)); }
sk()  { echo "SKIP  $1"; skip=$((skip+1)); }
psql_q() { docker exec "$PG_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAc "$1" 2>/dev/null; }

echo "== EFFICIENCY NOOP GATE =="
echo "mm=$MM_CONTAINER agent=$AGENT_CONTAINER channel=$MM_CHANNEL_NAME pg=$PG_CONTAINER"

if [ -z "$MM_CONTAINER" ] || [ -z "$AGENT_CONTAINER" ]; then
  sk "dev stack not fully up (mattermost/omniagent container missing)"; echo; echo "== RESULT: $pass passed, $fail failed, $skip skipped =="; exit 1
fi

MARK="effgate$(date +%s)"
T0=$(date -u +%Y-%m-%dT%H:%M:%S)

# --- scripted pattern: SAME file, overlapping ranges (thread-2874 signature) -------
SCRIPT=$(cat <<JSON
[{"tool":"filesystem_read","arguments":{"path":"/opt/omni/README.md","limit":120}},
 {"tool":"filesystem_read","arguments":{"path":"/opt/omni/README.md","offset":50,"limit":300}},
 {"tool":"filesystem_read","arguments":{"path":"/opt/omni/README.md","offset":80,"limit":200}},
 {"tool":"filesystem_read","arguments":{"path":"/opt/omni/README.md","offset":60,"limit":150}},
 {"tool":"notes_note_write","arguments":{"name":"${MARK}.md","content":"effgate ${MARK}"}},
 {"tool":"notes_note_read","arguments":{"name":"${MARK}.md"}}]
JSON
)

# mmctl >= 10: the channel is a POSITIONAL argument (not --channel).
POST_OUT=$(docker exec "$MM_CONTAINER" mmctl --local post create "$MM_CHANNEL_NAME" \
    --message "$SCRIPT" 2>&1)
ROOT_ID=$(printf '%s' "$POST_OUT" | grep -oE '"id": *"[a-z0-9]{26}"' | head -1 | grep -oE '[a-z0-9]{26}')
if [ -z "$ROOT_ID" ]; then
  bad "could not post the noop script into '$MM_CHANNEL_NAME' ($POST_OUT)"
else
  echo "posted scripted thread root=$ROOT_ID mark=$MARK"

  # --------------------------------------------------------------- GATE A
  deadline=$(( $(date +%s) + TIMEOUT )); dups=0
  while [ "$(date +%s)" -lt "$deadline" ]; do
    dups=$(psql_q "SELECT count(*) FROM messages WHERE created_at >= '$T0' AND content LIKE '%duplicate read%';" || echo 0)
    [ -n "$dups" ] && [ "${dups:-0}" -gt 0 ] && break
    sleep 3
  done
  if [ "${dups:-0}" -gt 0 ]; then
    ok "GATE A duplicate-read stub injected; thread duplicate-read counter=$dups (>0)"
    psql_q "SELECT left(content,150) FROM messages WHERE content LIKE '%duplicate read%' AND created_at >= '$T0' ORDER BY id LIMIT 2;" | sed 's/^/       /'
  else
    bad "GATE A no '[duplicate read' stub since $T0 (overlapping-range re-read NOT blocked)"
  fi

  # --------------------------------------------------------------- GATE B
  sleep 2
  docker exec "$MM_CONTAINER" mmctl --local post create "$MM_CHANNEL_NAME" --root "$ROOT_ID" \
      --message "[sub_cause] why are you taking so long? stop and report what you have." >/dev/null 2>&1
  echo "posted merged sub_cause into the LIVE thread"
  deadline=$(( $(date +%s) + TIMEOUT )); li=0
  while [ "$(date +%s)" -lt "$deadline" ]; do
    li=$(psql_q "SELECT count(*) FROM messages WHERE created_at >= '$T0' AND content LIKE '%LIVE INTERRUPT%';" || echo 0)
    [ -n "$li" ] && [ "${li:-0}" -gt 0 ] && break
    sleep 3
  done
  if [ "${li:-0}" -gt 0 ]; then
    ok "GATE B merged sub_cause raised LIVE INTERRUPT on the running thread (count=$li)"
  else
    bad "GATE B no 'LIVE INTERRUPT' marker after a merged sub_cause (interrupt did not fire)"
  fi
fi

# --------------------------------------------------------------- GATE C
SRC="/opt/workspace/omniagent/src/agent/efficiency.rs"
static_ok=0
[ -f "$SRC" ] && grep -q 'classify_fatal_provider_error' "$SRC" && grep -q 'insufficient' "$SRC" && static_ok=1
docker exec "$AGENT_CONTAINER" bash -lc "cd /app && cargo test --lib efficiency:: 2>&1 | tail -6" >/tmp/eff-unit.log 2>&1
unit_ok=0
grep -q 'test result: ok' /tmp/eff-unit.log && ! grep -qE '[1-9][0-9]* failed' /tmp/eff-unit.log && unit_ok=1
if [ "$static_ok" = 1 ] && [ "$unit_ok" = 1 ]; then
  ok "GATE C fatal-provider classification present + efficiency unit suite green"
  grep -E '^test result' /tmp/eff-unit.log | sed 's/^/       /'
else
  bad "GATE C static_ok=$static_ok unit_ok=$unit_ok (see /tmp/eff-unit.log)"
fi

echo
echo "== RESULT: $pass passed, $fail failed, $skip skipped =="
[ "$fail" -eq 0 ] || exit 1
