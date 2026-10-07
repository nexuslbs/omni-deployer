#!/usr/bin/env python3
"""Per-thread agent EFFICIENCY metrics (loop-regression observability).

Root-cause workstream for the SELF-REPETITION loop class (incident telegram
thread 2874: 87 min, 247 LLM calls, 14.7M prompt tokens for a 3-edit change;
post-mortem thread 2907). Operator correction (2026-09-23): the defect is
REPETITION and NON-ADVANCEMENT of calls, NOT read-only vs write. There is NO
read-only counter and NO read:write ratio here any more - the engine does not
classify a call as read-only, and neither does this report.

This is the *observability* half of the EFFICIENCY contract: the generic
duplicate-invocation ledger the engine maintains (`CallLedger`) surfaces a
payload-free duplicate-call stub as an agent message with
`metadata.eff = "duplicate-call"`, so a regression shows up in the daily report /
dashboard WITHOUT the operator having to complain.

ROLE CALIBRATION + 'edits without commit' REMOVAL (threads 3995 -> 4257).
The 2026-10-03 report flagged 19 threads with `edits > 0 and commits == 0`;
attribution (thread 3995) showed every one of them was a tester/reviewer report
or scratch artifact - ZERO lost repository work. The rule was first calibrated to
gate delivery threads only, then REMOVED outright by operator directive
(2026-10-07, thread 4256): "make sure that omnidev does not such verification in
core. That's not very helpful and have lots of false positives". The edits /
commits counters are still REPORTED (nothing hidden), but a thread with edits and
no commit is neither a WATCH nor a BREACH. What still gates: `duplicate_calls >
0` (every thread) and `tok/state-op > 100k` (BREACH on delivery threads, WATCH on
verification/adhoc).

SKIPPED WORK IS NOT MEASURED (same directive): "skipped tasks should not be
considered either, as the lack of commit may be due to the skip that is normally
not the agent fault". A thread whose status is `skipped` (or `merged`: folded
into another prompt by the sub-prompt merge) and a thread whose kanban task is
`skipped`/`cancelled` are excluded from the report entirely.

Metrics per thread (last HOURS, default 24):
  tools       tool-result messages (executed or answered with a stub)
  st          state-changing tool results, st = st_w + st_x
  st_w        write-class state ops: a durable change the report can point at
              (filesystem write-class, git commit/push/sync, memory/subtask/
              kanban writes)
  st_x        exec-class state ops: command execution (docker / ssh / workbench)
              whose durable effect cannot be derived from the tool result - it is
              counted, but never treated as a durable change (there is no
              read-only classification anywhere: an unknown command is simply not
              a write)
  dedup       invocations answered with a duplicate-call stub instead of being
              executed (metadata eff=duplicate-call, or the '[duplicate call'
              stub text) - target 0
  edits       filesystem write-class tool results
  commits     git commit/push/sync results
  tokens      prompt+completion tokens spent in the thread
  tok/st      tokens per state-changing op               (target < 100k)
  ttc         minutes from thread start to the first commit/push (time to first
              commit; '-' when the thread never committed)

Thread classes (from the thread row): `delivery` = a kanban task on an executor
step; `verification` = workflow step `testing` / `review`; `adhoc` = no kanban
task (operator / hook / cron thread).

Exit code is 1 when a DELIVERY thread breaks a threshold (tok/st > 100k), when
ANY thread re-issued a duplicate call (dedup > 0), or when EXPECT_EDIT marks a
thread as edit-shaped and it produced neither edit nor commit - so the script
doubles as a GATE. Over-threshold verification/ad-hoc threads are printed as
WATCH (informational) only. `edits > 0 and commits == 0` is NOT a rule any more
(removed 2026-10-07: false positives), and skipped/merged threads plus
skipped/cancelled kanban tasks are excluded from the metric entirely.

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

# EXPECT_EDIT=1 marks the measured thread(s) as EDIT-SHAPED: a task whose whole
# point was to change a file and land a commit. Without it, `edits == 0 &&
# commits == 0` cannot be graded (a pure Q&A thread is legitimately edit-free).
EXPECT_EDIT = (os.environ.get("EXPECT_EDIT") or os.environ.get("EXPECT") or "").strip().lower() in (
    "1", "true", "yes", "edit", "edit-shaped", "edit_shaped")

# The duplicate-call STUB carries this structured marker in the message metadata
# (never a plugin/tool-name list): it is the generic signal of a blocked replay.
DUP_PREDICATE = ("m.metadata::text LIKE '%\"eff\":\"duplicate-call\"%' "
                 "OR m.content LIKE '[duplicate call%'")

# write-class: the tool result IS the durable change (a file / repo / row).
WRITE_CLASS = (
    "filesystem__write",
    "filesystem__str_replace",
    "filesystem__insert",
    "filesystem__apply_patch",
    "git__commit_and_push",
    "git__sync",
    "git__create_github_repo",
    "memory__promote_to_memory",
    "memory__manage_memory",
)
# exec-class: the tool RAN something; the durable effect is not in its result.
EXEC_CLASS = (
    "ssh__run",
    "ssh__copy",
    "docker__compose",
    "workbench__tool",
)
WRITE_LIKE = ("m.msg_subtype LIKE 'subtasks__%' OR m.msg_subtype LIKE 'tasks__%' "
              "OR m.msg_subtype LIKE 'kanban__%'")

EDITS = (
    "filesystem__write",
    "filesystem__str_replace",
    "filesystem__insert",
    "filesystem__apply_patch",
)

COMMITS = ("m.msg_subtype IN ('git__commit_and_push','git__sync') "
           "OR (m.msg_subtype = 'git__run_command' "
           "AND m.content LIKE '%\"command\":\"git commit%')")

# A thread is `delivery` when it carries a kanban task and is NOT a verification
# step: those threads owe a commit. Verification and ad-hoc threads have no
# delivery contract, so their counter overruns are reported, not gated.
VERIFICATION_STEPS = ("testing", "review")

# Excluded from the metric entirely (operator directive 2026-10-07): a skipped
# thread owes nothing (the skip is normally not the agent's fault) and a merged
# thread was folded into another prompt by the sub-prompt merge. A kanban task in
# a skipped/cancelled state is likewise out of scope.
SKIP_THREAD_STATUS = ("skipped", "merged")
SKIP_TASK_STATUS = ("skipped", "cancelled")

SQL = """
WITH t AS (
  SELECT
    m.thread_id,
    count(*) FILTER (WHERE m.msg_type = 'tool-result') AS tools,
    count(*) FILTER (WHERE m.msg_type = 'tool-result' AND (m.msg_subtype IN ({st_w}) OR {st_like})) AS st_w,
    count(*) FILTER (WHERE m.msg_type = 'tool-result' AND m.msg_subtype IN ({st_x})) AS st_x,
    count(*) FILTER (WHERE m.msg_type = 'tool-result' AND m.msg_subtype IN ({edits})) AS edits,
    count(*) FILTER (WHERE m.msg_type = 'tool-result' AND ({dup})) AS dedup,
    count(*) FILTER (WHERE m.msg_type = 'tool-result' AND ({commit})) AS commits,
    COALESCE(SUM(COALESCE(NULLIF(m.token_usage::text, '{{}}')::jsonb ->> 'prompt_tokens', '0')::bigint), 0) AS ptok,
    COALESCE(SUM(COALESCE(NULLIF(m.token_usage::text, '{{}}')::jsonb ->> 'completion_tokens', '0')::bigint), 0) AS ctok,
    min(m.created_at) AS t0,
    max(m.created_at) AS t1,
    min(m.created_at) FILTER (WHERE m.msg_type = 'tool-result' AND ({commit})) AS t_commit
  FROM messages m
  WHERE {where}
  GROUP BY m.thread_id
)
SELECT t.thread_id, t.tools, t.st_w, t.st_x, t.edits, t.dedup, t.commits, t.ptok, t.ctok,
       to_char(t.t0, 'MM-DD HH24:MI') AS started,
       round(EXTRACT(EPOCH FROM (COALESCE(t.t1, now()) - t.t0)) / 60.0, 1) AS wall_min,
       round(EXTRACT(EPOCH FROM (t.t_commit - t.t0)) / 60.0, 1) AS ttc_min,
       th.workflow_step,
       th.task_id,
       th.status AS thread_status,
       kt.status AS task_status
FROM t
LEFT JOIN threads th ON th.id = t.thread_id
LEFT JOIN kanban_tasks kt ON kt.id = th.task_id
WHERE t.tools > 0
  AND COALESCE(lower(th.status), '') NOT IN ({skip_thread})
  AND COALESCE(lower(kt.status), '') NOT IN ({skip_task})
ORDER BY t.t1 DESC;
"""


def sql_list(names) -> str:
    return ",".join("'%s'" % n for n in names)


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


def skipped(thread_status, task_status) -> bool:
    """True when the metric must ignore the row entirely.

    A skipped/merged thread, or a thread whose kanban task is skipped/cancelled,
    is not the agent's deliverable: the absence of a commit there says nothing
    about work loss (operator directive 2026-10-07).
    """
    return ((thread_status or "").strip().lower() in SKIP_THREAD_STATUS
            or (task_status or "").strip().lower() in SKIP_TASK_STATUS)


def thread_class(workflow_step, task_id) -> str:
    """`delivery` = kanban task on a non-verification step; else verification/adhoc.

    The role comes from the thread row that the workflow already maintains - no
    tool-name heuristics, no message-text matching.
    """
    if not task_id or not str(task_id).strip():
        return "adhoc"
    if (workflow_step or "").strip().lower() in VERIFICATION_STEPS:
        return "verification"
    return "delivery"


def main() -> int:
    args = sys.argv[1:]
    as_json = "--json" in args
    hours = float(os.environ.get("HOURS", "24"))
    threads = os.environ.get("THREADS", "").strip()
    if threads:
        where = "m.thread_id IN (%s)" % ",".join(t.strip() for t in threads.split(",") if t.strip())
    else:
        where = "m.created_at > now() - interval '%s hours'" % hours

    sql = SQL.format(st_w=sql_list(WRITE_CLASS), st_x=sql_list(EXEC_CLASS),
                     st_like=WRITE_LIKE, edits=sql_list(EDITS), dup=DUP_PREDICATE,
                     commit=COMMITS, where=where,
                     skip_thread=sql_list(SKIP_THREAD_STATUS),
                     skip_task=sql_list(SKIP_TASK_STATUS))
    rows = psql(sql)

    report, failures, watch = [], [], []
    for r in rows:
        (tid, tools, st_w, st_x, edits, dedup, commits, ptok, ctok,
         started, wall, ttc, step, task, thread_status, task_status) = r
        if skipped(thread_status, task_status):
            continue
        tools, st_w, st_x, edits, dedup, commits = (int(x) for x in
                                                   (tools, st_w, st_x, edits, dedup, commits))
        ptok, ctok = int(ptok), int(ctok)
        wall = float(wall)
        ttc = float(ttc) if ttc not in ("", None) else None
        st = st_w + st_x
        toks = ptok + ctok
        cls = thread_class(step, task)
        gated = cls == "delivery"
        per_state = (toks / st) if st else float(toks)
        per_write = int(toks / st_w) if st_w else None

        reasons, watched = [], []

        def note(msg):
            (reasons if gated else watched).append(msg)

        if EXPECT_EDIT and edits == 0 and commits == 0:
            reasons.append("edit-shaped task with no edit and no commit (non-delivery)")
        if dedup > 0:
            reasons.append("%d duplicate call(s) re-issued with no state change" % dedup)
        if st and per_state > 100_000:
            note("tok/state-op %.0fk > 100k" % (per_state / 1000))

        if reasons:
            grade = "BREACH: " + "; ".join(reasons)
            failures.append("thread %s [%s]: %s" % (tid, cls, grade))
        elif watched:
            grade = "WATCH: " + "; ".join(watched)
            watch.append("thread %s [%s]: %s" % (tid, cls, grade))
        else:
            grade = "OK"

        report.append({
            "thread_id": int(tid), "thread_class": cls,
            "thread_status": (thread_status or None),
            "task_status": (task_status or None),
            "tools": tools, "state_changing": st,
            "state_writes": st_w, "state_execs": st_x,
            "edits": edits, "duplicate_calls": dedup, "commits": commits,
            "prompt_tokens": ptok, "completion_tokens": ctok, "tokens": toks,
            "tokens_per_state_op": int(per_state),
            "tokens_per_write_op": per_write,
            "started": started, "wall_minutes": wall,
            "time_to_first_commit_min": ttc, "grade": grade,
        })

    if as_json:
        print(json.dumps(report, indent=2))
    else:
        print("== agent efficiency metrics (%s, %s) ==" % (PG_CONTAINER, where))
        print("| thread | class | tools | st | st-w | st-x | edits | dup | commits | tokens | tok/st-op | wall | ttc | grade |")
        print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for m in report:
            print("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %sm | %s | %s |" % (
                m["thread_id"], m["thread_class"], m["tools"], m["state_changing"],
                m["state_writes"], m["state_execs"], m["edits"], m["duplicate_calls"],
                m["commits"], m["tokens"], m["tokens_per_state_op"], m["wall_minutes"],
                "%.1f" % m["time_to_first_commit_min"] if m["time_to_first_commit_min"] is not None else "-",
                m["grade"]))
        top = sorted((m for m in report if m["thread_class"] == "delivery"),
                     key=lambda x: -x["tokens_per_state_op"])[:5]
        if top:
            print("\nworst tok/state-op among DELIVERY threads (what the gate protects):")
            for m in top:
                print("  #%s %sk  st=%s (w=%s/x=%s) edits=%s commits=%s %s" % (
                    m["thread_id"], round(m["tokens_per_state_op"] / 1000),
                    m["state_changing"], m["state_writes"], m["state_execs"],
                    m["edits"], m["commits"], m["grade"].split(":")[0]))
        n_dup = sum(m["duplicate_calls"] for m in report)
        print("\nthreads=%d duplicate_calls=%d breaches=%d watch=%d" % (
            len(report), n_dup, len(failures), len(watch)))
        for f in failures:
            print("BREACH " + f)
        for w in watch:
            print("WATCH " + w)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
