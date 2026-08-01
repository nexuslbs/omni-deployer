#!/usr/bin/env python3
"""
real_test.py — end-to-end kanban implementation test for omni stacks.

Usage:
  python3 real_test.py dev       # omnidev: build from host repos (fast feedback)
  python3 real_test.py stable    # omnistable: GHCR images (static, image-fixed)

Flow:
  1. setup  — build/start/configure stack + Mattermost (via shared.setup)
  2. test   — shared tool tests (17 tools × 3 states via test-tool-caller)
  3. kanban — create a Mattermost channel (mm-kanban), post `$new` so the
     omniagent registers it, patch the channel to deepseek/deepseek-v4-flash
     + profile omni, write the omni profile allowed_tools config, and create
     a Kanban task in Todo status linked to that channel with the
     dev-development template and plan mode enabled.
"""

import argparse
import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import shared
from shared import BORD

WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", "/opt/workspace")
OMNI_STACK_DIR = os.path.join(WORKSPACE_DIR, "omni-stack")

# ── Kanban task content ─────────────────────────────────────────────────────
KANBAN_CHANNEL_NAME = "mm-kanban"
KANBAN_TEMPLATE = "dev-development"
PROFILE_NAME = "omni"

TASK_TITLE = "Implement IMDB-like movie database app in playground repo"

TASK_BODY = """Create a complete IMDB-like movie database web application in a NEW directory: /opt/workspace/playground/movie-db/

IMPORTANT: The playground contains OTHER unrelated projects (calc, chat, minesweeper, etc). Do NOT explore or read them. Create the new project from scratch.

Project structure to create:
- movie-db/docker-compose.yml (db + backend + frontend services)
- movie-db/backend/ (Go REST API)
- movie-db/frontend/ (React app)

Requirements:
1. DB service: PostgreSQL (docker-compose service 'db', database movie_db, user/pass via env)
2. Backend: Go (Golang) REST API with:
   - Dockerfile (multi-stage: golang:1.22-alpine build, alpine runtime)
   - go.mod (module movie-db/backend)
   - main.go with HTTP server on :8080
   - SQL schema + seed: create tables (users, movies) and insert 200 movie entries with title, year, genre, rating, description
   - Endpoints: POST /register (email+password, DEV mode auto-confirm, no email verification), POST /login, POST /reset-password, GET /movies (list with optional ?search=), GET /movies/{id}, GET / (homepage info)
3. Frontend: React (Vite) with pages: Home (movie list + search), Register, Reset Password. Use fetch to call the backend API (http://localhost:8080).
4. docker-compose: db (postgres:16), backend (build ./backend, ports 8080:8080, depends_on db), frontend (build ./frontend, ports 5173:5173, depends_on backend)

Workflow: use filesystem_write to create all files, then docker_compose(project_dir=/opt/workspace/playground/movie-db) build + up -d, verify with docker_compose ps and curl the backend health endpoint. Then commit and push with git_commit-and-push. Report the full structure and how to access it."""

# ── Omni profile allowed_tools (the callable gate for the worker agent) ─────
ALLOWED_TOOLS = [
    "cron_list-cron-jobs",
    "docker_compose",
    "fetch_fetch",
    "filesystem_read",
    "filesystem_write",
    "filesystem_list",
    "filesystem_search",
    "filesystem_info",
    "git_status",
    "git_clone-repo",
    "git_commit-and-push",
    "git_create-github-repo",
    "kanban_list-kanban-tasks",
    "metrics_get-metrics",
    "prompt_generate",
    "prompt_compact-messages",
    "search_messages",
    "search_wiki",
    "subtasks_list-subtasks",
    "actions_relevance-indexer",
    "plugin-manager_plugin-manager",
    "memory_list-memories",
    "memory_manage-memory",
    "memory_promote-to-memory",
    "memory_review-memories",
    "memory_generate-summary",
    "query_database",
    "skills_list-skills",
    "skills_create-skill",
]


def make_settings(mode):
    """Build shared.Settings for the requested mode."""
    if mode == "dev":
        return shared.Settings(
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
    elif mode == "stable":
        return shared.Settings(
            env_path=os.path.join(SCRIPT_DIR, "omnistable.env"),
            compose_file=os.path.join(OMNI_STACK_DIR, "docker-compose.yml"),
            dev_overlay=None,
            project_name="omnistable",
            container="omnistable-omniagent-1",
            setup_channel="stable-channel",
            omni_stack_dir=OMNI_STACK_DIR,
            workspace_dir=WORKSPACE_DIR,
            script_dir=SCRIPT_DIR,
            use_api=False,
        )
    raise ValueError("mode must be 'dev' or 'stable'")


def write_omni_profile():
    """Write profiles/omni/config.json with the desired allowed_tools.

    The omni-stack dir is bind-mounted into the container at /opt/omni, so this
    file is live-read by the agent (no restart needed for allowed_tools).
    """
    profile_dir = os.path.join(OMNI_STACK_DIR, "profiles", "omni")
    os.makedirs(profile_dir, exist_ok=True)
    config_path = os.path.join(profile_dir, "config.json")
    config = {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "allowed_tools": ALLOWED_TOOLS,
    }
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    print(f"  Profile config written: {config_path} ({len(ALLOWED_TOOLS)} tools)")
    return config_path


def mm_create_channel_and_new():
    """Create the mm-kanban Mattermost channel, add members, post $new.

    Returns the omniagent channel id + the Mattermost channel id.
    """
    s = shared.sett()
    print(f"\n{'=' * 50}")
    print(f"  Kanban channel setup (Mattermost + omniagent)")
    print(f"{'=' * 50}")

    # Login as admin (needed to create channels / add members)
    admin_token = shared._mm_login("lucasbasquerotto", s.mm_admin_pass)
    print("  Logged in as admin")

    team_id = shared._mm_get_team_id(admin_token)
    if not team_id:
        raise RuntimeError("Could not find Mattermost team")
    print(f"  Team ID: {team_id}")

    # Get user ids
    r = shared._mm_get(f"/api/v4/users?per_page=200", admin_token)
    if r.returncode != 0:
        raise RuntimeError("Could not list users")
    users = json.loads(r.stdout)
    user_ids = {u["username"]: u["id"] for u in users}
    bot_id = user_ids.get("omnibot")
    test_id = user_ids.get("testuser")
    admin_id = user_ids.get("lucasbasquerotto")
    if not (bot_id and test_id and admin_id):
        raise RuntimeError(f"Missing users: bot={bot_id} test={test_id} admin={admin_id}")
    print(f"  Users: bot={bot_id} test={test_id} admin={admin_id}")

    # Find or create the channel
    r = shared._mm_get(f"/api/v4/teams/{team_id}/channels", admin_token)
    channels = json.loads(r.stdout) if r.returncode == 0 else []
    ch = next((c for c in channels if c.get("name") == KANBAN_CHANNEL_NAME), None)
    if ch:
        mm_channel_id = ch["id"]
        print(f"  Channel {KANBAN_CHANNEL_NAME} exists: {mm_channel_id}")
    else:
        body = json.dumps({
            "team_id": team_id,
            "name": KANBAN_CHANNEL_NAME,
            "display_name": KANBAN_CHANNEL_NAME,
            "type": "O",
            "purpose": "Kanban board channel",
        })
        r = shared._mm_post("/api/v4/channels", body, admin_token)
        if r.returncode != 0:
            raise RuntimeError(f"Could not create channel: {r.stderr[:200]}")
        mm_channel_id = json.loads(r.stdout)["id"]
        print(f"  Channel {KANBAN_CHANNEL_NAME} created: {mm_channel_id}")

    # Add members (idempotent-ish: 201 created, 400 if already member)
    for uid, label in [(bot_id, "bot"), (test_id, "testuser"), (admin_id, "admin")]:
        body = json.dumps({"user_id": uid})
        shared._mm_post(f"/api/v4/channels/{mm_channel_id}/members", body, admin_token)
    print("  Members added: bot, testuser, admin")

    # Post `$new` as testuser (non-bot so the plugin receives the WS event)
    test_token = shared._mm_login("testuser", s.mm_test_pass)
    body = json.dumps({"channel_id": mm_channel_id, "message": "$new"})
    r = shared._mm_post("/api/v4/posts", body, test_token)
    if r.returncode != 0:
        raise RuntimeError(f"Could not post $new: {r.stderr[:200]}")
    print("  Posted $new as testuser")

    # Wait for omniagent to register the channel
    omni_ch = None
    for _ in range(20):
        try:
            channels = shared.oc_curl("GET", "/channels").get("data", [])
            omni_ch = next(
                (c for c in channels if c.get("platform") == "mattermost"
                 and c.get("resource_identifier") == mm_channel_id),
                None,
            )
            if omni_ch:
                break
        except Exception:
            pass
        time.sleep(3)
    if not omni_ch:
        raise RuntimeError("omniagent did not register the mm-kanban channel after $new")
    print(f"  omniagent channel registered: id={omni_ch['id']} name={omni_ch['name']}")
    return omni_ch["id"], mm_channel_id


def patch_channel_config(omni_channel_id):
    """Set provider/model/profile on the omniagent channel."""
    s = shared.sett()
    print("\n[Patching kanban channel config...]")
    shared.oc_curl("PATCH", f"/channels/{omni_channel_id}", {
        "current_provider": "deepseek",
        "current_model": "deepseek-v4-flash",
        "current_profile": PROFILE_NAME,
    })
    print(f"  Channel {omni_channel_id} -> deepseek/deepseek-v4-flash, profile={PROFILE_NAME}")


def create_kanban_task(omni_channel_id):
    """Create the IMDB kanban task in Todo status."""
    print("\n[Creating kanban task...]")
    body = {
        "title": TASK_TITLE,
        "body": TASK_BODY,
        "status": "todo",
        "channel_id": omni_channel_id,
        "profile": PROFILE_NAME,
        "template": KANBAN_TEMPLATE,
        "plan": True,
        "priority": 1,
    }
    # Use the task-body file approach to avoid NUL/shell mangling
    payload_path = "/tmp/_real_test_task.json"
    with open(payload_path, "w") as f:
        json.dump(body, f)
    shared.sh("docker cp " + payload_path + " " + shared.sett().container + ":/tmp/_real_test_task.json")
    r = shared.oc("curl -sf -X POST http://localhost:8080/kanban/tasks "
                  "-H 'Content-Type: application/json' -d @/tmp/_real_test_task.json")
    if r.returncode != 0:
        raise RuntimeError(f"Could not create kanban task: {r.stderr[:300]}")
    resp = json.loads(r.stdout)
    task_id = resp.get("data", {}).get("id")
    print(f"  Kanban task created: {task_id} (status=todo)")
    return task_id


def run(mode):
    print(f"{BORD}")
    print(f"  REAL TEST — mode={mode}")
    print(f"{BORD}")

    settings = make_settings(mode)
    shared.init(settings)

    # 1. setup
    shared.setup()

    # 2. test (shared tool tests: 17 tools × 3 states)
    shared._check_container()
    shared.run_tests()

    # 3. omni profile allowed_tools
    write_omni_profile()

    # 4. kanban channel + $new
    omni_channel_id, mm_channel_id = mm_create_channel_and_new()

    # 5. patch channel config
    patch_channel_config(omni_channel_id)

    # 6. create kanban task in Todo
    task_id = create_kanban_task(omni_channel_id)

    print(f"\n{BORD}")
    print(f"  ✅ REAL TEST SETUP COMPLETE (mode={mode})")
    print(f"  omniagent channel id : {omni_channel_id}")
    print(f"  Mattermost channel id: {mm_channel_id}")
    print(f"  Kanban task id       : {task_id}")
    print(f"  Task status          : todo (dispatch with kanban_dispatcher)")
    print(f"{BORD}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="End-to-end kanban implementation test")
    parser.add_argument("mode", choices=["dev", "stable"], help="dev (omnidev, host repos) or stable (omnistable, GHCR images)")
    args = parser.parse_args()
    try:
        sys.exit(run(args.mode))
    except Exception as e:
        print(f"\n❌ REAL TEST FAILED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
