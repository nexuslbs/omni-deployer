#!/usr/bin/env python3
"""Canonical A/B corpus runner for the omniagent internal improvement plan.

Workstream T, item T-4 (plan page: Projects/Omniagent/
Omniagent-Internal-Improvement-Plan.md, section 3T, candidate S11).

Runs the canonical task corpus (scripts/corpus/tNN-*.json) against the
omnidev dev core and compares the section 3T gates between two sides:

  side a (baseline)  : the currently deployed omnidev binary (main)
  side b (candidate) : same binary in identity mode, or a binary built from
                       --candidate <git-ref> of the omniagent repo

Gates per section 3T:
  T-1 helpfulness: per-task status/outcome, iterations, duplicate-read
                   markers, compaction events, retrieval calls, grounded
                   responses, token totals (success rate, median/p90
                   iterations are computed across the side).
  T-2 speed      : search p50/p95 latency probes + prompt-generate build ms
                   on sample threads of the side.
  T-3 resources  : DB size, data-dir (spill/threads) sizes, container RSS
                   measured before and after the side.

The identity default (no --candidate) proves corpus stability: every task
must complete with the same outcome on both sides.

Environment: run from a docker.sock-capable host on the omnidev compose
network (e.g. omnidev-toolbox-1), with the omnidev stack up. Only omnidev
containers are touched; production omni-stack/omnistable are never touched.

No emdashes in output text.
"""
import argparse
import datetime
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CORPUS_DIR = os.path.join(SCRIPT_DIR, "corpus")
DEFAULT_OUTROOT = "/opt/workspace/tmp/corpus-ab"
OMNIAGENT_CONTAINER = "omnidev-omniagent-1"
POSTGRES_CONTAINER = "omnidev-postgres-1"
API_BASE = "http://omnidev-omniagent-1:8080"
MM_BASE = "http://mattermost:8065"
MM_TEAM_LOOKUP = None  # discovered at runtime
CORE_CHANNEL = "mattermost-dev-channel"
MM_USER = "testuser"
MM_PASS = "Mattermost_Fresh_Start_1"
TOOLSET_PLUGINS = {
    "filesystem": {"enabled": True, "source": "built-in", "config": {}},
    "search": {"enabled": True, "source": "built-in",
               "config": {"database_url": "$env:DATABASE_URL"}},
    "notes": {"enabled": True, "source": "built-in",
              "config": {"omni_dir": "$env:OMNI_DIR"}},
    "git": {"enabled": True, "source": "built-in",
            "config": {
                "github_app_id": 3967918,
                "github_app_private_key": "$secret:GITHUB_APP_KEY",
                "github_installation_id": 138119822}},
    "memory": {"enabled": True, "source": "built-in", "config": {}},
    "prompt": {"enabled": True, "source": "built-in", "config": {}},
    "subtasks": {"enabled": True, "source": "built-in", "config": {}},
}
PROBE_TOOL_NAMES = [
    "filesystem_info", "search_messages", "notes_note-list",
    "git_status", "memory_list-memories", "subtasks_get-subtask-counts",
]
PROMPT_PROBE_TOOL_NAMES = [
    "filesystem_read", "filesystem_search", "filesystem_list",
    "search_messages", "search_wiki", "notes_note-write",
    "notes_note-append", "git_run-command",
]

# ---------------------------------------------------------------------------
# Shell helpers (run from a docker.sock-capable host)
# ---------------------------------------------------------------------------
def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def oc(container, cmd):
    r = sh("docker exec -i %s sh -c %s" % (container, _sq(cmd)))
    return r


def _sq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def psql(sql):
    """Run read-only SQL on the omnidev dev DB; returns stdout text."""
    inner = 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U omniagent -d omniagent -At -c %s' % _sq(sql)
    r = oc(POSTGRES_CONTAINER, inner)
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def api_get(path, timeout=15):
    req = urllib.request.Request(API_BASE + path)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        raise RuntimeError("GET %s failed: %s" % (path, exc))


def api_execute(tool, arguments, timeout=90):
    """Call a tool via /mcp/execute (no thread context)."""
    body = json.dumps({"name": tool, "arguments": arguments or {}}).encode()
    req = urllib.request.Request(API_BASE + "/mcp/execute", data=body,
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return time.time() - t0, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()[:500]
        return time.time() - t0, "HTTPERR %s %s" % (exc.code, raw)
    except Exception as exc:
        return time.time() - t0, "ERR %s" % exc


def mm_login():
    body = json.dumps({"login_id": MM_USER, "password": MM_PASS}).encode()
    req = urllib.request.Request(MM_BASE + "/api/v4/users/login", data=body,
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        hdrs = dict(resp.headers)
        token = hdrs.get("Token") or hdrs.get("token")
        if not token:
            raise RuntimeError("MM login returned no token header")
        return token


def mm_get(path, token):
    req = urllib.request.Request(MM_BASE + path,
                                 headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def mm_post(path, payload, token):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(MM_BASE + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def mm_find_channel(token, team_id, name):
    for ch in mm_get("/api/v4/teams/%s/channels" % team_id, token):
        if ch.get("name") == name:
            return ch["id"]
    return None


def mem_to_bytes(s):
    """'1.234GiB' -> bytes; handles B/KiB/MiB/GiB."""
    s = (s or "").strip()
    m = re.match(r"^([0-9.]+)\s*([KMG]?i?B|B)$", s)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).lower()
    mult = {"b": 1, "kb": 1e3, "kib": 1024, "mb": 1e6, "mib": 1024 ** 2,
            "gb": 1e9, "gib": 1024 ** 3}.get(unit)
    return int(val * mult) if mult else None

# ---------------------------------------------------------------------------
# Corpus loading / validation
# ---------------------------------------------------------------------------
class CorpusError(Exception):
    pass


def load_corpus(corpus_dir=CORPUS_DIR):
    tasks = []
    for fn in sorted(os.listdir(corpus_dir)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(corpus_dir, fn)) as f:
            t = json.load(f)
        tasks.append(t)
    return tasks


def validate_corpus(tasks):
    ids = [t["id"] for t in tasks]
    if not (5 <= len(tasks) <= 10):
        raise CorpusError("corpus size %d outside 5..10" % len(tasks))
    if len(set(ids)) != len(ids):
        raise CorpusError("duplicate task ids")
    known_shapes = {"project-layout-discovery", "re-read-heavy-verification",
                    "search-recall", "long-task-150-plus",
                    "fresh-thread-follow-up"}
    for t in tasks:
        for field in ("id", "shape", "title", "prompt", "expected",
                      "timeout_min", "target_iterations"):
            if field not in t:
                raise CorpusError("%s missing field %s" % (t.get("id"), field))
        if not re.match(r"^t\d\d-", t["id"]):
            raise CorpusError("%s: id must match tNN-" % t["id"])
        if t["shape"] not in known_shapes:
            raise CorpusError("%s: unknown shape %s" % (t["id"], t["shape"]))
        exp = t["expected"]
        for field in ("substrings", "min_substrings", "regex", "require_token"):
            if field not in exp:
                raise CorpusError("%s: expected missing %s" % (t["id"], field))
        if exp["require_token"] not in (None, "token", "witness"):
            raise CorpusError("%s: bad require_token" % t["id"])
    return True


# ---------------------------------------------------------------------------
# Placeholder fill / expectation evaluation
# ---------------------------------------------------------------------------
def fill_prompt(t, side, run, token, witness, wit_prefix):
    """side: 'a'|'b'; fills {TOKEN} {WITNESS} {WITNESS_PREFIX}
    {TASKS_DIR} {RUN_DIR}."""
    p = t["prompt"]
    p = p.replace("{TOKEN}", token)
    p = p.replace("{WITNESS}", witness)
    p = p.replace("{WITNESS_PREFIX}", wit_prefix)
    p = p.replace("{TASKS_DIR}", CORPUS_DIR)
    p = p.replace("{RUN_DIR}", run["outdir"])
    return p


def eval_expectation(t, final_text, token, witness):
    """Returns (passed, detail dict)."""
    exp = t["expected"]
    detail = {"matched": [], "regex": None, "regex_ok": None,
              "token_ok": None}
    subs = exp.get("substrings") or []
    matched = [s for s in subs if s in (final_text or "")]
    detail["matched"] = matched
    sub_ok = len(matched) >= int(exp.get("min_substrings", 0))
    if exp.get("regex"):
        m = re.search(exp["regex"], final_text or "")
        detail["regex"] = m.group(1) if m else None
        detail["regex_ok"] = m is not None
    need = exp.get("require_token")
    if need == "token":
        detail["token_ok"] = bool(token) and token in (final_text or "")
    elif need == "witness":
        detail["token_ok"] = bool(witness) and witness in (final_text or "")
    else:
        detail["token_ok"] = True
    ok = sub_ok and (detail["regex_ok"] is not False) and bool(detail["token_ok"])
    detail["pass"] = ok
    return ok, detail


# ---------------------------------------------------------------------------
# Toolset ensure (ephemeral plugins.yml write inside the omnidev container)
# ---------------------------------------------------------------------------
TOOLSET_HEAD = (
    "platforms:\n"
    "  mattermost:\n"
    "    enabled: true\n"
    "    source: built-in\n"
    "    config:\n"
    "      server_url: http://mattermost:8065\n"
    "      access_token_name: MATTERMOST_ACCESS_TOKEN\n"
    "      setup_team: omni\n"
    "      setup_channel: dev-channel\n"
    "      admin_user: lucasbasquerotto\n"
    "      admin_password: $secret:MATTERMOST_ADMIN_PASSWORD\n"
    "      test_user: testuser\n"
    "      test_password: $secret:MATTERMOST_TEST_PASSWORD\n"
    "      bot_user: omnibot\n"
    "      bot_password: $secret:MATTERMOST_BOT_PASSWORD\n"
)


def _toolset_yaml():
    lines = [TOOLSET_HEAD, "tools:"]
    for name in sorted(TOOLSET_PLUGINS):
        cfg = TOOLSET_PLUGINS[name]
        lines.append("  %s:" % name)
        lines.append("    enabled: %s" % str(cfg["enabled"]).lower())
        lines.append("    source: %s" % cfg["source"])
        lines.append("    config: %s" % json.dumps(cfg["config"]))
    lines.append("providers:")
    lines.append("  deepseek:")
    lines.append("    enabled: true")
    lines.append("    source: built-in")
    lines.append("    config:")
    lines.append("      api_key: $secret:DEEPSEEK_API_KEY")
    lines.append("  noop:")
    lines.append("    enabled: true")
    lines.append("    source: bundled")
    lines.append("    config: {}")
    return "\n".join(lines) + "\n"


def read_plugins_yml():
    r = oc(OMNIAGENT_CONTAINER, "cat /opt/omni-stack/config/plugins.yml 2>/dev/null")
    return r.stdout if r.returncode == 0 else None


def write_plugins_yml(content, run):
    tmp = os.path.join(run["outdir"], "plugins.yml.tmp")
    with open(tmp, "w") as f:
        f.write(content)
    r = sh("docker cp %s %s:/opt/omni-stack/config/plugins.yml" % (tmp, OMNIAGENT_CONTAINER))
    if r.returncode != 0:
        raise RuntimeError("could not write plugins.yml into container: " + r.stderr[:300])
    os.unlink(tmp)


def read_profiles_yml():
    r = oc(OMNIAGENT_CONTAINER, "cat /opt/omni-stack/config/profiles.yml 2>/dev/null")
    return r.stdout if r.returncode == 0 else None


def write_profiles_yml(content, run):
    tmp = os.path.join(run["outdir"], "profiles.yml.tmp")
    with open(tmp, "w") as f:
        f.write(content)
    r = sh("docker cp %s %s:/opt/omni-stack/config/profiles.yml" %
           (tmp, OMNIAGENT_CONTAINER))
    if r.returncode != 0:
        raise RuntimeError("could not write profiles.yml into container: " +
                           r.stderr[:300])


def _repo_allowlist():
    """omni profile allowed_tools from the runtime config/profiles.yml (the
    dev mount /opt/omni/config/profiles.yml is the source checkout's config).
    An extra candidate can be supplied with OMNI_SOURCE_CONFIG, so host-side
    runs never depend on a hard-coded checkout path. Corpus threads only get
    the tools the profile declares (profiles.yml allowed_tools); mirroring the
    runtime allowlist keeps the dev profile identical to production."""
    candidates = ["/opt/omni/config/profiles.yml"]
    if os.environ.get("OMNI_SOURCE_CONFIG"):
        candidates.append(os.environ["OMNI_SOURCE_CONFIG"])
    for p in candidates:
        try:
            with open(p) as f:
                txt = f.read()
        except Exception:
            continue
        m = re.search(r"profiles:\s*\n\s+omni:\s*\n(?P<body>(?:[ \t].*\n|\n)*)", txt)
        if not m:
            continue
        tools = re.findall(r"^\s+-\s+(\S+)\s*$", m.group("body"), re.M)
        if tools:
            return tools
    return None


def _profiles_yaml():
    tools = _repo_allowlist()
    if not tools:
        raise RuntimeError("could not read omni allowed_tools from repo "
                           "config/profiles.yml")
    lines = ["profiles:", "  omni:", "    allowed_tools:"]
    for t in tools:
        lines.append("    - " + t)
    return "\n".join(lines) + "\n"


def ensure_toolset(run, verbose=True):
    """Enable the standard tool plugins on the omnidev core for the run.
    Backs up the original plugins.yml (first call) into run['toolset_backup'].
    """
    if run.get("no_toolset"):
        return {"enabled": False, "reason": "--no-toolset"}
    if run.get("toolset_backup") is None:
        run["toolset_backup"] = read_plugins_yml()
        if run["toolset_backup"]:
            with open(os.path.join(run["outdir"], "plugins.yml.original"), "w") as f:
                f.write(run["toolset_backup"])
        run["profiles_backup"] = read_profiles_yml()
        if run["profiles_backup"]:
            with open(os.path.join(run["outdir"], "profiles.yml.original"), "w") as f:
                f.write(run["profiles_backup"])
    cur = read_plugins_yml() or ""
    want = _toolset_yaml()
    pcur = read_profiles_yml() or ""
    pwant = _profiles_yaml()
    if run.get("toolset_restarted"):
        status = "already-restarted"
    elif cur.strip() == want.strip() and pcur.strip() == pwant.strip():
        status = "already-ok"
    else:
        write_plugins_yml(want, run)
        write_profiles_yml(pwant, run)
        status = "written"
        if verbose:
            print("  [toolset] plugins.yml + profiles.yml %s (backups kept "
                  "in outdir)" % status)
        # Providers/plugins are read at startup; restart the dev core once per
        # run so the corpus runs with the standard toolset + provider config.
        print("  [toolset] restarting %s to load config" % OMNIAGENT_CONTAINER,
              flush=True)
        r = sh("docker restart %s" % OMNIAGENT_CONTAINER)
        if r.returncode != 0:
            raise RuntimeError("docker restart failed: " + (r.stderr or "")[:300])
        run["toolset_restarted"] = True
        _wait_healthy(240)
        if verbose:
            print("  [toolset] container healthy after restart")
    # Wait for registration by probing each tool via /mcp/execute.
    registered = {}
    for tool in PROBE_TOOL_NAMES:
        args = _probe_args(tool)
        ok = False
        deadline = time.time() + 150
        while time.time() < deadline:
            _, raw = api_execute(tool, args, timeout=25)
            low = raw.lower()
            if ("unknown tool" not in low and "not found" not in low
                    and "thread processing failed" not in low
                    and raw.startswith("HTTPERR") is False):
                ok = True
                break
            time.sleep(4)
        registered[tool] = ok
        if verbose:
            print("  [toolset] registered %-24s %s" % (tool, "yes" if ok else "NO"))
    return {"enabled": True, "status": status, "registered": registered}


def _probe_args(tool):
    return {
        "filesystem_info": {"path": "/opt/workspace"},
        "search_messages": {"query": "omnidev", "limit": 1},
        "notes_note-list": {},
        "git_status": {"repo_dir": "/opt/workspace/omni-deployer"},
        "memory_list-memories": {},
        "subtasks_get-subtask-counts": {},
    }.get(tool, {})


def restore_toolset(run):
    if run.get("toolset_backup"):
        write_plugins_yml(run["toolset_backup"], run)
        print("  [toolset] original plugins.yml restored")
    if run.get("profiles_backup"):
        write_profiles_yml(run["profiles_backup"], run)
        print("  [toolset] original profiles.yml restored")

# ---------------------------------------------------------------------------
# Metrics (gate SQL) per thread
# ---------------------------------------------------------------------------
def measure_thread(tid):
    """Collect T-1 gate numbers for one thread from the omnidev DB."""
    row = psql(
        "SELECT status, iterations, duration_ms, input_tokens, cached_tokens,"
        " output_tokens, started_at, ended_at FROM threads WHERE id=%d" % tid)
    fields = {}
    if row:
        parts = row.split("|")
        keys = ["status", "iterations", "duration_ms", "input_tokens",
                "cached_tokens", "output_tokens", "started_at", "ended_at"]
        for i, k in enumerate(keys):
            v = parts[i] if i < len(parts) else ""
            fields[k] = v if k in ("status", "started_at", "ended_at") else _num(v)
    def q(sql):
        v = psql(sql)
        return _num(v)
    m = {
        "thread_id": tid,
        "messages_total": q("SELECT count(*) FROM messages WHERE thread_id=%d" % tid),
        "duplicate_markers": q(
            "SELECT count(*) FROM messages WHERE thread_id=%d AND"
            " (content LIKE '%%[duplicate%%' OR content LIKE '%%duplicate of %%')" % tid),
        "compaction_events": q(
            "SELECT count(*) FROM messages WHERE thread_id=%d AND"
            " content LIKE '%%=== Compaction Summary ===%%'" % tid),
        "retrieval_calls": q(
            "SELECT count(*) FROM messages WHERE thread_id=%d AND msg_type='tool' AND"
            " (content LIKE '%%search_messages%%' OR content LIKE '%%search_wiki%%')" % tid),
        "grounded_total": q(
            "SELECT count(*) FROM messages WHERE thread_id=%d AND role='agent' AND"
            " msg_type IN ('message','summary')" % tid),
        "grounded_hits": q(
            "SELECT count(*) FROM messages WHERE thread_id=%d AND role='agent' AND"
            " msg_type IN ('message','summary') AND"
            " (metadata->'context'->>'total_chars') IS NOT NULL" % tid),
        "avg_prompt_per_call": q(
            "SELECT round(avg((token_usage->>'prompt_tokens')::bigint)) FROM messages"
            " WHERE thread_id=%d AND token_usage ? 'prompt_tokens'" % tid),
        "max_prompt_per_call": q(
            "SELECT max((token_usage->>'prompt_tokens')::bigint) FROM messages"
            " WHERE thread_id=%d AND token_usage ? 'prompt_tokens'" % tid),
    }
    m.update(fields)
    return m


def _num(v):
    try:
        if v is None or v == "":
            return None
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return None


def final_message(tid):
    return psql(
        "SELECT content FROM messages WHERE thread_id=%d AND role='agent'"
        " AND msg_type IN ('message','summary') ORDER BY id DESC LIMIT 1" % tid)


# ---------------------------------------------------------------------------
# Side execution
# ---------------------------------------------------------------------------
def post_task(mm_token, mm_channel_id, prompt):
    """Post a corpus task prompt to the dev-channel MM channel (creates a
    thread on the omnidev core). Returns the MM post."""
    return mm_post("/api/v4/posts", {"channel_id": mm_channel_id,
                                     "message": prompt}, mm_token)


def latest_thread_id():
    v = psql("SELECT coalesce(max(id),0) FROM threads")
    return int(v or 0)


def find_corpus_thread(since_id, task_id):
    """Poll the omnidev DB for the thread whose cause message carries the task
    marker '(id <task_id>,' and was created after since_id. Cause matching
    binds every task to its own thread even when unrelated or leftover
    threads interleave."""
    marker = task_id.split("-")[0]
    sql = ("SELECT t.id, t.status FROM threads t WHERE t.id > %d AND EXISTS ("
           "SELECT 1 FROM messages m WHERE m.thread_id = t.id AND"
           " m.role = 'cause' AND m.content LIKE '%%(id %s,%%')"
           " ORDER BY t.id LIMIT 1" % (since_id, marker))
    row = psql(sql)
    if not row:
        return None
    parts = row.split("|")
    if len(parts) < 2:
        return None
    return {"id": int(parts[0]), "status": parts[1]}


def run_task(mm_token, mm_channel_id, task, side_ctx, verbose=True):
    """Execute one corpus task end to end; returns a task result dict."""
    tid = None
    t0 = time.time()
    if verbose:
        print("  [task] %s posting..." % task["id"], flush=True)
    since = latest_thread_id()
    try:
        post_task(mm_token, mm_channel_id, side_ctx["prompts"][task["id"]])
    except Exception as exc:
        return {"task": task["id"], "shape": task["shape"], "status": "post-error",
                "error": str(exc)[:300], "duration_s": round(time.time() - t0),
                "outcome": "FAIL", "thread_id": None, "metrics": {},
                "detail": {}, "final_excerpt": ""}
    deadline = time.time() + int(task["timeout_min"]) * 60 * side_ctx.get("timeout_scale", 1.0)
    thread = None
    while time.time() < deadline:
        time.sleep(5)
        thread = find_corpus_thread(since, task["id"])
        if thread:
            st = thread.get("status")
            if st in ("completed", "error", "cancelled", "failed"):
                tid = thread.get("id")
                if verbose:
                    print("  [task] %s thread %s status=%s (%.0fs)" %
                          (task["id"], tid, st, time.time() - t0), flush=True)
                break
    if thread is None:
        return {"task": task["id"], "shape": task["shape"], "status": "timeout-no-thread",
                "error": "no thread appeared within timeout",
                "duration_s": round(time.time() - t0),
                "outcome": "FAIL", "thread_id": None, "metrics": {},
                "detail": {}, "final_excerpt": ""}
    if tid is None:
        # thread seen but not terminal within deadline
        tid = thread.get("id")
        return {"task": task["id"], "shape": task["shape"], "status": "timeout-running",
                "thread_id": tid, "error": "thread did not finish within timeout",
                "duration_s": round(time.time() - t0),
                "outcome": "FAIL", "metrics": {}, "detail": {},
                "final_excerpt": ""}
    metrics = measure_thread(tid)
    status = metrics.get("status") or "unknown"
    final_text = final_message(tid)
    outcome_ok, detail = eval_expectation(task, final_text,
                                          side_ctx["token"], side_ctx["witness"])
    res = {
        "task": task["id"], "shape": task["shape"], "status": status,
        "thread_id": tid, "outcome": "PASS" if outcome_ok else "FAIL",
        "detail": detail,
        "metrics": metrics,
        "duration_s": round(time.time() - t0),
        "final_excerpt": (final_text or "")[:400],
    }
    if verbose:
        print("  [task] %s status=%s outcome=%s iterations=%s dup=%s comp=%s "
              "retrieval=%s" % (task["id"], status, res["outcome"],
                                metrics.get("iterations"),
                                metrics.get("duplicate_markers"),
                                metrics.get("compaction_events"),
                                metrics.get("retrieval_calls")), flush=True)
    return res


# ---------------------------------------------------------------------------
# Speed / resource probes
# ---------------------------------------------------------------------------
def probe_search_latency(n):
    out = {"search_messages": [], "search_wiki": []}
    for tool in ("search_messages", "search_wiki"):
        for _ in range(n):
            dt, raw = api_execute(tool, {"query": "omnidev", "limit": 3},
                                  timeout=30)
            if raw.startswith("HTTPERR") or "unknown tool" in raw.lower():
                return None  # tool not available
            out[tool].append(round(dt * 1000, 1))
    return out


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))
    return s[idx]


def probe_prompt_ms(thread_ids, n):
    """Time prompt_generate on sample completed threads (build ms probe)."""
    results = []
    for tid in (thread_ids or [])[:n]:
        args = {"profile_name": "omni", "platform": "mattermost",
                "thread_id": tid, "channel_id": CORE_CHANNEL,
                "tool_names": PROMPT_PROBE_TOOL_NAMES,
                "user_message": "corpus probe"}
        dt, raw = api_execute("prompt_generate", args, timeout=60)
        results.append({"thread_id": tid, "ms": round(dt * 1000, 1),
                        "chars": len(raw)})
    return results


def snapshot_resources():
    db = psql("SELECT pg_database_size('omniagent')")
    rel = psql("SELECT coalesce(pg_relation_size('messages'),0) || '|' ||"
               " coalesce(pg_relation_size('threads'),0) || '|' ||"
               " coalesce(pg_relation_size(to_regclass('public.summaries')),0)")
    parts = rel.split("|") if rel else []
    data_du = oc(OMNIAGENT_CONTAINER,
                 "du -sb /opt/omni-stack/data 2>/dev/null | cut -f1")
    spill_du = oc(OMNIAGENT_CONTAINER,
                  "du -sb /opt/omni-stack/data/spill 2>/dev/null | cut -f1")
    stats = sh("docker stats --no-stream --format '{{.Name}}|{{.MemUsage}}' "
               "%s %s" % (OMNIAGENT_CONTAINER, POSTGRES_CONTAINER))
    rss = {}
    for line in (stats.stdout or "").strip().splitlines():
        if "|" in line:
            name, mem = line.split("|", 1)
            rss[name.strip()] = mem.strip()
    return {
        "db_size_bytes": _num(db),
        "rel_messages_bytes": _num(parts[0]) if len(parts) > 0 else None,
        "rel_threads_bytes": _num(parts[1]) if len(parts) > 1 else None,
        "rel_summaries_bytes": _num(parts[2]) if len(parts) > 2 else None,
        "data_dir_bytes": _num(data_du.stdout.strip()) if data_du.returncode == 0 else None,
        "spill_bytes": _num(spill_du.stdout.strip()) if spill_du.returncode == 0 else None,
        "rss": rss,
    }

# ---------------------------------------------------------------------------
# Candidate (branch X) build and restore
# ---------------------------------------------------------------------------
COMPOSE_BASE = "/opt/workspace/omni-stack/docker-compose.yml"
COMPOSE_DEV = "/opt/workspace/omni-stack/docker-compose.dev.yml"
OMNIDEV_ENV = "/opt/workspace/omni-deployer/omnidev.env"
AGENT_REPO = "/opt/workspace/omniagent"


def git(repo, *args):
    r = sh("git -C %s %s" % (repo, " ".join(_q(a) for a in args)))
    return r


def _q(s):
    return "'" + s.replace("'", "'\\''") + "'"


def resolve_ref(ref):
    """Returns (sha, worktree_path or None). Resolves a branch/sha/tag."""
    git(AGENT_REPO, "fetch", "origin", "--quiet")
    r = git(AGENT_REPO, "rev-parse", "--verify", ref)
    if r.returncode == 0:
        sha = r.stdout.strip()
        return sha, None
    r = git(AGENT_REPO, "rev-parse", "--verify", "origin/" + ref)
    if r.returncode == 0:
        return r.stdout.strip(), None
    raise RuntimeError("cannot resolve ref %r in %s" % (ref, AGENT_REPO))


def build_and_swap(sha, worktree, run):
    """Build the omnidev omniagent image from the worktree at sha and
    recreate the omnidev omniagent container on it."""
    wt = worktree or _worktree_for(sha, run)
    override = os.path.join(run["outdir"], "compose.override.yml")
    with open(override, "w") as f:
        f.write("services:\n  omniagent:\n    build:\n"
                "      context: %s\n      dockerfile: Dockerfile.dev\n" % wt)
    print("  [candidate] building omniagent-dev from %s (%s)" % (wt, sha[:12]),
          flush=True)
    cmd = ("docker compose -f %s -f %s -f %s --env-file %s -p omnidev build "
           "omniagent" % (COMPOSE_BASE, COMPOSE_DEV, override, OMNIDEV_ENV))
    r = sh(cmd)
    if r.returncode != 0:
        raise RuntimeError("candidate build failed: " +
                           (r.stderr or r.stdout)[-800:])
    r = sh("docker compose -f %s -f %s -f %s --env-file %s -p omnidev "
           "up -d omniagent" % (COMPOSE_BASE, COMPOSE_DEV, override, OMNIDEV_ENV))
    if r.returncode != 0:
        raise RuntimeError("candidate up failed: " + (r.stderr or r.stdout)[-800:])
    _wait_healthy(240)
    # Verify the running binary matches the expected sha.
    rr = oc(OMNIAGENT_CONTAINER, "git -C /app rev-parse HEAD 2>/dev/null")
    got = rr.stdout.strip() if rr.returncode == 0 else "?"
    return {"sha": sha, "worktree": wt, "verified_in_app": got}


def _worktree_for(sha, run):
    wt = os.path.join(run["outdir"], "wt-" + sha[:12])
    if os.path.isdir(wt):
        return wt
    r = git(AGENT_REPO, "worktree", "add", "--detach", wt, sha)
    if r.returncode != 0:
        raise RuntimeError("worktree add failed: " + (r.stderr or r.stdout)[-300:])
    return wt


def remove_worktree(run):
    wt_dir = os.path.join(run["outdir"])
    r = sh("ls -d %s/wt-* 2>/dev/null" % wt_dir)
    for wt in (r.stdout or "").split():
        git(AGENT_REPO, "worktree", "remove", "--force", wt)
        sh("rm -rf " + wt)


def _wait_healthy(timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = sh("curl -sf " + API_BASE + "/health")
        if r.returncode == 0:
            return
        time.sleep(3)
    raise RuntimeError("omniagent did not become healthy after recreate")


def restore_baseline(run, base_sha):
    """Rebuild the baseline image from origin/main and recreate the
    omniagent container (used after a --candidate side b)."""
    wt = _worktree_for(base_sha, run)
    override = os.path.join(run["outdir"], "compose.override.restore.yml")
    with open(override, "w") as f:
        f.write("services:\n  omniagent:\n    build:\n"
                "      context: %s\n      dockerfile: Dockerfile.dev\n" % wt)
    print("  [restore] rebuilding baseline omniagent-dev from %s" % wt,
          flush=True)
    cmd = ("docker compose -f %s -f %s -f %s --env-file %s -p omnidev build "
           "omniagent" % (COMPOSE_BASE, COMPOSE_DEV, override, OMNIDEV_ENV))
    r = sh(cmd)
    if r.returncode != 0:
        raise RuntimeError("baseline rebuild failed: " +
                           (r.stderr or r.stdout)[-800:])
    r = sh("docker compose -f %s -f %s -f %s --env-file %s -p omnidev "
           "up -d omniagent" % (COMPOSE_BASE, COMPOSE_DEV, override, OMNIDEV_ENV))
    if r.returncode != 0:
        raise RuntimeError("baseline up failed: " + (r.stderr or r.stdout)[-800:])
    _wait_healthy(240)
    print("  [restore] baseline container back up")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def fmt_bytes(n):
    if n is None:
        return "n/a"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return "%.1f GiB" % n


def summarize_side(side):
    tasks = [t for t in side["tasks"] if t["status"] == "completed"]
    iter_vals = sorted(t["metrics"].get("iterations") or 0
                       for t in tasks if t.get("metrics"))
    dup_vals = [t["metrics"].get("duplicate_markers") or 0
                for t in tasks if t.get("metrics")]
    ok = sum(1 for t in tasks if t["outcome"] == "PASS")
    n = len(side["tasks"])
    med = statistics.median(iter_vals) if iter_vals else None
    p90 = None
    if iter_vals:
        idx = min(len(iter_vals) - 1, int(round(0.9 * (len(iter_vals) - 1))))
        p90 = sorted(iter_vals)[idx]
    return {
        "tasks": n,
        "completed": len(tasks),
        "pass": ok,
        "fail": n - ok,
        "iter_median": med,
        "iter_p90": p90,
        "dup_total": sum(dup_vals),
    }


def flush_progress(run, side_key, side):
    try:
        with open(os.path.join(run["outdir"], "progress.json"), "w") as f:
            json.dump({"side": side_key, "tasks": side["tasks"]}, f,
                      indent=1, default=str)
    except Exception:
        pass


def side_gate_table(side):
    lines = []
    lines.append("| task | shape | status | outcome | iters | dur_s | dup | comp | retr | grounded | in_tok | out_tok |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for t in side["tasks"]:
        m = t.get("metrics") or {}
        g = m.get("grounded_total")
        gtxt = ("%s/%s" % (m.get("grounded_hits"), g)) if g else "0/0"
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            t["task"], t["shape"], t["status"], t["outcome"],
            m.get("iterations"), t["duration_s"],
            m.get("duplicate_markers"), m.get("compaction_events"),
            m.get("retrieval_calls"), gtxt,
            m.get("input_tokens"), m.get("output_tokens")))
    return "\n".join(lines)


def comparison_table(a, b, order):
    lines = []
    lines.append("| task | A status | B status | A iters | B iters | A outcome | B outcome | A dup | B dup | A comp | B comp |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for tid in order:
        ta = a["by_id"].get(tid)
        tb = b["by_id"].get(tid)
        if not ta or not tb:
            continue
        ma = ta.get("metrics") or {}
        mb = tb.get("metrics") or {}
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            tid, ta["status"], tb["status"],
            ma.get("iterations"), mb.get("iterations"),
            ta["outcome"], tb["outcome"],
            ma.get("duplicate_markers"), mb.get("duplicate_markers"),
            ma.get("compaction_events"), mb.get("compaction_events")))
    return "\n".join(lines)


def write_report(run, sides, order, outpath):
    sa, sb = sides["a"], sides["b"]
    a_sum, b_sum = summarize_side(sa), summarize_side(sb)
    lines = []
    lines.append("# Canonical A/B corpus run: gate comparison")
    lines.append("")
    lines.append("- run id: `%s`" % run["run_id"])
    lines.append("- timestamp: %s" % run["ts"])
    lines.append("- side a label: `%s` (binary sha %s)" %
                 (sa["label"], sa.get("binary_sha", "?")))
    lines.append("- side b label: `%s` (binary sha %s)" %
                 (sb["label"], sb.get("binary_sha", "?")))
    lines.append("- candidate ref: %s" % (run.get("candidate") or "(identity)"))
    lines.append("- outdir: `%s`" % run["outdir"])
    lines.append("- toolset ensure: %s" %
                 json.dumps(sa.get("toolset", {}).get("registered", {})))
    lines.append("")
    lines.append("## Helpfulness (T-1): side a")
    lines.append("")
    lines.append(side_gate_table(sa))
    lines.append("")
    lines.append("## Helpfulness (T-1): side b")
    lines.append("")
    lines.append(side_gate_table(sb))
    lines.append("")
    lines.append("## Comparison (A vs B)")
    lines.append("")
    lines.append(comparison_table(sa, sb, order))
    lines.append("")
    lines.append("| gate | side a | side b |")
    lines.append("|---|---|---|")
    lines.append("| tasks completed | %s/%s | %s/%s |" %
                 (a_sum["completed"], a_sum["tasks"], b_sum["completed"], b_sum["tasks"]))
    lines.append("| tasks pass | %s/%s | %s/%s |" %
                 (a_sum["pass"], a_sum["tasks"], b_sum["pass"], b_sum["tasks"]))
    lines.append("| iterations median | %s | %s |" % (a_sum["iter_median"], b_sum["iter_median"]))
    lines.append("| iterations p90 | %s | %s |" % (a_sum["iter_p90"], b_sum["iter_p90"]))
    lines.append("| duplicate markers total | %s | %s |" % (a_sum["dup_total"], b_sum["dup_total"]))
    lines.append("")
    lines.append("## Speed (T-2) probes")
    lines.append("")
    lines.append("| probe | side a | side b |")
    lines.append("|---|---|---|")
    for probe in ("search_messages_p50_ms", "search_messages_p95_ms",
                  "search_wiki_p50_ms", "search_wiki_p95_ms",
                  "prompt_build_ms_avg"):
        lines.append("| %s | %s | %s |" %
                     (probe, sa["probes"].get(probe), sb["probes"].get(probe)))
    lines.append("")
    lines.append("## Resources (T-3)")
    lines.append("")
    lines.append("| resource | A before | A after | B before | B after |")
    lines.append("|---|---|---|---|---|")
    keys = ("db_size_bytes", "rel_messages_bytes", "rel_threads_bytes",
            "rel_summaries_bytes", "data_dir_bytes", "spill_bytes")
    for k in keys:
        lines.append("| %s | %s | %s | %s | %s |" % (
            k, fmt_bytes(sa["res_before"].get(k)), fmt_bytes(sa["res_after"].get(k)),
            fmt_bytes(sb["res_before"].get(k)), fmt_bytes(sb["res_after"].get(k))))
    rss_a = sa["res_before"].get("rss") or {}
    rss_b = sb["res_before"].get("rss") or {}
    lines.append("| rss omniagent | %s | %s |" %
                 (rss_a.get(OMNIAGENT_CONTAINER), rss_b.get(OMNIAGENT_CONTAINER)))
    lines.append("| rss postgres | %s | %s |" %
                 (rss_a.get(POSTGRES_CONTAINER), rss_b.get(POSTGRES_CONTAINER)))
    lines.append("")
    if run.get("candidate"):
        lines.append("> Fixed in repo, ships in the next release; production untouched.")
    lines.append("")
    with open(outpath, "w") as f:
        f.write("\n".join(lines) + "\n")
    return lines

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Run the canonical A/B task corpus in omnidev and emit a "
                    "section 3T gate comparison table.")
    p.add_argument("--candidate", default=None,
                   help="git ref (branch/sha/tag) of /opt/workspace/omniagent to "
                        "build and run as side b; default: identity A/B on the "
                        "currently deployed omnidev binary")
    p.add_argument("--tasks", default=None,
                   help="comma list of task ids to run (default: all)")
    p.add_argument("--shapes", default=None,
                   help="comma list of shapes to run (default: all)")
    p.add_argument("--skip-long", action="store_true",
                   help="exclude the long >150-iteration task (t06)")
    p.add_argument("--no-toolset", action="store_true",
                   help="do not ensure/enable the standard tool plugins")
    p.add_argument("--no-restore-toolset", action="store_true",
                   help="leave the toolset plugins.yml in place at the end")
    p.add_argument("--probes", type=int, default=7,
                   help="search latency probe count per side (default 7)")
    p.add_argument("--outdir", default=None,
                   help="output directory (default /opt/workspace/tmp/"
                        "corpus-ab/<timestamp>)")
    p.add_argument("--timeout-scale", type=float, default=1.0,
                   help="multiply per-task timeouts by this factor")
    p.add_argument("--list", action="store_true",
                   help="validate the corpus and list tasks, then exit")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def select_tasks(tasks, args):
    if args.skip_long:
        tasks = [t for t in tasks if t["id"] != "t06-long-knowledge-index"]
    if args.tasks:
        want = set(x.strip() for x in args.tasks.split(",") if x.strip())
        tasks = [t for t in tasks
                 if any(t["id"] == w or t["id"].startswith(w) for w in want)]
    if args.shapes:
        want = set(x.strip() for x in args.shapes.split(",") if x.strip())
        tasks = [t for t in tasks if t["shape"] in want]
    if not tasks:
        raise SystemExit("no tasks selected")
    # fresh-thread follow-up pair must run write side (t07) before recall (t08)
    tasks.sort(key=lambda t: t["id"])
    return tasks


def rnd6():
    return os.urandom(3).hex()


def rnd4():
    return os.urandom(2).hex()


def main(argv):
    args = parse_args(argv)
    tasks = load_corpus()
    validate_corpus(tasks)
    if args.list:
        print("%-28s %-32s %-22s %s" % ("id", "shape", "timeout_min", "title"))
        for t in tasks:
            print("%-28s %-32s %-22s %s" %
                  (t["id"], t["shape"], t["timeout_min"], t["title"]))
        print("corpus OK: %d tasks, ids/shapes/expected validated" % len(tasks))
        return 0
    tasks = select_tasks(tasks, args)
    order = [t["id"] for t in tasks]

    ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    run_id = "corpus-" + ts
    outdir = args.outdir or os.path.join(DEFAULT_OUTROOT, ts)
    os.makedirs(outdir, exist_ok=True)
    run = {"run_id": run_id, "ts": ts, "outdir": outdir,
           "candidate": args.candidate, "no_toolset": args.no_toolset,
           "toolset_backup": None}

    # Baseline binary identity (side a).
    base_r = oc(OMNIAGENT_CONTAINER, "git -C /app rev-parse HEAD 2>/dev/null")
    base_sha = base_r.stdout.strip() if base_r.returncode == 0 else "?"
    img_r = sh("docker inspect -f '{{.Image}}' %s" % OMNIAGENT_CONTAINER)
    print("== corpus runner ==")
    print("run_id   :", run_id)
    print("tasks    :", ", ".join(order))
    print("baseline :", base_sha, "image", (img_r.stdout or "?").strip()[:20])

    mm_token = mm_login()
    team_id = mm_get("/api/v4/users/me/teams", mm_token)[0]["id"]
    mm_channel_id = mm_find_channel(mm_token, team_id, "dev-channel")
    if not mm_channel_id:
        raise SystemExit("MM channel dev-channel not found")
    # make sure testuser is a member of the channel (best effort)
    try:
        mm_post("/api/v4/channels/%s/members" % mm_channel_id,
                {"user_id": mm_get("/api/v4/users/me", mm_token)["id"]}, mm_token)
    except Exception:
        pass
    print("mm channel: dev-channel (%s)" % mm_channel_id)

    # Optional candidate build for side b (done up-front so side a is never
    # disturbed by the build; side a always runs the deployed baseline).
    cand_info = None
    cand_sha = None
    if args.candidate:
        cand_sha, _ = resolve_ref(args.candidate)
        print("candidate :", args.candidate, cand_sha[:12])

    # --- side a (baseline) ---
    sa_tag = rnd6()
    sa_token = "s3-a-" + sa_tag
    sa_wit = "w7-a-" + sa_tag + "-" + rnd4()
    sa_wprefix = "w7-a-" + sa_tag + "-"
    sa = {"key": "a", "label": "main", "binary_sha": base_sha,
          "token": sa_token, "witness": sa_wit, "wprefix": sa_wprefix,
          "prompts": {}, "tasks": [], "by_id": {},
          "timeout_scale": args.timeout_scale}
    for t in tasks:
        sa["prompts"][t["id"]] = fill_prompt(t, "a", run, sa_token, sa_wit,
                                             sa_wprefix)
    ts_a = ensure_toolset(run)
    sa["toolset"] = ts_a
    sa["res_before"] = snapshot_resources()
    print("\n== side a (%s) ==" % sa["label"])
    print("token=%s witness=%s" % (sa_token, sa_wit))
    for t in tasks:
        res = run_task(mm_token, mm_channel_id, t, sa, args.verbose)
        sa["tasks"].append(res)
        sa["by_id"][t["id"]] = res
        flush_progress(run, "a", sa)
    sa["res_after"] = snapshot_resources()
    sa["probes"] = {}
    sp = probe_search_latency(args.probes)
    if sp:
        sa["probes"]["search_messages_p50_ms"] = pct(sp["search_messages"], 50)
        sa["probes"]["search_messages_p95_ms"] = pct(sp["search_messages"], 95)
        sa["probes"]["search_wiki_p50_ms"] = pct(sp["search_wiki"], 50)
        sa["probes"]["search_wiki_p95_ms"] = pct(sp["search_wiki"], 95)
    done_ids = [t["thread_id"] for t in sa["tasks"] if t.get("thread_id")]
    pm = probe_prompt_ms(done_ids, 2)
    sa["probes"]["prompt_build_ms_avg"] = (
        round(statistics.mean([x["ms"] for x in pm]), 1) if pm else None)
    sa["probes"]["prompt_build_samples"] = pm

    # --- side b ---
    if args.candidate:
        print("\n== building candidate for side b ==")
        cand_info = build_and_swap(cand_sha, None, run)
    sb_tag = rnd6()
    sb_token = "s3-b-" + sb_tag
    sb_wit = "w7-b-" + sb_tag + "-" + rnd4()
    sb_wprefix = "w7-b-" + sb_tag + "-"
    b_label = args.candidate or "main-identity"
    sb = {"key": "b", "label": b_label,
          "binary_sha": cand_info["verified_in_app"] if cand_info else base_sha,
          "token": sb_token, "witness": sb_wit, "wprefix": sb_wprefix,
          "prompts": {}, "tasks": [], "by_id": {},
          "timeout_scale": args.timeout_scale}
    for t in tasks:
        sb["prompts"][t["id"]] = fill_prompt(t, "b", run, sb_token, sb_wit,
                                             sb_wprefix)
    ts_b = ensure_toolset(run)
    sb["toolset"] = ts_b
    sb["res_before"] = snapshot_resources()
    print("\n== side b (%s) ==" % b_label)
    print("token=%s witness=%s" % (sb_token, sb_wit))
    for t in tasks:
        res = run_task(mm_token, mm_channel_id, t, sb, args.verbose)
        sb["tasks"].append(res)
        sb["by_id"][t["id"]] = res
        flush_progress(run, "b", sb)
    sb["res_after"] = snapshot_resources()
    sb["probes"] = {}
    sp = probe_search_latency(args.probes)
    if sp:
        sb["probes"]["search_messages_p50_ms"] = pct(sp["search_messages"], 50)
        sb["probes"]["search_messages_p95_ms"] = pct(sp["search_messages"], 95)
        sb["probes"]["search_wiki_p50_ms"] = pct(sp["search_wiki"], 50)
        sb["probes"]["search_wiki_p95_ms"] = pct(sp["search_wiki"], 95)
    done_ids = [t["thread_id"] for t in sb["tasks"] if t.get("thread_id")]
    pm = probe_prompt_ms(done_ids, 2)
    sb["probes"]["prompt_build_ms_avg"] = (
        round(statistics.mean([x["ms"] for x in pm]), 1) if pm else None)
    sb["probes"]["prompt_build_samples"] = pm

    # --- teardown ---
    if args.candidate:
        print("\n== restoring baseline ==")
        restore_baseline(run, base_sha)
        remove_worktree(run)
    if not args.no_restore_toolset:
        restore_toolset(run)

    # --- report ---
    report_lines = write_report(run, {"a": sa, "b": sb}, order,
                                os.path.join(outdir, "report.md"))
    with open(os.path.join(outdir, "results.json"), "w") as f:
        json.dump({"meta": {"run_id": run_id, "ts": ts,
                            "candidate": args.candidate,
                            "outdir": outdir,
                            "tasks": order},
                   "side_a": sa, "side_b": sb}, f, indent=1, default=str)
    print("\n" + "\n".join(report_lines))
    print("\n== report written to %s/report.md ==" % outdir)

    # --- exit code ---
    a_fail = [t for t in sa["tasks"] if t["status"] != "completed"
              or t["outcome"] != "PASS"]
    b_fail = [t for t in sb["tasks"] if t["status"] != "completed"
              or t["outcome"] != "PASS"]
    code = 0
    if a_fail or b_fail:
        print("outcome mismatches or non-completed tasks:")
        for t in a_fail:
            print("  side a %s status=%s outcome=%s" %
                  (t["task"], t["status"], t["outcome"]))
        for t in b_fail:
            print("  side b %s status=%s outcome=%s" %
                  (t["task"], t["status"], t["outcome"]))
        code = 2
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
