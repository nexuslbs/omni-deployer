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
    setup_parser.add_argument("--deepseek-api-key", required=True, help="DeepSeek API key")

    subparsers.add_parser("agent", help="Send math question via Mattermost and verify")

    subparsers.add_parser("test", help="Comprehensive plugin/tool testing")

    args = parser.parse_args()

    if args.command == "setup":
        shared.setup(args.deepseek_api_key)
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
