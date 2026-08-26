#!/usr/bin/env python3
"""Dispatch follow-up: finish build/verify/commit/push of the ALREADY-IMPLEMENTED
query-tool hardening + dashboard sort arrows. The code is done in the working tree."""
import json, os, sys

sys.path.insert(0, "/opt/workspace/omni-deployer")
import shared

SCRIPT_DIR = "/opt/workspace/omni-deployer"
WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", "/opt/workspace")
OMNI_STACK_DIR = os.path.join(WORKSPACE_DIR, "omni-stack")

settings = shared.Settings(
    env_path=os.path.join(SCRIPT_DIR, "omnidev.env"),
    compose_file=os.path.join(OMNI_STACK_DIR, "docker-compose.yml"),
    dev_overlay=os.path.join(OMNI_STACK_DIR, "docker-compose.dev.yml"),
    project_name="omnidev",
    container="omnidev-omniagent-1",
    setup_channel="dev-channel",
    omni_stack_dir=OMNI_STACK_DIR,
    workspace_dir=WORKSPACE_DIR,
    script_dir=SCRIPT_DIR,
    use_api=False,
)
shared.init(settings)
s = shared.sett()

TASK = """TASK (FOLLOW-UP - the implementation is DONE, do NOT rewrite it): The previous turn implemented but hit the iteration limit before building/verifying/committing/pushing. The changes are ALREADY in the working trees:

1. /opt/workspace/omniagent - plugins/tools/query/src/main.rs (237 insertions): added WRITE_KEYWORDS token scan (blocks data-modifying CTEs), strip_sql_literals_and_comments, readonly_database_url() swapping the DSN username omniagent -> omniagent_readonly, BEGIN TRANSACTION READ ONLY + COMMIT/ROLLBACK around queries, and the configure callback now connects via the read-only role.

2. /opt/workspace/omni-dashboard - src/pages/database.ts + src/style.css (stacked up/down sort arrows with active direction highlighted).

YOUR JOB IS ONLY: build, verify, commit, push. Do NOT re-implement anything. Keep tool calls tight - you have a limited budget.

STEP 1 - Verify the omniagent plugin compiles and deploy it:
- cd /opt/workspace/omniagent && cargo fmt --check (fix with cargo fmt if needed) and cargo clippy --package mcp-server-query 2>&1 | tail -5 (fix real warnings if trivial)
- Rebuild ONLY the query plugin inside the dev container:
  docker exec omnidev-omniagent-1 bash -c 'cd /app && cargo build --release -p mcp-server-query'
  (plugin binaries resolve as siblings of current_exe() under /target/release/ - no cp needed)
- docker restart omnidev-omniagent-1
- Wait ~10s, then verify read-only enforcement via curl INSIDE the container:
  docker exec omnidev-omniagent-1 curl -s -X POST http://localhost:8080/mcp/execute -H 'Content-Type: application/json' -d '{"name":"query_database","arguments":{"operation":"query","sql":"SELECT current_user"}}'
  -> content must show "omniagent_readonly"
  Then test the 4 rejection/acceptance cases:
  a) WITH x AS (DELETE FROM messages RETURNING *) SELECT * FROM x  -> MUST be rejected
  b) INSERT INTO messages (role, content) VALUES ('user','x')       -> MUST fail
  c) DROP TABLE messages                                           -> MUST fail
  d) SELECT table_name FROM information_schema.tables LIMIT 3       -> MUST succeed

STEP 2 - Build + test the dashboard:
- Fix any root-owned files: sudo chown -R hermes:hermes /opt/workspace/omni-dashboard/dist /opt/workspace/omni-dashboard/node_modules/.vite /opt/workspace/omni-dashboard/node_modules/.vite-temp
- cd /opt/workspace/omni-dashboard && npm run build  (frontend + server, 0 errors)
- npm run test:unit -> all pass, 0 skipped (58 tests)
- npm run format:check clean; npm run lint 0 errors (pre-existing warnings ok)
- Restart dashboard: docker compose -f /opt/workspace/omni-stack/docker-compose.yml -f /opt/workspace/omni-stack/docker-compose.dev.yml --env-file /opt/workspace/omni-deployer/omnidev.env -p omnidev up -d dashboard
- Verify via docker exec omnidev-dashboard-1 node -e '...' (curl not installed; use fetch):
  - GET http://localhost:3001/api/db/tables -> 13 tables
  - POST /api/db/query {"table":"messages","page":1,"pageSize":25} -> columns + 25 rows + total
  - POST /api/db/query {"sql":"SELECT * FROM messages"} -> 25 rows (capped)
  - POST /api/db/query {"sql":"DELETE FROM messages"} -> error
  - POST /api/db/query {"table":"messages","page":1,"pageSize":5,"sortField":"id","sortDir":"desc"} -> sorted
  - Confirm the query tool reports current_user=omniagent_readonly through the dashboard path as well.

STEP 3 - Commit + push (origin/main ONLY, never stable):
- omniagent: git add plugins/tools/query/src/main.rs && git commit -m "fix: query tool enforces read-only at DB level via omniagent_readonly role + hardened validation" && git push origin main
- omni-dashboard: git add src/pages/database.ts src/style.css && git commit -m "feat: database page sort headers show stacked up/down arrows" && git push origin main
- Remove scratch files. Verify both pushes: git log origin/main --oneline -1 in each repo.

REPORT BACK: the exact outputs of the 5 PART-1 checks and 6 PART-2 checks, test counts, and BOTH commit hashes (verify with git log after pushing). Be concise."""

def main():
    print("=" * 50)
    print("  Dispatch FOLLOW-UP: build/verify/commit/push")
    print("=" * 50)

    testuser_token = shared._mm_login("testuser", s.mm_test_pass)
    if not testuser_token:
        raise RuntimeError("Could not login to Mattermost as testuser")
    print("  Logged in as testuser")

    team_id = shared._mm_get_team_id(testuser_token)
    mm_channel_id = shared._mm_find_channel_by_name(testuser_token, team_id, s.setup_channel)
    if not mm_channel_id:
        raise RuntimeError(f"Could not find channel '{s.setup_channel}'")
    print(f"  Channel: {s.setup_channel} (MM ID: {mm_channel_id})")

    omni_ch_id = None
    try:
        r = shared.oc("curl -sf http://localhost:8080/channels")
        if r.returncode == 0:
            channels = json.loads(r.stdout).get("data", [])
            ch = next((c for c in channels if c.get("platform") == "mattermost"
                       and c.get("name") == "mattermost-" + s.setup_channel), None)
            if ch:
                omni_ch_id = ch["id"]
    except Exception as e:
        raise RuntimeError(f"Could not find omniagent channel: {e}")
    if not omni_ch_id:
        raise RuntimeError("No omniagent mattermost channel")
    print(f"  Omniagent channel ID: {omni_ch_id}")

    latest_thread_id = 0
    try:
        r = shared.oc("curl -sf http://localhost:8080/threads")
        if r.returncode == 0:
            threads_data = json.loads(r.stdout)
            for t in threads_data.get("data", {}).get("threads", []):
                tid = t.get("id", 0) or 0
                if tid > latest_thread_id:
                    latest_thread_id = tid
    except Exception:
        pass
    print(f"  Latest thread ID before post: {latest_thread_id}")

    post_body = json.dumps({"channel_id": mm_channel_id, "message": TASK})
    r = shared._mm_post("/api/v4/posts", post_body, testuser_token)
    if r.returncode != 0:
        raise RuntimeError(f"Failed to post: {r.stderr[:300] if r.stderr else r.stdout[:300]}")
    print("  Task posted to dev-channel")

    print("  Tracking thread (timeout=2400s)...")
    thread = shared._wait_for_thread(omni_ch_id, timeout=2400, since_id=latest_thread_id)
    print(f"\n  Thread completed: id={thread.get('id')} status={thread.get('status')}")

    tid = thread.get("id", 0)
    if tid:
        try:
            r_detail = shared.oc("curl -sf http://localhost:8080/threads/" + str(tid))
            if r_detail.returncode == 0:
                detail = json.loads(r_detail.stdout)
                msgs = (detail.get("data", {}) or detail).get("messages", [])
                print(f"\n  Total messages in thread: {len(msgs)}")
                for m in msgs[-10:]:
                    role = m.get("role", "?")
                    content = str(m.get("content", "") or m.get("message", ""))[:900]
                    print(f"\n  --- [{role}] ---\n  {content}")
        except Exception as e:
            print(f"  (detail fetch failed: {e})")

    print("\n  ✅ DISPATCH + TRACKING COMPLETE")

if __name__ == "__main__":
    main()
