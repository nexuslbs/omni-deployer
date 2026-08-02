#!/usr/bin/env python3
"""Create the P1/P2 kanban task on the mm-kanban channel."""
import json, sys
sys.path.insert(0, "/opt/workspace/omni-deployer")
import shared

settings = shared.Settings(
    env_path="/opt/workspace/omni-deployer/omnidev.env",
    compose_file="/opt/workspace/omni-stack/docker-compose.yml",
    dev_overlay="/opt/workspace/omni-stack/docker-compose.dev.yml",
    project_name="omnidev",
    container="omnidev-omniagent-1",
    setup_channel="dev-channel",
    omni_stack_dir="/opt/workspace/omni-stack",
    workspace_dir="/opt/workspace",
    script_dir="/opt/workspace/omni-deployer",
    use_api=False,
)
shared.init(settings)

BODY = """Implement remaining items from the database-page audit (P1/P2). IMPORTANT RULES:
- Do NOT restart the omnidev-omniagent-1 container (you run inside it; a restart kills your own thread).
- Plugin/dashboard changes never need a restart. If an omniagent core/plugin change is required, just BUILD it (docker exec omnidev-omniagent-1 bash -c 'cd /app && cargo build --release -p <pkg>') and report; Hermes will restart + verify after you finish.

P1 — correctness + hardening:
1. Query plugin value serialization: plugins/tools/query/src/main.rs handle_query decodes rows via try_get <&str>/i64/f64/bool/Option<String> — timestamps (timestamptz), UUID, JSONB, arrays fall through to NULL. Fix: decode by column type (chrono -> RFC3339 string, UUID -> string, JSONB -> serde_json::Value, bytea -> hex, arrays -> JSON array) so SELECT id, created_at FROM messages shows real timestamps not NULL.
2. Row cap in the tool: cap results server-side (e.g. LIMIT 1000) regardless of caller LIMIT (defense in depth; dashboard already caps at 25).
3. Remove dead create_readonly_user migration (db-migrations/src/lib.rs ~line 701): the query tool connects with the main user; read-only is enforced by BEGIN TRANSACTION READ ONLY. The hardcoded-password role is dead weight — remove the function and its call.

P2 — polish:
4. Count via subquery wrapper, not regex: server/routes/db.ts stripLimitOffset regex can strip the wrong LIMIT when SQL contains LIMIT inside a subquery/literal. Replace with SELECT count(*) FROM (<base>) sub on the un-paginated SQL.
5. Concise pool-not-configured error: plugins/tools/query/src/main.rs dumps the whole input schema JSON with that error. Return a concise message (Query database pool not configured — check plugin config).
6. Executor/agent guard: an agent must never restart the container it runs inside (caused thread 488 self-kill). Add a guard or documented rule in the profile/executor template.
7. Dashboard unit test for error propagation: verify /api/db/query surfaces a tool is_error as a 4xx/5xx error message instead of empty results.

After each change: cargo fmt + cargo clippy for Rust (inside container), npm run build + npm run test:unit for dashboard (58 tests, 0 skipped). Commit + push to origin/main ONLY (never stable): omniagent + omni-dashboard. Report commit hashes. Do NOT restart the container."""

def main():
    print("Creating kanban task on mm-kanban channel...")
    payload = json.dumps({
        "name": "kanban_create-kanban-task",
        "arguments": {
            "title": "Database page + query tool follow-up (P1/P2)",
            "status": "ready",
            "priority": 3,
            "channel_id": 4,
            "body": BODY,
        },
    })
    r = shared.oc(f"docker exec omnidev-omniagent-1 curl -s -X POST http://localhost:8080/mcp/execute -H 'Content-Type: application/json' -d {repr(payload)}")
    print(r.stdout[:800])

    # Now list tasks to confirm
    r2 = shared.oc("docker exec omnidev-omniagent-1 curl -s -X POST http://localhost:8080/mcp/execute -H 'Content-Type: application/json' -d '{\"name\":\"kanban_list-kanban-tasks\",\"arguments\":{}}'")
    print(r2.stdout[:1500])

if __name__ == "__main__":
    main()
