#!/usr/bin/env python3
"""Diagnose/prepare a LIVE (real-provider) efficiency measurement channel.

Env:
  MM_URL       e.g. http://mattermost:8065
  MM_LOGIN     admin login
  MM_PASSWORD  admin password
  MM_BOT_TOKEN omniagent bot access token
  MM_TEAM      team name (default omni)
  MM_WANT_CHAN channel name to ensure exists (default eff-live)
  MM_CFG_CHAN_ID channel id already present in the agent config (optional)

Prints JSON: team_id, config_chan_status, bot {id, username}, bot_channels, want_chan {id, created}
"""
import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("MM_URL", "http://mattermost:8065").rstrip("/")


def call(path, token=None, data=None, method=None):
    req = urllib.request.Request(URL + path, data=json.dumps(data).encode() if data is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if method:
        req.get_method = lambda: method
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


def main():
    out = {}
    st, login = call("/api/v4/users/login",
                     data={"login_id": os.environ["MM_LOGIN"], "password": os.environ["MM_PASSWORD"]})
    # token is in the response header; redo with header capture
    req = urllib.request.Request(URL + "/api/v4/users/login",
                                 data=json.dumps({"login_id": os.environ["MM_LOGIN"],
                                                  "password": os.environ["MM_PASSWORD"]}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        token = r.headers.get("Token")
        me = json.loads(r.read().decode())
    out["admin"] = {"id": me["id"], "username": me["username"], "roles": me.get("roles")}

    team = os.environ.get("MM_TEAM", "omni")
    st, t = call("/api/v4/teams/name/" + team, token)
    out["team"] = {"id": t["id"] if st == 200 else None, "status": st}
    team_id = t["id"]

    cfg_id = os.environ.get("MM_CFG_CHAN_ID")
    if cfg_id:
        st, c = call("/api/v4/channels/" + cfg_id, token)
        out["config_chan"] = {"id": cfg_id, "status": st, "name": c.get("name") if st == 200 else c}

    bot_token = os.environ.get("MM_BOT_TOKEN")
    if bot_token:
        st, bot = call("/api/v4/users/me", bot_token)
        out["bot"] = {"status": st, "id": bot.get("id") if st == 200 else None,
                      "username": bot.get("username") if st == 200 else bot}
        if st == 200:
            st2, chans = call("/api/v4/users/%s/teams/%s/channels" % (bot["id"], team_id), token)
            if st2 == 200:
                out["bot_channels"] = [{"name": c["name"], "id": c["id"]} for c in chans]

    want = os.environ.get("MM_WANT_CHAN", "eff-live")
    st, ch = call("/api/v4/teams/%s/channels/name/%s" % (team_id, want), token)
    if st == 200:
        out["want_chan"] = {"id": ch["id"], "created": False}
    else:
        st, ch = call("/api/v4/channels", token,
                      {"team_id": team_id, "name": want, "display_name": want, "type": "O"})
        out["want_chan"] = {"id": ch.get("id") if st in (200, 201) else None,
                            "created": st in (200, 201), "status": st, "err": ch if st not in (200, 201) else None}
    if out["want_chan"].get("id"):
        wid = out["want_chan"]["id"]
        # ensure admin + bot are members
        for who, tok in (("admin", token), ("bot", bot_token)):
            if not tok:
                continue
            st, m = call("/api/v4/channels/%s/members" % wid, tok, {"user_id": out["admin"]["id"] if who == "admin" else out["bot"]["id"]})
            out.setdefault("membership", {})[who] = st
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
