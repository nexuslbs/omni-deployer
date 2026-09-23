#!/usr/bin/env python3
"""Post a message into a dev Mattermost channel by CHANNEL ID (REST v4).

Env: MM_URL, MM_LOGIN, MM_PASSWORD, MM_CHANNEL_ID, MM_MESSAGE_FILE
The session token comes back in the `Token` response header (not the JSON body).
"""
import json
import os
import sys
import urllib.request


def main():
    url = os.environ.get("MM_URL", "http://mattermost:8065").rstrip("/")
    login = os.environ["MM_LOGIN"]
    password = os.environ["MM_PASSWORD"]
    chan_id = os.environ["MM_CHANNEL_ID"]
    msg_file = os.environ["MM_MESSAGE_FILE"]
    msg = open(msg_file).read()

    req = urllib.request.Request(
        url + "/api/v4/users/login",
        data=json.dumps({"login_id": login, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        token = resp.headers.get("Token")
    if not token:
        print("ERROR: no Token header on login", file=sys.stderr)
        return 2

    req = urllib.request.Request(
        url + "/api/v4/posts",
        data=json.dumps({"channel_id": chan_id, "message": msg}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    print(json.dumps({"channel_id": chan_id, "post_id": data.get("id"), "len": len(msg)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
