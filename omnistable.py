#!/usr/bin/env python3
"""
OmniStack stable launcher — thin wrapper around shared.py.

Usage:
  python3 omnistable.py setup
  python3 omnistable.py agent
  python3 omnistable.py test

The `setup` command:
  1. Generates omnistable.env with random passwords and the latest GHCR
     :latest image tags (omniagent, dashboard, toolbox)
  2. Starts Docker Compose (project=omnistable, profiles=noop,mattermost)
     with `--pull always` so the newest :latest images are always fetched
     (the `memory` profile — hindsight+qdrant — is DEV-only; stable does
     not run it, see shared.py generate_env)
  3. Creates secrets (from secrets.env + Mattermost credentials)
  4. Enables + configures + runs the Mattermost platform setup
     (team `omni`, channel `stable-channel`, admin/test/bot users)
  5. Patches the stable-channel to deepseek/deepseek-v4-flash

IMPORTANT — image-based deployment: omnistable runs the omniagent binary and
all built-in plugin binaries FIXED in the CI-built image (no dev overlay, no
source repo mount at /app). The omniagent + built-in plugins are only updated
when a new CI stable build publishes new images; bundled/remote plugins can be
added at runtime via the plugin API. Source builds from the repo are what
omnidev does — never omnistable.

The `test` command runs the full tool-test suite (Phase 0/1/2) against the
stable stack. The `agent` command posts a math question to #stable-channel
and verifies the deepseek agent answers correctly.
"""

import os
import sys

# Ensure shared.py is importable from the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import shared
from shared import BORD

# ═══════════════════════════════════════════════════════════════════════════
#  Stable settings
# ═══════════════════════════════════════════════════════════════════════════

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", "/opt/workspace")
# The omnistable stack runs its data dir (config/profiles/wiki/memories) from the
# omni-root mirror repo, NOT omni-stack. omni-stack stays the deploy/omnideploy
# data dir (deploy.py). Both repos are kept identical in content; the stack just
# binds the omni-root checkout.
OMNI_ROOT_DIR = os.path.join(WORKSPACE_DIR, "omni-root")

settings = shared.Settings(
    env_path=os.path.join(SCRIPT_DIR, "omnistable.env"),
    compose_file=os.path.join(OMNI_ROOT_DIR, "docker-compose.yml"),
    dev_overlay=None,  # stable mode — pull production GHCR images, no source build
    project_name="omnistable",
    container="omnistable-omniagent-1",
    setup_channel="stable-channel",
    base_url="http://localhost:8080",
    omni_stack_dir=OMNI_ROOT_DIR,
    workspace_dir=WORKSPACE_DIR,
    script_dir=SCRIPT_DIR,
    # use_api=False (docker-exec mode): the host cannot reach localhost:8080
    # (docker-proxy binding quirk), but curl inside the container always can.
    use_api=False,
)

shared.init(settings)


def patch_channel_to_deepseek():
    """Patch the Mattermost stable-channel to deepseek/deepseek-v4-flash.

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
        description="OmniStack stable launcher: pull latest images, start, configure, and test the stack",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    setup_parser = subparsers.add_parser(
        "setup", help="Pull latest images, start the stack, configure omniagent + mattermost"
    )

    subparsers.add_parser("agent", help="Send math question via Mattermost and verify the agent response")
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
