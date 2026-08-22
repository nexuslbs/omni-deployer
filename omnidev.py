#!/usr/bin/env python3
"""
OmniStack dev launcher — thin wrapper around shared.py.

Usage:
  python3 omnidev.py setup
  python3 omnidev.py agent
  python3 omnidev.py test
"""

import os, sys

# Ensure shared.py is importable from the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import shared
from shared import BORD

# ═══════════════════════════════════════════════════════════════════════════
#  Dev settings
# ═══════════════════════════════════════════════════════════════════════════

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", "/opt/workspace")
# The omnidev stack runs its data dir (config/profiles/wiki/memories) from the
# omni-root mirror repo, NOT omni-stack. omni-stack stays the deploy/omnideploy
# data dir (deploy.py). Both repos are kept identical in content; the stack just
# binds the omni-root checkout.
OMNI_ROOT_DIR = os.path.join(WORKSPACE_DIR, "omni-root")

settings = shared.Settings(
    env_path=os.path.join(SCRIPT_DIR, "omnidev.env"),
    compose_file=os.path.join(OMNI_ROOT_DIR, "docker-compose.yml"),
    dev_overlay=os.path.join(OMNI_ROOT_DIR, "docker-compose.dev.yml"),
    project_name="omnidev",
    container="omnidev-omniagent-1",
    setup_channel="dev-channel",
    omni_stack_dir=OMNI_ROOT_DIR,
    workspace_dir=WORKSPACE_DIR,
    script_dir=SCRIPT_DIR,
    use_api=False,
)

shared.init(settings)


def patch_channel_to_deepseek():
    """Patch the Mattermost dev-channel to deepseek/deepseek-v4-flash.

    Without this, the channel keeps the default provider (noop/test-tool-caller
    from the platform setup), so the agent test would get the noop canned reply
    instead of a real deepseek answer.
    """
    s = shared.sett()
    print("\n[Configuring channel provider/model...]")
    try:
        channels = shared.oc_curl("GET", "/channels")
        data = channels.get("data", [])
        ch = next((c for c in data if c.get("platform") == "mattermost"
                   and c.get("name") == "mattermost-" + s.setup_channel), None)
        if not ch:
            ch = next((c for c in data if c.get("platform") == "mattermost"), None)
        if not ch:
            raise RuntimeError("No mattermost channel found to patch")
        shared.oc_curl("PATCH", "/channels/" + str(ch["id"]), {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
        })
        print(f"  Channel patched to deepseek/deepseek-v4-flash (id={ch['id']}, name={ch.get('name')})")
    except Exception as e:
        raise RuntimeError(f"Could not patch channel to deepseek: {e}")


# ═══════════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════════

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="OmniStack dev launcher: build, start, configure, and test the stack",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    setup_parser = subparsers.add_parser("setup", help="Build, start, and configure the stack")

    subparsers.add_parser("agent", help="Send math question via Mattermost and verify")

    subparsers.add_parser("test", help="Comprehensive plugin/tool testing")
    subparsers.add_parser(
        "prepare",
        help="Create mm-kanban MM channel, register via $new, patch to opencode-go/deepseek-v4-flash, enable opencode-go provider + all builtin tool MCPs",
    )

    args = parser.parse_args()

    if args.command == "setup":
        shared.setup()
        patch_channel_to_deepseek()
    elif args.command == "agent":
        shared.agent()
    elif args.command == "test":
        shared._check_container()
        shared.run_tests()
    elif args.command == "prepare":
        shared._check_container()
        shared.prepare()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
