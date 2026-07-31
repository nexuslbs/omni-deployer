#!/usr/bin/env python3
"""
OmniStack dev launcher — thin wrapper around shared.py.

Usage:
  python3 omnidev.py setup --deepseek-api-key <key>
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


def _load_deepseek_key():
    """Read DEEPSEEK_API_KEY from the data .env file without printing it."""
    candidates = [
        "/opt/data/.env",
        "/opt/omni/data/.env",
    ]
    for path in candidates:
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DEEPSEEK_API_KEY"):
                        key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if key and not key.startswith("$"):
                            return key
        except OSError:
            continue
    return None


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
            "current_provider": "deepseek",
            "current_model": "deepseek-v4-flash",
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
    setup_parser.add_argument(
        "--deepseek-api-key", default=None,
        help="DeepSeek API key (defaults to DEEPSEEK_API_KEY from /opt/data/.env)",
    )

    subparsers.add_parser("agent", help="Send math question via Mattermost and verify")

    subparsers.add_parser("test", help="Comprehensive plugin/tool testing")

    args = parser.parse_args()

    if args.command == "setup":
        key = args.deepseek_api_key or _load_deepseek_key()
        if not key:
            print("ERROR: --deepseek-api-key not provided and DEEPSEEK_API_KEY not found in /opt/data/.env")
            sys.exit(1)
        shared.setup(key)
        patch_channel_to_deepseek()
    elif args.command == "agent":
        shared.agent()
    elif args.command == "test":
        shared._check_container()
        shared.run_tests()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
