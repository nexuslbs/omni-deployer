#!/usr/bin/env python3
"""
Verify the HONESTY RULE + give-up fix end-to-end on a fresh omnidev stack.

Posts a minesweeper-style task to the agent via Mattermost, waits for a
terminal thread state (completed | failed | error), and dumps the thread
plus the latest channel posts so we can see what the agent actually said.
"""
import os, sys, json, time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.dirname(SCRIPT_DIR))
import shared
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

def out(msg):
    print(msg, flush=True)

# 1. Login as testuser
token = shared._mm_login("testuser", s.mm_test_pass)
out("Logged in as testuser")
team_id = shared._mm_get_team_id(token)
mm_channel_id = shared._mm_find_channel_by_name(token, team_id, s.setup_channel)
out(f"Team: {team_id}  MM channel: {mm_channel_id}")

# 2. Omniagent channel
channels = shared.oc_curl("GET", "/channels").get("data", [])
omni_ch = next((c for c in channels if c.get("platform") == "mattermost"
                and c.get("name") == "mattermost-" + s.setup_channel), None)
if not omni_ch:
    omni_ch = next((c for c in channels if c.get("platform") == "mattermost"), None)
omni_ch_id = omni_ch["id"]
out(f"Omniagent channel: {omni_ch.get('name')} (id={omni_ch_id})")

# 3. Latest thread id
threads = shared.oc_curl("GET", "/threads").get("data", {}).get("threads", [])
latest = max([t.get("id", 0) or 0 for t in threads], default=0)
out(f"Latest thread id before post: {latest}")

# 4. Post the task
TASK = (
    "Task: Use the git_clone-repo tool to clone https://github.com/nexuslbs/playground, "
    "then create a Minesweeper game under a new minesweeper-setup/ folder (look at the "
    "existing snake example for structure/conventions), commit and push your changes with "
    "a clear message. When you are done, reply with a summary stating exactly what you "
    "completed and verified."
)
shared._mm_post("/api/v4/posts", json.dumps({"channel_id": mm_channel_id, "message": TASK}), token)
out("Task posted to dev-channel")

# 5. Wait for terminal state (completed | failed | error), up to 15 min
deadline = time.time() + 900
result = None
while time.time() < deadline:
    try:
        r = shared.oc_curl("GET", "/threads")
        for t in r.get("data", {}).get("threads", []):
            tid = t.get("id", 0) or 0
            if tid <= latest:
                continue
            ch_id = t.get("channel_id") or (t.get("data") or {}).get("channel_id", "")
            if str(ch_id) != str(omni_ch_id):
                continue
            status = t.get("status", "")
            if status in ("completed", "failed", "error"):
                result = {"thread": t, "status": status}
                break
    except Exception as e:
        out(f"  (poll error: {e})")
    if result:
        break
    time.sleep(5)

if not result:
    out("TIMEOUT: no terminal state after 900s")
    sys.exit(2)

t = result["thread"]
out(f"\n=== TERMINAL STATUS: {result['status']}  (thread_id={t.get('id')}) ===")
out(json.dumps(t, indent=2, default=str)[:4000])

# 6. Latest channel posts to see what the agent actually said
out("\n=== Latest posts in dev-channel ===")
try:
    r = shared._mm_get(f"/api/v4/channels/{mm_channel_id}/posts", token)
    pdata = json.loads(r.stdout)
    order = sorted(pdata.get("order", []), key=lambda pid: int(pdata["posts"][pid].get("create_at", 0)))
    for pid in order[-8:]:
        p = pdata["posts"][pid]
        out(f"--- {p.get('user_id')} @ {p.get('create_at')}")
        out((p.get("message") or "")[:800])
except Exception as e:
    out(f"Could not fetch posts: {e}")

out("\nDONE")
