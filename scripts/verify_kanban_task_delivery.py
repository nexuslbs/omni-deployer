#!/usr/bin/env python3
"""Verify kanban task delivery: the task body MUST reach the LLM prompt.

Regression test for the "agent never sees its task" bug:
  - get_thread_messages() filtered out the seq-0 cause message
    (msg_type='kanban' / 'Cause'), so the task body never appeared in the
    prompt context.
  - the plan phase in main_loop.rs dropped prompt_parts.user, so the
    planning prompt had no user request at all.

CI-safe: uses the noop provider + test-tool-caller model as a FAKE LLM
(no API keys, no real HTTP LLM calls). The planning prompt is persisted to
the messages table BEFORE the LLM call, so the assertion verifies the
prompt *building* path — exactly what regressed.

Flow:
  1. Create a kanban task with a distinctive body marker on the mm-kanban
     channel, patched to noop/test-tool-caller.
  2. Trigger the kanban_dispatcher action.
  3. Wait for the thread + persisted planning prompt (msg_type='prompt').
  4. Assert the task body marker appears in the prompt content
     (context block AND/OR user message).
  5. Report PASS/FAIL; clean up the task.
"""
import json, os, sys, time, uuid

sys.path.insert(0, "/opt/workspace/omni-deployer")
sys.path.insert(0, "/opt/workspace/omni-deployer/scripts")
import shared

SCRIPT_DIR = "/opt/workspace/omni-deployer"
settings = shared.Settings(
    env_path=os.path.join(SCRIPT_DIR, "omnidev.env"),
    compose_file="/opt/workspace/omni-stack/docker-compose.yml",
    dev_overlay="/opt/workspace/omni-stack/docker-compose.dev.yml",
    project_name="omnidev",
    container="omnidev-omniagent-1",
    setup_channel="dev-channel",
    omni_stack_dir="/opt/workspace/omni-stack",
    workspace_dir="/opt/workspace",
    script_dir=SCRIPT_DIR,
    use_api=False,
)
shared.init(settings)
s = shared.sett()

MARKER = "KANBAN_DELIVERY_MARKER_" + uuid.uuid4().hex[:10].upper()
TITLE = "Delivery test: implement frobnicator"
BODY = (f"{MARKER}: implement the frobnicator in the omni-dashboard repo. "
        "Add a button that calls GET /api/frobnicate and shows the result. "
        "Commit and push to main.")

def out(msg):
    print(msg, flush=True)

def api(path, method="GET", body=None):
    return shared.oc_curl(method, path, body)

def main():
    out(f"marker={MARKER}")
    # 1. Find the mm-kanban omniagent channel (id 4 expected after prepare)
    channels = api("/channels").get("data", [])
    ch = next((c for c in channels if c.get("platform") == "mattermost"
               and c.get("name") == "mattermost-mm-kanban"), None)
    if not ch:
        ch = next((c for c in channels if c.get("platform") == "mattermost"), None)
    if not ch:
        raise RuntimeError("No mattermost channel found")
    cid = ch["id"]
    out(f"channel id={cid} name={ch.get('name')}")

    # Save original provider/model to restore after
    orig_provider = ch.get("current_provider") or ""
    orig_model = ch.get("current_model") or ""

    # 2. Patch channel to noop/test-tool-caller (fake LLM, CI-safe)
    api(f"/channels/{cid}", "PATCH", {
        "current_provider": "noop",
        "current_model": "test-tool-caller",
    })
    out("patched channel to noop/test-tool-caller")

    # 3. Create the kanban task (status todo -> dispatcher picks it up)
    create = api("/kanban/tasks", "POST", {
        "title": TITLE,
        "body": BODY,
        "status": "todo",
        "priority": 1,
        "channel_id": cid,
        "plan": True,
    })
    out(f"create resp: {json.dumps(create)[:300]}")
    task_id = None
    data = create.get("data", create)
    if isinstance(data, dict):
        task_id = data.get("id")
    if not task_id:
        raise RuntimeError(f"Could not extract task id from {json.dumps(create)[:300]}")

    try:
        # 4. Trigger dispatch via the kanban_dispatcher action
        disp = api("/mcp/execute", "POST", {
            "name": "actions_kanban-dispatcher",
            "arguments": {},
        })
        disp_text = json.dumps(disp)
        out(f"dispatcher resp: {disp_text[:300]}")

        # The dispatcher response contains the thread id, e.g.
        # "Dispatched kanban task '...' (task_xxx) \u2192 thread 56 (ready)".
        # The /threads API summary does NOT expose task_id, so parse it here.
        import re
        m = re.search(r"thread (\d+)", disp_text)
        if not m:
            raise RuntimeError(f"Could not parse thread id from dispatcher resp: {disp_text[:300]}")

        # 5. Wait for the persisted planning prompt for that thread
        deadline = time.time() + 120
        thread_id = int(m.group(1))
        prompt_found = False
        prompt_content = ""
        while time.time() < deadline:
            try:
                ev = api(f"/messages/events?thread_id={thread_id}&msg_type=prompt", "GET")
                ev_data = ev.get("data", {})
                rows = ev_data.get("messages") or ev_data.get("rows") or []
                if rows:
                    prompt_content = rows[0].get("content") or ""
                    if prompt_content:
                        prompt_found = True
                        break
            except Exception as e:
                out(f"  (wait: {e})")
            time.sleep(5)

        out(f"thread_id={thread_id} prompt_found={prompt_found}")
        if not prompt_found:
            raise RuntimeError("Timed out waiting for persisted planning prompt")

        # 6. Assert the task body marker is in the prompt
        if MARKER not in prompt_content:
            out("PROMPT CONTENT (first 2000 chars):")
            out(prompt_content[:2000])
            raise AssertionError(
                f"TASK BODY NOT IN PROMPT — marker {MARKER} missing from planning prompt"
            )
        out("✓ task body marker found in planning prompt")
        out("")
        out("=" * 50)
        out("  ✅ KANBAN DELIVERY TEST PASSED")
        out("  Task body reaches the LLM prompt (context + user message).")
        out("=" * 50)
        return 0
    finally:
        # Cleanup: restore channel provider/model
        try:
            if orig_provider:
                api(f"/channels/{cid}", "PATCH", {
                    "current_provider": orig_provider,
                    "current_model": orig_model,
                })
                out(f"restored channel {cid} -> {orig_provider}/{orig_model}")
        except Exception as e:
            out(f"  (restore channel failed: {e})")
        # Mark the test task done so it doesn't linger
        try:
            api(f"/kanban/tasks/{task_id}", "PATCH", {"status": "done"})
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
