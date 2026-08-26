#!/usr/bin/env python3
"""Dispatch (attempt 2): query read-only fix with EXACT inline code.

Previous attempt failed: the agent burned its 120-turn budget on context
compaction (94 prompt_compact calls) before editing anything. This dispatch
gives exact before/after code so the agent applies patches without reading
large files. Keep tool calls minimal.
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

TASK = """TASK: Apply these EXACT code changes. Do NOT read any files first - the exact edits are below. Do NOT restart the container. Do NOT create roles. Do NOT explore. Apply, build, commit, push.

═══ EDIT 1: /opt/workspace/omniagent/plugins/tools/query/src/main.rs ═══
Use python to apply these string replacements (exact match):

1) DELETE the entire readonly_database_url() function. Replace this exact text:
"""
TASK += """/// Rewrites a PostgreSQL connection URL so it authenticates as the dedicated
/// `omniagent_readonly` role instead of the full-privilege `omniagent` user.
/// The password comes from the existing DATABASE_URL / plugin config - only the
/// username is substituted, so no credentials are hardcoded in source code.
fn readonly_database_url(url: &str) -> String {
    const FULL: &str = "://omniagent:";
    const READONLY: &str = "://omniagent_readonly:";
    if url.contains(FULL) {
        url.replace(FULL, READONLY)
    } else {
        url.to_string()
    }
}
"""
TASK += """with an empty string.

2) In the configure callback (run_server_with_config), replace this exact text:
"""
TASK += """            let config = PluginConfig::from_json(&params);
            // Always connect through the dedicated read-only role: resolve the
            // DSN (plugin config, or DATABASE_URL env when unset) and swap the
            // full-privilege `omniagent` username for `omniagent_readonly`.
            let url = if config.database_url.is_empty() {
                std::env::var("DATABASE_URL").unwrap_or_default()
            } else {
                config.database_url.clone()
            };
            let readonly_url = readonly_database_url(&url);
            tokio::task::block_in_place(|| {
                let rt = tokio::runtime::Handle::current();
                let new_pool = rt.block_on(db::connect(&readonly_url));
"""
TASK += """with this exact text:
"""
TASK += """            let config = PluginConfig::from_json(&params);
            // Connect with the MAIN database user via the plugin's database_url
            // config field. The framework resolves the "$env:DATABASE_URL"
            // default before sending the configure message, so the plugin never
            // reads env vars directly. Read-only is enforced per-query by
            // BEGIN TRANSACTION READ ONLY in handle_query (PostgreSQL refuses
            // any write inside a read-only transaction, SQLSTATE 25006).
            let url = config.database_url.clone();
            tokio::task::block_in_place(|| {
                let rt = tokio::runtime::Handle::current();
                let new_pool = rt.block_on(db::connect(&url));
"""
TASK += """
3) Fix a misleading comment. Replace this exact text:
"""
TASK += """    // 3) Semicolons are still rejected by AssertSqlSafe at execution time.
"""
TASK += """with:
"""
TASK += """    // 3) `AssertSqlSafe` is a sqlx MARKER type, not a semicolon validator;
    //    multi-statement SQL is rejected by the extended query protocol.
"""
TASK += """
4) If the file no longer compiles due to unused imports (e.g. no more env usage), fix them minimally.

Then run:
- cd /opt/workspace/omniagent && cargo fmt && cargo clippy --package mcp-server-query 2>&1 | tail -5
- docker exec omnidev-omniagent-1 bash -c 'cd /app && cargo build --release -p mcp-server-query'
  (this compiles the new binary; do NOT restart)

═══ EDIT 2: /opt/workspace/omni-dashboard/server/routes/db.ts ═══
1) Replace this exact text:
"""
TASK += """interface McpExecuteResult {
  success?: boolean;
  content?: unknown;
  error?: string;
}
"""
TASK += """with:
"""
TASK += """interface McpExecuteResult {
  success?: boolean;
  is_error?: boolean;
  content?: unknown;
  error?: string;
}
"""
TASK += """
2) Replace this exact text:
"""
TASK += """  if (body.success !== true) {
    throw new ApiError(502, body.error || "Query tool failed");
  }
"""
TASK += """with:
"""
TASK += """  if (body.success !== true || body.is_error === true) {
    throw new ApiError(502, body.error || (typeof body.content === "string" ? body.content : "Query tool failed"));
  }
"""
TASK += """
Then run:
- cd /opt/workspace/omni-dashboard && npm run build && npm run test:unit

═══ COMMIT + PUSH (origin/main ONLY, never stable) ═══
- omniagent: git add plugins/tools/query/src/main.rs && git commit -m "fix: query tool connects with main DB user; read-only enforced by READ ONLY transactions" && git push origin main
- omni-dashboard: git add server/routes/db.ts && git commit -m "fix: database API surfaces query tool errors (is_error) instead of empty results" && git push origin main
- Verify: git log origin/main --oneline -1 in each repo.

REPORT BACK concisely: the 3 edit results, compile output tail, test counts, and BOTH commit hashes. Be brief."""


def main():
    print("=" * 50)
    print("  Dispatch attempt 2: exact-code query read-only fix")
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

    # Poll for completion OR terminal failure (interrupted/error/skipped)
    poll_start = time.time()
    timeout = 1800
    thread = None
    while time.time() - poll_start < timeout:
        r = shared.oc("curl -sf http://localhost:8080/threads")
        if r.returncode == 0:
            try:
                threads_data = json.loads(r.stdout)
                for t in threads_data.get("data", {}).get("threads", []):
                    tid = t.get("id", 0) or 0
                    if tid <= latest_thread_id:
                        continue
                    status = t.get("status", "")
                    if status in ("completed", "error", "interrupted", "skipped", "failed"):
                        thread = t
                        break
            except (json.JSONDecodeError, KeyError):
                pass
        if thread:
            break
        time.sleep(3)

    if not thread:
        raise RuntimeError(f"No thread terminal state after {timeout}s")
    print(f"\n  Thread {thread.get('id')} status={thread.get('status')}")
    print(f"  last_message: {str(thread.get('last_message'))[:1500]}")

    # ── Phase B: only restart + verify if the agent completed the work ──
    status = thread.get("status")
    # Verify actual artifacts regardless of thread status
    print("\n  --- Artifact verification (source-level) ---")
    rr = shared.oc("grep -c 'readonly_database_url' /opt/workspace/omniagent/plugins/tools/query/src/main.rs")
    print(f"  readonly_database_url refs in main.rs (expect 0): {rr.stdout.strip()}")
    rr = shared.oc("grep -n 'is_error' /opt/workspace/omni-dashboard/server/routes/db.ts | head -3")
    print(f"  db.ts is_error handling:\n{rr.stdout[:300]}")
    for repo in ["/opt/workspace/omniagent", "/opt/workspace/omni-dashboard"]:
        rr = shared.oc(f"cd {repo} && git log origin/main --oneline -2")
        print(f"  {repo}:\n{rr.stdout}")
        rr = shared.oc(f"cd {repo} && git status -sb | head -3")
        print(f"  {rr.stdout}")

    if status == "completed":
        print("\n  --- Restarting container to load new binary ---")
        rr = shared.oc("docker restart omnidev-omniagent-1")
        print(f"  restart rc={rr.returncode}")
        time.sleep(15)
        checks = [
            ("current_user is main user",
             '{"name":"query_database","arguments":{"operation":"query","sql":"SELECT current_user"}}',
             "omniagent", "omniagent_readonly"),
            ("INSERT rejected",
             '{"name":"query_database","arguments":{"operation":"query","sql":"INSERT INTO messages (role, content) VALUES (\'user\', \'x\')"}}',
             "is_error", None),
            ("DROP rejected",
             '{"name":"query_database","arguments":{"operation":"query","sql":"DROP TABLE messages"}}',
             "is_error", None),
            ("CTE write rejected",
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

        print("\n  --- Dashboard API checks ---")
        for label, method, body, expect in [
            ("GET /api/db/tables", "GET", None, "tables"),
            ("POST DELETE (must error)", "POST", '{"sql":"DELETE FROM messages"}', "error"),
            ("POST SELECT (must work)", "POST", '{"sql":"SELECT id, role FROM messages LIMIT 2"}', "role"),
        ]:
            try:
                base = "http://localhost:3001/api/db"
                if method == "GET":
                    r2 = shared.oc(f"curl -s {base}/tables")
                    ok = expect in r2.stdout
                    print(f"  [{'PASS' if ok else 'FAIL'}] {label} -> {r2.stdout[:120]}")
                else:
                    r = shared.oc(f"curl -s -X POST {base}/query -H 'Content-Type: application/json' -d {repr(body)}")
                    ok = expect in r.stdout.lower()
                    print(f"  [{'PASS' if ok else 'FAIL'}] {label} -> {r.stdout[:150]}")
            except Exception as e:
                print(f"  [FAIL] {label}: {e}")
    else:
        print(f"\n  Thread did NOT complete (status={status}); container NOT restarted; no runtime verification.")

    print("\n  ✅ DISPATCH 2 COMPLETE")


if __name__ == "__main__":
    main()
