#!/usr/bin/env python3
"""efficiency-noop-gate.py - noop-provider E2E gate for the agent EFFICIENCY contract.

Root-cause fix verification for the re-read / no-progress / ignored-stop-signal loop
class (incident thread 2874, post-mortem thread 2907). No real LLM is needed: the
noop model `test-tool-caller` replays the FIRST user message as a JSON array of tool
calls, so we can script the exact thread-2874 duplicate-read pattern.

Gates:
  A) duplicate-read stub + counter: the SAME file is read three times with OVERLAPPING
     (not byte-identical) ranges. The second/third call must come back as a
     "[duplicate read" stub and the thread duplicate-read counter must be > 0.
  B) merged sub_cause on a LIVE thread: a reply posted into the running thread while it
     is still processing must raise the "=== LIVE INTERRUPT ===" marker.
  C) fatal provider error (402 / insufficient balance / auth) is classified as fatal
     (cargo test --lib efficiency::).

Run inside the omniagent container of the dev stack (needs the docker socket for the
DB read):  python3 /opt/workspace/omni-deployer/scripts/efficiency-noop-gate.py
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

SCRIPTS = "/opt/workspace/omni-deployer/scripts"
OMNI_DIR = os.environ.get("OMNI_DIR", "/opt/omni")
PG = os.environ.get("PG_CONTAINER", "omnidev-postgres")
TIMEOUT = int(os.environ.get("TIMEOUT", "120"))
MARK = "effgate%d" % int(time.time())

results = []


def report(gate, ok, detail):
    results.append((gate, ok, detail))
    print("%s  %s  %s" % ("PASS" if ok else "FAIL", gate, detail), flush=True)


def db(sql):
    for args in (["docker", "exec", PG, "psql", "-U", "omniagent", "-d", "omniagent", "-tAc", sql],
                 ["docker", "exec", PG, "psql", "-U", "postgres", "-d", "omniagent", "-tAc", sql]):
        p = subprocess.run(args, capture_output=True, text=True)
        if p.returncode == 0:
            return p.stdout.strip()
    return ""


def yaml_noop_channel():
    """Resolve the dedicated noop test channel id from channels.yml (resource_identifier)."""
    for cfg in (os.path.join(OMNI_DIR, "config", "channels.yml"),
                "/opt/workspace/omni-root/config/channels.yml"):
        if not os.path.exists(cfg):
            continue
        blk_name, blk_lines = None, []
        blocks = {}
        for line in open(cfg, encoding="utf-8", errors="replace"):
            if re.match(r"^[a-z0-9_-]+:\s*$", line):
                if blk_name:
                    blocks[blk_name] = blk_lines
                blk_name, blk_lines = line.strip().rstrip(":"), [line]
            elif blk_name:
                blk_lines.append(line)
        if blk_name:
            blocks[blk_name] = blk_lines
        for name, lines in blocks.items():
            body = "".join(lines)
            if re.search(r"(noop|test-tool-caller)", body) and "resource_identifier" in body:
                rid = re.search(r"resource_identifier:\s*([A-Za-z0-9]+)", body)
                if rid:
                    print("noop channel %r -> %s (%s)" % (name, rid.group(1), cfg), flush=True)
                    return rid.group(1)
    return ""


def resolution():
    """(mm_base, channel_id, user_token) using the dev-stack test harness helpers."""
    sys.path.insert(0, SCRIPTS)
    import tests  # noqa: E402  module-level constants only; main() is __main__-guarded

    base = getattr(tests, "MM", "http://mattermost:8065")
    cid = ""
    try:
        cid = tests._wf_dedicated_channel() or ""
    except Exception as exc:  # noqa: BLE001
        print("[warn] _wf_dedicated_channel failed: %r" % exc, flush=True)
    cid = cid or yaml_noop_channel()

    pw = "Mattermost_Fresh_Start_1"
    try:
        got = tests._get_secret_value("MATTERMOST_TEST_PASSWORD", pw)
        pw = got or pw
    except Exception:  # noqa: BLE001
        pass
    token = ""
    for user in ("testuser", "admin"):
        try:
            token = tests._mm_login(base, user, pw)
        except Exception as exc:  # noqa: BLE001
            print("[warn] login %s failed: %r" % (user, exc), flush=True)
        if token:
            print("logged in as %s @ %s" % (user, base), flush=True)
            break
    return base, cid, token


def post(base, cid, token, message, root_id=None):
    body = {"channel_id": cid, "message": message}
    if root_id:
        body["root_id"] = root_id
    req = urllib.request.Request(
        base + "/api/v4/posts", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    return json.loads(urllib.request.urlopen(req, timeout=20).read().decode())


def wait_marker(last_id, needle, label, timeout=TIMEOUT):
    """Poll the messages table for a row whose content contains `needle`."""
    deadline = time.time() + timeout
    hits = []
    while time.time() < deadline:
        out = db("SELECT id||'|'||replace(left(content,300),E'\\n',' ') FROM messages "
                 "WHERE id > %s AND content LIKE '%%%s%%' ORDER BY id LIMIT 4" % (last_id, needle))
        hits = [r for r in out.splitlines() if r.strip()]
        if hits:
            break
        time.sleep(3)
    print("  %s: %d hit(s)" % (label, len(hits)), flush=True)
    for h in hits[:3]:
        print("    " + h[:260], flush=True)
    return hits


def main():
    base, cid, token = resolution()
    if not (cid and token):
        report("ENV", False, "noop channel=%r token=%r (dev stack not reachable)" % (cid, bool(token)))
        return 1

    # ---- scripted pattern: SAME file, overlapping ranges (thread 2874 signature) ----
    script = [
        {"tool": "filesystem_read", "arguments": {"path": "/opt/omni/README.md", "limit": 120}},
        {"tool": "filesystem_read", "arguments": {"path": "/opt/omni/README.md", "offset": 50, "limit": 300}},
        {"tool": "filesystem_read", "arguments": {"path": "/opt/omni/README.md", "offset": 80, "limit": 200}},
        {"tool": "filesystem_read", "arguments": {"path": "/opt/omni/README.md", "offset": 60, "limit": 150}},
        {"tool": "notes_note_write", "arguments": {"name": MARK + ".md", "content": "effgate " + MARK}},
        {"tool": "notes_note_read", "arguments": {"name": MARK + ".md"}},
    ]
    last_id = int(db("SELECT coalesce(max(id),0) FROM messages") or 0)
    print("== EFFICIENCY NOOP GATE == mark=%s channel=%s last_msg_id=%s" % (MARK, cid, last_id), flush=True)
    root = post(base, cid, token, json.dumps(script))
    root_id = root.get("id")
    print("posted scripted thread root=%s" % root_id, flush=True)

    # ---------------------------------------------------------------- GATE A
    hits = wait_marker(last_id, "duplicate read", "GATE A duplicate-read stubs")
    counter = db("SELECT count(*) FROM messages WHERE content LIKE '%%duplicate read%%' AND id > %s" % last_id)
    report("GATE A", bool(hits) and int(counter or 0) > 0,
           "duplicate-read stubs=%s, thread counter=%s (>0 required) - overlapping-range re-read blocked"
           % (len(hits), counter or 0))

    # ---------------------------------------------------------------- GATE B
    time.sleep(2)
    try:
        post(base, cid, token,
             "[sub_cause] why are you taking so long? stop and report what you have.",
             root_id=root_id)
        print("posted merged sub_cause into the LIVE thread", flush=True)
    except Exception as exc:  # noqa: BLE001
        print("[warn] sub_cause post failed: %r" % exc, flush=True)
    hits_b = wait_marker(last_id, "LIVE INTERRUPT", "GATE B LIVE INTERRUPT markers")
    report("GATE B", bool(hits_b),
           "merged sub_cause raised LIVE INTERRUPT=%s (no 35-min silence)" % bool(hits_b))

    # ---------------------------------------------------------------- GATE C
    src = "/opt/workspace/omniagent/src/agent/efficiency.rs"
    static_ok = os.path.exists(src) and "classify_fatal_provider_error" in open(src, encoding="utf-8", errors="replace").read()
    p = subprocess.run(["bash", "-lc", "cd /app && cargo test --lib efficiency:: 2>&1 | tail -6"],
                       capture_output=True, text=True)
    unit_ok = "test result: ok" in p.stdout and "0 failed" in p.stdout
    report("GATE C", static_ok and unit_ok,
           "fatal-provider classification present=%s, efficiency unit suite green=%s"
           % (static_ok, unit_ok))

    print("\n== RESULT: %d passed, %d failed ==" % (sum(1 for _, o, _ in results if o),
                                                   sum(1 for _, o, _ in results if not o)), flush=True)
    return 0 if all(o for _, o, _ in results) else 2


if __name__ == "__main__":
    sys.exit(main())
