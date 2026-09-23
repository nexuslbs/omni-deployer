#!/usr/bin/env python3
"""Post a message (the noop test-tool-caller script) into a dev Mattermost channel.

Used by efficiency-noop-gate.sh for GATE A/B. Talks to the Mattermost REST API
directly: `mmctl --local post create` (mmctl 10.x) resolves to a URL the dev
server rejects ("could not find the page /api/v4/posts"), so the harness uses
/api/v4/users/login + /api/v4/posts instead.

Env:
  MM_URL            base URL              (default http://mattermost:8065)
  MM_LOGIN          login id              (default lucasbasquerotto)
  MM_PASSWORD       password              (default Mattermost_Fresh_Start_1)
  MM_TEAM           team name             (default omni)
  MM_CHANNEL        channel name          (default test-channel)
  MM_MESSAGE_FILE   file with the message (default: stdin)

Prints one JSON line: {"channel","channel_id","post_id","len"}.
"""
import json
import os
import sys
import urllib.request

MM_URL = os.environ.get("MM_URL", "http://mattermost:8065").rstrip("/")
LOGIN = os.environ.get("MM_LOGIN", "lucasbasquerotto")
PASSWORD = os.environ.get("MM_PASSWORD", "Mattermost_Fresh_Start_1")
TEAM = os.environ.get("MM_TEAM", "omni")
CHANNEL = os.environ.get("MM_CHANNEL", "test-channel")
MESSAGE_FILE = os.environ.get("MM_MESSAGE_FILE", "")
ROOT_ID = os.environ.get("MM_ROOT_ID", "")


def req(path, body=None, token=None):
    """JSON request; returns (parsed_body, response_headers)."""
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(MM_URL + path, data=data,
                               method="POST" if data is not None else "GET")
    if data is not None:
        r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(r, timeout=25) as resp:
        return json.loads(resp.read().decode() or "{}"), dict(resp.headers)


def login():
    """Mattermost returns the session token in the `Token` response header."""
    body, headers = req("/api/v4/users/login", {"login_id": LOGIN, "password": PASSWORD})
    tok = headers.get("Token") or headers.get("token") or body.get("token")
    if not tok:
        raise SystemExit("login returned no session token (headers=%s)" % list(headers))
    return tok


def main():
    msg = open(MESSAGE_FILE).read() if MESSAGE_FILE else sys.stdin.read()
    if not msg.strip():
        print(json.dumps({"error": "empty message"}))
        return 2
    token = login()
    team, _ = req("/api/v4/teams/name/" + TEAM, token=token)
    chan, _ = req("/api/v4/teams/%s/channels/name/%s" % (team["id"], CHANNEL), token=token)
    body = {"channel_id": chan["id"], "message": msg}
    if ROOT_ID:
        body["root_id"] = ROOT_ID
    post, _ = req("/api/v4/posts", body, token=token)
    print(json.dumps({"channel": chan["name"], "channel_id": chan["id"],
                      "post_id": post["id"], "len": len(msg)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
