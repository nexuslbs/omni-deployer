#!/usr/bin/env python3
"""Per-thread agent EFFICIENCY metrics (loop-regression observability).

Root-cause workstream for the re-read / no-progress / ignored-stop-signal loop
class (incident telegram thread 2874: 87 min, 977 tool results, 550 filesystem
reads, 8 edits, 14.7M prompt tokens; post-mortem thread 2907).

This is the *observability* half of the EFFICIENCY contract: the counters the
engine already maintains (duplicate-read stub rows, read-only vs state-changing
tool results) are aggregated per thread and printed, so a regression shows up
in the daily report / dashboard WITHOUT the operator having to complain.

Metrics per thread (last HOURS, default 24):
  tools      tool-result messages (executed or blocked)
  ro         read-only tool results (filesystem_read/list/info/search/grep,
             search_*, skills_*, notes_*, ssh_status, core_list_tool_details)
  st         state-changing tool results (write/str_replace/insert/apply_patch,
             git commit/push/sync, docker/shh run, memory/kanban/subtask writes)
  dup        duplicate-read stubs returned in place of a re-execution
             (msg_type=tool-result with metadata eff=duplicate-read, or the
             '[duplicate read - ...' stub text)
  r:w        read-only : state-changing ratio          (target < 5)
  tokens     prompt+completion tokens spent in the thread
  tok/st     tokens per state-changing op               (target < 100k)
  ttc        minutes from thread start to the first commit/push (time to first
             commit; a thread with edits but no commit is flagged)

Exit code is 1 when any thread breaks a threshold (dup > 0, r:w > 5,
edits > 0 and no commit, tok/st > 100k) so the script doubles as a GATE.

Usage (inside the toolbox container, or any host with docker access):
  python3 efficiency-metrics.py                       # last 24h, dev stack
  HOURS=6 python3 efficiency-metrics.py
  PG_CONTAINER=omni-stack-postgres-1 DB_NAME=omniagent python3 efficiency-metrics.py
  THREADS=2874,2920 python3 efficiency-metrics.py     # explicit thread ids
  python3 efficiency-metrics.py --json                # machine-readable
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

PG_CONTAINER = os.environ.get("PG_CONTAINER", "omnidev-postgres-1")
DB_USER = os.environ.get("DB_USER", "omniagent")
DB_NAME = os.environ.get("DB_NAME", "omniagent")
PG_USER_ENV = os.environ.get("PG_USER_ENV", "POSTGRES_USER")

READ_ONLY = (
    "filesystem__read",
    "filesystem__list",
    "filesystem__info",
    "filesystem__search",
    "filesystem__grep",
    "git__status",
    "search__messages",
    "search__thread_messages",
    "search__channels",
    "search__channel_prompts",
    "search__wiki",
    "search__metrics",
    "search__database",
    "skills__list_skills",
    "skills__view_skill",
    "notes__note_list",
    "notes__note_read",
    "ssh__status",
    "core__list_tool_details",
    "memory__list_memories",
    "memory__review_memories",
)

STATE_CHANGING = (
    "filesystem__write",
    "filesystem__str_replace",
    "filesystem__insert",
    "filesystem__apply_patch",
    "git__commit_and_push",
    "git__sync",
    "git__create_github_repo",
    "ssh__run",
    "ssh__copy",
    "docker__compose",
    "memory__promote_to_memory",
    "memory__manage_memory",
    "subtasks__manage_subtasks",
    "subtasks__add_subtask",
    "subtasks__update_subtask",
    "subtasks__delete_subtask",
    "kanban__",
    "workbench__tool",
)

STATE_SQL = ",".join("'%s'" % s for s in STATE_CHANGING if not s.endswith("__"))
STATE_LIKE = "msg_subtype LIKE 'kanban__%' OR msg_subtype LIKE 'subtasks__%'"

SQL = """
WITH t AS (
  SELECT
    thread_id,
    count(*) FILTER (WHERE msg_type = 'tool-result') AS tools,
    count(*) FILTER (WHERE msg_type = 'tool-result' AND msg_subtype IN ({ro})) AS ro,
    count(*) FILTER (WHERE msg_type = 'tool-result' AND msg_subtype = 'git__run_command') AS git,
    count(*) FILTER (WHERE msg_type = 'tool-result' AND (msg_subtype IN ({st}) OR {st_like})) AS st,
    count(*) FILTER (WHERE msg_type = 'tool-result' AND (
        msg_subtype = 'filesystem__write'
        OR msg_subtype IN ('filesystem__str_replace','filesystem__insert','filesystem__apply_patch'))) AS edits,
    count(*) FILTER (WHERE msg_type = 'tool-result' AND (
        metadata::text LIKE '%"eff":"duplicate-read"%'
        OR content LIKE '[duplicate read%'
        OR content LIKE '%no payload re-injected%')) AS dup,
    count(*) FILTER (WHERE msg_type = 'tool-result'
        AND (msg_subtype IN ('git__commit_and_push','git__sync')
             OR (msg_subtype = 'git__run_command'
                 AND content LIKE '%"command":"git commit%'))) AS commits,
    COALESCE(SUM(COALESCE(NULLIF(token_usage::text, '{{}}')::jsonb ->> 'prompt_tokens', '0')::bigint), 0) AS ptok,
    COALESCE(SUM(COALESCE(NULLIF(token_usage::text, '{{}}')::jsonb ->> 'completion_tokens', '0')::bigint), 0) AS ctok,
    min(created_at) AS t0,
    max(created_at) AS t1,
    min(created_at) FILTER (WHERE msg_type = 'tool-result'
        AND (msg_subtype IN ('git__commit_and_push','git__sync')
             OR (msg_subtype = 'git__run_command'
                 AND content LIKE '%"command":"git commit%'))) AS t_commit
  FROM messages
  WHERE {where}
  GROUP BY thread_id
)
SELECT thread_id, tools, ro, git, st, edits, dup, commits, ptok, ctok,
       to_char(t0, 'MM-DD HH24:MI') AS started,
       round(EXTRACT(EPOCH FROM (COALESCE(t1, now()) - t0)) / 60.0, 1) AS wall_min,
       round(EXTRACT(EPOCH FROM (t_commit - t0)) / 60.0, 1) AS ttc_min
FROM t
WHERE tools > 0
ORDER BY t1 DESC;
"""


def psql(sql: str) -> list[list[str]]:
    cmd = [
        "docker", "exec", PG_CONTAINER,
        "psql", "-U", DB_USER, "-d", DB_NAME, "-At", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        sys.stderr.write("psql failed (container=%s db=%s):\n%s\n" % (PG_CONTAINER, DB_NAME, out.stderr.strip()))
        sys.exit(2)
    return [ln.split("|") for ln in out.stdout.splitlines() if ln.strip()]


def main() -> int:
    args = sys.argv[1:]
    as_json = "--json" in args
    hours = float(os.environ.get("HOURS", "24"))
    threads = os.environ.get("THREADS", "").strip()
    if threads:
        where = "thread_id IN (%s)" % ",".join(t.strip() for t in threads.split(",") if t.strip())
    else:
        where = "created_at > now() - interval '%s hours'" % hours

    sql = SQL.format(
        ro=",".join("'%s'" % s for s in READ_ONLY),
        st=STATE_SQL,
        st_like=STATE_LIKE,
        where=where,
    )
    rows = psql(sql)

    report, failures = [], []
    for r in rows:
        (tid, tools, ro, git, st, edits, dup, commits, ptok, ctok, started, wall, ttc) = r
        tools, ro, git, st, edits, dup, commits = (int(x) for x in (tools, ro, git, st, edits, dup, commits))
        ptok, ctok = int(ptok), int(ctok)
        wall = float(wall)
        ttc = float(ttc) if ttc not in ("", None) else None
        ratio = (ro / st) if st else float(ro)
        toks = ptok + ctok
        per_state = (toks / st) if st else float(toks)
        grade = "OK"
        reasons = []
        if dup > 0:
            reasons.append("%d duplicate read(s)" % dup)
        if st and ratio > 5:
            reasons.append("r:w %.1f > 5" % ratio)
        if edits > 0 and commits == 0:
            reasons.append("edits without commit")
        if st and per_state > 100_000:
            reasons.append("tok/state-op %.0fk > 100k" % (per_state / 1000))
        if reasons:
            grade = "BREACH: " + "; ".join(reasons)
            failures.append("thread %s: %s" % (tid, grade))
        report.append({
            "thread_id": int(tid), "tools": tools, "read_only": ro, "git_commands": git, "state_changing": st,
            "edits": edits, "duplicate_reads": dup, "commits": commits,
            "read_write_ratio": round(ratio, 2),
            "prompt_tokens": ptok, "completion_tokens": ctok, "tokens": toks,
            "tokens_per_state_op": int(per_state),
            "started": started, "wall_minutes": wall,
            "time_to_first_commit_min": ttc, "grade": grade,
        })

    if as_json:
        print(json.dumps(report, indent=2))
    else:
        print("== agent efficiency metrics (%s, %s) ==" % (PG_CONTAINER, where))
        print("| thread | tools | ro | git | st | edits | dup | commits | r:w | tokens | tok/st-op | wall | ttc | grade |")
        print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for m in report:
            print("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %sm | %s | %s |" % (
                m["thread_id"], m["tools"], m["read_only"], m["git_commands"], m["state_changing"], m["edits"],
                m["duplicate_reads"], m["commits"], m["read_write_ratio"], m["tokens"],
                m["tokens_per_state_op"], m["wall_minutes"],
                "%.1f" % m["time_to_first_commit_min"] if m["time_to_first_commit_min"] is not None else "-",
                m["grade"]))
        n_dup = sum(m["duplicate_reads"] for m in report)
        print("\nthreads=%d duplicate_reads=%d breaches=%d" % (len(report), n_dup, len(failures)))
        for f in failures:
            print("BREACH " + f)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
