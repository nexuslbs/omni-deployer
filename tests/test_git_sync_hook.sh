#!/bin/bash
# Regression test: git sync hook in toolbox backup.sh (final step) and
# restore_backup.sh (first step) - task_omnidev_backup_restore_git_sync_hook_fix.
#
# Gates covered:
#   (a) backup ends with the git sync call; restore starts with it
#   (b) sync is skipped (with an explanatory log) when the omniagent
#       container is not running - omniagent is never started just for this
#   (c) fail-soft: a sync failure is logged and backup/restore continues
#
# Runs the REAL scripts against stubbed docker/curl/rclone in a temp sandbox.
# The scripts under test live in the omni-stack toolbox service
# (services/toolbox/scripts); this harness lives in omni-deployer/tests/.
# Point TOOLBOX_DIR at a checkout containing services/toolbox/scripts
# (default: ../omni-stack sibling of this repo, else /opt/workspace/omni-stack).
# Usage: bash tests/test_git_sync_hook.sh   (from omni-deployer/)
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -z "${TOOLBOX_DIR:-}" ]; then
  if [ -d "$HERE/../omni-stack/services/toolbox/scripts" ]; then
    TOOLBOX_DIR="$(cd "$HERE/../omni-stack/services/toolbox" && pwd)"
  else
    TOOLBOX_DIR="/opt/workspace/omni-stack/services/toolbox"
  fi
fi
BACKUP="$TOOLBOX_DIR/scripts/backup.sh"
RESTORE="$TOOLBOX_DIR/scripts/restore_backup.sh"

if [ ! -x "$BACKUP" ] || [ ! -x "$RESTORE" ]; then
  echo "FAIL: scripts not found in toolbox dir ($BACKUP / $RESTORE)" >&2
  echo "      set TOOLBOX_DIR to a checkout containing services/toolbox/scripts" >&2
  exit 1
fi

SCRATCH="$(mktemp -d /tmp/hooktest-XXXXXX)"
trap 'rm -rf "$SCRATCH"' EXIT
STUBBIN="$SCRATCH/bin"
mkdir -p "$STUBBIN" "$SCRATCH/omni/data/backups" "$SCRATCH/omni/data/credentials"
export MODE_FILE="$SCRATCH/mode"

cat > "$SCRATCH/omni/.env" <<'EOF'
S3_ACCESS_KEY=test-key
S3_SECRET_KEY=test-secret
EOF

cat > "$STUBBIN/docker" <<'EOF'
#!/bin/bash
if [ "${1:-}" = "inspect" ]; then exit 1; fi   # no compose label -> PROJECT=omni-stack
if [ "${1:-}" = "ps" ]; then
  if echo "$*" | grep -q "name=omniagent"; then
    if [ -f "${MODE_FILE:-/none}" ] && grep -q "noagent" "${MODE_FILE}"; then exit 0; fi
    echo "abc123omniagent"
  fi
  exit 0
fi
if [ "${1:-}" = "volume" ] && [ "${2:-}" = "ls" ]; then exit 1; fi
if [ "${1:-}" = "stop" ] || [ "${1:-}" = "start" ] || [ "${1:-}" = "exec" ] || [ "${1:-}" = "cp" ] || [ "${1:-}" = "run" ]; then exit 0; fi
exit 0
EOF

cat > "$STUBBIN/curl" <<'EOF'
#!/bin/bash
if echo "$*" | grep -q "/git/sync"; then
  if [ -f "${MODE_FILE:-/none}" ] && grep -q "curlfail" "${MODE_FILE}"; then
    echo "curl: (22) The requested URL returned error: 500" >&2
    exit 22
  fi
  echo '{"success":true,"tool":"git_sync","output":"Sync complete"}'
  exit 0
fi
if echo "$*" | grep -q "/health"; then
  echo '{"status":"ok"}'
  exit 0
fi
exit 0
EOF

cat > "$STUBBIN/rclone" <<'EOF'
#!/bin/bash
exit 0
EOF
chmod +x "$STUBBIN/docker" "$STUBBIN/curl" "$STUBBIN/rclone"

export PATH="$STUBBIN:$PATH"
export OMNI_DIR="$SCRATCH/omni"
export COMPOSE_PROFILES=""
unset POSTGRES_PASSWORD MM_POSTGRES_PASSWORD S3_PATH S3_ACCESS_KEY S3_SECRET_KEY

PASS=0; FAIL=0
check() { if grep -q "$2" "$3"; then echo "PASS: $1"; PASS=$((PASS+1)); else echo "FAIL: $1 (missing: $2)"; FAIL=$((FAIL+1)); fi; }
order() { # name file stepA stepB
  local A B
  A=$(grep -n "$3" "$2" | head -1 | cut -d: -f1)
  B=$(grep -n "$4" "$2" | head -1 | cut -d: -f1)
  if [ -n "$A" ] && [ -n "$B" ] && [ "$A" -lt "$B" ]; then
    echo "PASS: $1 ($3 line $A < $4 line $B)"; PASS=$((PASS+1))
  else
    echo "FAIL: $1 ($3=$A $4=$B)"; FAIL=$((FAIL+1))
  fi
}

echo "=== 1: backup, omniagent RUNNING, sync OK ==="
rm -f "$MODE_FILE"
OUT1="$SCRATCH/backup_ok.log"
bash "$BACKUP" >"$OUT1" 2>&1
check "backup completes (ok path)" "Backup complete" "$OUT1"
check "backup sync OK logged" "Git sync OK" "$OUT1"
order "backup sync is FINAL step (Step 5 < Step 6 < complete)" "$OUT1" "Step 5/6" "Step 6/6"
order "backup sync before completion banner" "$OUT1" "Step 6/6" "Backup complete"

echo "=== 2: backup, omniagent NOT running -> skip + log, still completes ==="
echo noagent > "$MODE_FILE"
OUT2="$SCRATCH/backup_skip.log"
bash "$BACKUP" >"$OUT2" 2>&1
check "backup completes (skip path)" "Backup complete" "$OUT2"
check "backup skip logged" "SKIPPING git sync" "$OUT2"
check "backup skip reason logged" "omniagent container not running" "$OUT2"

echo "=== 3: backup, curl FAILS -> WARNING + continue ==="
echo curlfail > "$MODE_FILE"
OUT3="$SCRATCH/backup_fail.log"
bash "$BACKUP" >"$OUT3" 2>&1
check "backup completes (fail-soft path)" "Backup complete" "$OUT3"
check "backup fail-soft WARNING" "WARNING: git sync failed - backup continues" "$OUT3"

echo "=== 4: restore, omniagent RUNNING, sync OK (FIRST step) ==="
rm -f "$MODE_FILE"
OUT4="$SCRATCH/restore_ok.log"
bash "$RESTORE" >"$OUT4" 2>&1
check "restore completes" "Restore complete" "$OUT4"
check "restore sync OK logged" "Git sync OK" "$OUT4"
order "restore sync is FIRST step (Step 0 < Step 1)" "$OUT4" "Step 0/8" "Step 1/8"

echo "=== 5: restore, omniagent NOT running -> skip, restore still completes ==="
echo noagent > "$MODE_FILE"
OUT5="$SCRATCH/restore_skip.log"
bash "$RESTORE" >"$OUT5" 2>&1
check "restore completes (skip path)" "Restore complete" "$OUT5"
check "restore skip logged" "SKIPPING git sync" "$OUT5"

echo "=== 6: restore, curl FAILS -> WARNING + continue ==="
echo curlfail > "$MODE_FILE"
OUT6="$SCRATCH/restore_fail.log"
bash "$RESTORE" >"$OUT6" 2>&1
check "restore completes (fail-soft path)" "Restore complete" "$OUT6"
check "restore fail-soft WARNING" "WARNING: git sync failed - restore continues" "$OUT6"

echo ""
echo "HOOKTEST_RESULT: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
