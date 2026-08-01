#!/usr/bin/env python3
"""Post a complex task + a trivial question, verify the plugin's runtime plan decision."""
import os, sys, json, time

sys.path.insert(0, "/opt/workspace/omni-deployer/scripts")
sys.path.insert(0, "/opt/workspace/omni-deployer")
import shared

SCRIPT_DIR = "/opt/workspace/omni-deployer"
settings = shared.Settings(
    env_path=os.path.join(SCRIPT_DIR, "omnidev.env"),
    compose_file="/opt/workspace/omni-stack/docker-compose.yml",
    dev_overlay="/opt/workspace/omni-stack/docker-compose.dev.yml",
    project_name="omnidev",
    container="omnidev-omniagent-1",
    setup_channel="dev-channel",
    use_api=False,
)
shared.init(settings)
s = shared.sett()

def out(msg):
    print(msg, flush=True)

token = shared._mm_login("testuser", s.mm_test_pass)
team_id = shared._mm_get_team_id(token)
mm_channel_id = shared._mm_find_channel_by_name(token, team_id, s.setup_channel)

threads = shared.oc_curl("GET", "/threads").get("data", {}).get("threads", [])
latest = max([t.get("id", 0) or 0 for t in threads], default=0)
out(f"latest thread id: {latest}")

COMPLEX = "Task: Use the git_clone-repo tool to clone https://github.com/nexuslbs/playground, then create a Minesweeper game under a new minesweeper-setup/ folder (look at the existing snake example for structure/conventions), commit and push your changes with a clear message."
TRIVIAL = "What is 15 * 37 + 42?"

shared._mm_post("/api/v4/posts", json.dumps({"channel_id": mm_channel_id, "message": COMPLEX}), token)
out("posted complex task")
time.sleep(5)
shared._mm_post("/api/v4/posts", json.dumps({"channel_id": mm_channel_id, "message": TRIVIAL}), token)
out("posted trivial question")

# Wait for both threads to start processing (context_builder persists the plan decision)
deadline = time.time() + 60
seen = {}
while time.time() < deadline:
    r = shared.oc_curl("GET", "/threads")
    for t in r.get("data", {}).get("threads", []):
        tid = t.get("id", 0) or 0
        if tid <= latest:
            continue
        ch_id = t.get("channel_id") or (t.get("data") or {}).get("channel_id", "")
        if str(ch_id) != "3":
            continue
        status = t.get("status", "")
        if tid not in seen:
            seen[tid] = t
        if status in ("processing", "completed", "failed", "skipped"):
            if t.get("iterations", 0) > 0 or t.get("msg_count", 0) > 3:
                seen[tid] = t
    if len(seen) >= 2:
        break
    time.sleep(3)

out("")
for tid in sorted(seen):
    t = seen[tid]
    cause = (t.get("cause_content_preview") or "")[:60].replace("\n", " ")
    out(f"thread {tid}: status={t.get('status')} plan={t.get('plan')} msgs={t.get('msg_count')} | {cause}")
out("DONE")
