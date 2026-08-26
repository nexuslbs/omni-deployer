#!/usr/bin/env python3
"""Dispatch CORRECTED query-tool read-only design to the omnidev agent.

Key fixes vs prior dispatch:
- NO omniagent_readonly role / username swap. Connect with the MAIN user via
  the plugin's `database_url` config field (default `$env:DATABASE_URL`,
  resolved by the framework before the configure message reaches the plugin -
  plugin code must NOT read env vars directly).
- Read-only is enforced by PostgreSQL itself: BEGIN TRANSACTION READ ONLY
  around every query (already implemented) + keyword scan + SELECT/WITH start
  check as defense in depth.
- The agent must NOT restart omnidev-omniagent-1 (it runs inside it; restart
  kills its own thread). The dispatcher restarts + verifies AFTER the thread
  completes.
- Dashboard: fix runQueryTool to surface is_error instead of silent [].
"""
import json, os, sys, time

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

TASK = """TASK: Fix the query tool read-only enforcement design. The current implementation is BROKEN end-to-end:
the pool never configures because it swaps the DSN username to `omniagent_readonly` (whose password differs
from the omniagent user's), so every query_database call returns "Query database pool not configured" and the
dashboard silently shows empty tables.

THE CORRECT DESIGN (do NOT create/alter any DB role, do NOT swap usernames):
1. Connect with the MAIN database user via the plugin's `database_url` config field.
   - plugin.json already declares config_schema key "database_url" with default "$env:DATABASE_URL".
   - The omniagent framework resolves $env: refs in config defaults BEFORE sending the configure message
     (see src/mcp/external/config.rs ~line 595-614: resolve_config_value inserts the resolved value into
     the config env). So the plugin receives the real URL in `config.database_url` when the env var is set.
   - In plugins/tools/query/src/main.rs, DELETE the `readonly_database_url()` function entirely and make the
     configure callback connect with the plain resolved URL. The plugin must NOT read env vars directly:
       let url = config.database_url.clone();  // framework already resolved $env:DATABASE_URL default
     If config.database_url is empty, log a clear error (do NOT fall back to std::env::var in the plugin).
2. Read-only enforcement stays PostgreSQL-native (already implemented - KEEP it):
   - handle_query wraps every query in BEGIN TRANSACTION READ ONLY ... COMMIT/ROLLBACK (main.rs ~408-455).
     PostgreSQL refuses ANY write inside a read-only transaction (SQLSTATE 25006) regardless of role grants.
   - Keep the app-level checks as defense in depth: statement must start with SELECT/WITH, WRITE_KEYWORDS
     token scan after stripping literals/comments (blocks data-modifying CTEs), and note that
     `AssertSqlSafe` is a sqlx MARKER type, NOT a semicolon validator - multi-statement is blocked by the
     extended query protocol. FIX the misleading comment at ~line 387 accordingly.
3. Dashboard (server/routes/db.ts): runQueryTool currently only checks body.success and silently returns []
   when body.content is non-JSON (e.g. a tool error string). FIX: if body.is_error is true, throw
   ApiError(502, error message) so failures surface instead of empty tables.
4. Do NOT touch db-migrations/src/lib.rs create_readonly_user - it becomes dead weight but is harmless.
   Do NOT restart the omnidev-omniagent-1 container (you run inside it - restart kills your own thread;
   the dispatcher restarts after you finish).

YOUR JOB:
STEP 1 - omniagent plugin:
- cd /opt/workspace/omniagent && cargo fmt --check (fix with cargo fmt if needed)
- cargo clippy --package mcp-server-query 2>&1 | tail -5 (fix real warnings if trivial)
- Build the plugin INSIDE the dev container (compiles the new binary; NO restart):
  docker exec omnidev-omniagent-1 bash -c 'cd /app && cargo build --release -p mcp-server-query'
- Ensure it compiles cleanly (this is the verification that the code is correct).

STEP 2 - dashboard:
- Fix runQueryTool is_error handling in /opt/workspace/omni-dashboard/server/routes/db.ts
- cd /opt/workspace/omni-dashboard && npm run build (0 errors) and npm run test:unit (all pass, 0 skipped)

STEP 3 - Commit + push (origin/main ONLY, never stable):
- omniagent: git add plugins/tools/query/src/main.rs && git commit -m "fix: query tool connects with main DB user; read-only enforced by READ ONLY transactions" && git push origin main
- omni-dashboard: git add server/routes/db.ts && git commit -m "fix: database API surfaces query tool errors (is_error) instead of empty results" && git push origin main
- Remove scratch files. Verify both pushes: git log origin/main --oneline -1 in each repo.

REPORT BACK concisely: the compile result, the exact diff summary of what you changed in both repos, test counts,
and BOTH commit hashes (verify with git log after pushing). Do NOT restart the container. Do NOT create roles."""


def main():
    print("=" * 50)
    print("  Dispatch CORRECTED query read-only design")
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

    print("  Tracking thread (timeout=3000s)...")
    thread = shared._wait_for_thread(omni_ch_id, timeout=3000, since_id=latest_thread_id)
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
                    content = str(m.get("content", "") or m.get("message", ""))[:1200]
                    print(f"\n  --- [{role}] ---\n  {content}")
        except Exception as e:
            print(f"  (detail fetch failed: {e})")

    # ── Phase B: dispatcher restarts container (agent is done; safe now) ──
    print("\n" + "=" * 50)
    print("  PHASE B: restarting omnidev-omniagent-1 to load new plugin binary")
    print("=" * 50)
    rr = shared.oc("docker restart omnidev-omniagent-1")
    print(f"  restart rc={rr.returncode}")
    time.sleep(15)

    # Verify new binary is loaded + read-only enforcement works
    checks = [
        ("current_user is main user (omniagent, NOT omniagent_readonly)",
         '{"name":"query_database","arguments":{"operation":"query","sql":"SELECT current_user"}}',
         "omniagent", "omniagent_readonly"),
        ("INSERT rejected (read-only txn)",
         '{"name":"query_database","arguments":{"operation":"query","sql":"INSERT INTO messages (role, content) VALUES (\'user\', \'x\')"}}',
         "is_error", None),
        ("DROP TABLE rejected",
         '{"name":"query_database","arguments":{"operation":"query","sql":"DROP TABLE messages"}}',
         "is_error", None),
        ("data-modifying CTE rejected",
         '{"name":"query_database","arguments":{"operation":"query","sql":"WITH x AS (DELETE FROM messages RETURNING *) SELECT * FROM x"}}',
         "is_error", None),
        ("plain SELECT works",
         '{"name":"query_database","arguments":{"operation":"query","sql":"SELECT table_name FROM information_schema.tables WHERE table_schema=\'public\' LIMIT 3"}}',
         "table_name", None),
    ]
    for label, payload, expect, forbid in checks:
        try:
            r = shared.oc(
                "docker exec omnidev-omniagent-1 curl -s -X POST http://localhost:8080/mcp/execute "
                "-H 'Content-Type: application/json' -d " + repr(payload)
            )
            out = r.stdout
            ok = expect in out and (forbid is None or forbid not in out)
            print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
            print(f"        -> {out[:200]}")
        except Exception as e:
            print(f"  [FAIL] {label}: {e}")

    # Dashboard API checks (proxies through the query tool)
    print("\n  --- Dashboard API checks ---")
    dash_checks = [
        ("GET /api/db/tables", "GET", None, "tables"),
        ("POST /api/db/query DELETE (must error)",
         "POST", '{"sql":"DELETE FROM messages"}', "error"),
        ("POST /api/db/query SELECT (must work)",
         "POST", '{"sql":"SELECT id, role FROM messages LIMIT 2"}', "role"),
    ]
    for label, method, body, expect in dash_checks:
        try:
            base = "http://localhost:3001/api/db"
            if method == "GET":
                r = shared.oc(f"curl -s -o /dev/null -w '%{{http_code}}' {base}/tables")
                # fetch tables content separately
                r2 = shared.oc(f"curl -s {base}/tables")
                ok = "tables" in r2.stdout and expect in r2.stdout
                print(f"  [{'PASS' if ok else 'FAIL'}] {label} (http={r.stdout}) -> {r2.stdout[:120]}")
            else:
                r = shared.oc(
                    f"curl -s -X POST {base}/query -H 'Content-Type: application/json' -d {repr(body)}"
                )
                ok = expect in r.stdout.lower()
                print(f"  [{'PASS' if ok else 'FAIL'}] {label} -> {r.stdout[:150]}")
        except Exception as e:
            print(f"  [FAIL] {label}: {e}")

    # Verify pushes
    print("\n  --- Push verification ---")
    for repo in ["/opt/workspace/omniagent", "/opt/workspace/omni-dashboard"]:
        r = shared.oc(f"cd {repo} && git log origin/main --oneline -2")
        print(f"  {repo}:\n{r.stdout}")
        r2 = shared.oc(f"cd {repo} && git status -sb | head -3")
        print(f"  {r2.stdout}")

    print("\n  ✅ DISPATCH + TRACKING + VERIFICATION COMPLETE")


if __name__ == "__main__":
    main()
