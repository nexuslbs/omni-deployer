#!/usr/bin/env python3
"""Self-test for scripts/efficiency-metrics.py (role calibration + REVISION 2).

The 2026-10-03 daily report flagged 19 threads as 'edits > 0 and commits == 0'.
Attribution (thread 3995) proved that class was a METRIC ARTIFACT, not lost work:
12 of the threads wrote only /opt/workspace/tester-report.md (outside every git
repo) and the rest only scratch probes / reverted config probes. The rule was
first calibrated to delivery threads, then REMOVED outright (operator directive
2026-10-07, thread 4256: 'not very helpful and have lots of false positives').

This self-test pins the behaviour that revision introduced:

  * role classification - `delivery` (kanban task, non-verification step) /
    `verification` (workflow step testing|review) / `adhoc` (no kanban task) is
    derived from the thread row ALONE (no tool-name heuristics);
  * `edits > 0 and commits == 0` is gone: it is never a WATCH, never a BREACH
    and never touches the exit code (the counters stay visible);
  * skipped threads (status `skipped` / `merged`) and threads whose kanban task
    is `skipped` / `cancelled` are excluded from the metric entirely - a skip is
    normally not the agent's fault;
  * the remaining DELIVERY rule ('tok/state-op > 100k') gates DELIVERY threads
    and is merely WATCHed (exit-code free) for the others;
  * `duplicate_calls > 0` still gates EVERY thread class (no exemption);
  * `st = st_w + st_x`, so splitting the counter does not shift historical
    tok/state-op and no read-only classification is reintroduced;
  * the JSON exposes thread_class / thread_status / task_status / state_writes /
    state_execs / tokens_per_write_op.

The DB is stubbed (the module-level `psql` helper is monkeypatched), so the test
needs no container, no stack and no database - it exercises the real report/gate
logic end to end.

Run from the repo root:  python3 scripts/test_efficiency_metrics.py
Exit code 0 = all cases pass, 1 = at least one case failed.
"""
import contextlib
import importlib.util
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "efficiency-metrics.py")


def _load():
    if not os.path.isfile(SCRIPT):
        sys.exit("efficiency-metrics.py not found next to this test")
    spec = importlib.util.spec_from_file_location("efficiency_metrics", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()

# The row shape main() unpacks from the SQL result (all psql fields are text).
# (tid, tools, st_w, st_x, edits, dedup, commits, ptok, ctok, started, wall,
#  ttc, step, task)


def row(tid, st_w=0, st_x=0, edits=0, dedup=0, commits=0, ptok=0, ctok=0,
        step="running", task="task_x", wall=1.0, ttc="",
        thread_status="completed", task_status="done"):
    return [str(tid), str(st_w + st_x + edits), str(st_w), str(st_x),
            str(edits), str(dedup), str(commits), str(ptok), str(ctok),
            "10-03 00:00", str(wall), str(ttc), step, task,
            thread_status, task_status]


def run(rows, argv=None, expect_edit=False):
    """Execute main() against a stubbed DB; return (exit_code, stdout)."""
    old_psql, old_argv, old_expect = MOD.psql, sys.argv, MOD.EXPECT_EDIT
    MOD.psql = lambda sql: rows
    MOD.EXPECT_EDIT = expect_edit
    sys.argv = ["efficiency-metrics.py"] + list(argv or [])
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = MOD.main()
    finally:
        MOD.psql, sys.argv, MOD.EXPECT_EDIT = old_psql, old_argv, old_expect
    return code, buf.getvalue()


CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@case("thread_class: delivery / verification / adhoc from the thread row")
def t_class():
    assert MOD.thread_class("running", "task_x") == "delivery"
    assert MOD.thread_class("", "task_x") == "delivery"
    assert MOD.thread_class(None, "task_x") == "delivery"
    assert MOD.thread_class("testing", "task_x") == "verification"
    assert MOD.thread_class("review", "task_x") == "verification"
    assert MOD.thread_class("TESTING", "task_x") == "verification"
    assert MOD.thread_class("running", None) == "adhoc"
    assert MOD.thread_class("running", "") == "adhoc"
    assert MOD.thread_class("testing", "  ") == "adhoc"


@case("edits>0 & commits==0 is NOT a rule any more (delivery -> OK, exit 0)")
def t_delivery_edits_removed():
    code, out = run([row(1, st_w=1, edits=2, commits=0, step="running", task="task_x")])
    assert code == 0, out
    assert "| 1 | delivery |" in out, out
    assert "edits without commit" not in out, out
    assert "WATCH" not in out and "BREACH" not in out, out
    assert "| OK |" in out, out


@case("edits>0 & commits==0 on a verification thread -> OK too, exit 0")
def t_verification_edits_removed():
    code, out = run([row(2, st_w=1, edits=2, commits=0, step="testing", task="task_x")])
    assert code == 0, out
    assert "| 2 | verification |" in out and "| OK |" in out, out
    assert "WATCH" not in out and "BREACH" not in out, out


@case("the script carries no 'edits without commit' rule any more")
def t_rule_gone():
    with open(SCRIPT, encoding="utf-8") as fh:
        src = fh.read()
    assert "if edits > 0 and commits == 0:" not in src, "rule line still present"
    assert 'note("edits without commit")' not in src, "note() call still present"
    assert "SKIP_THREAD_STATUS" in src and "SKIP_TASK_STATUS" in src, src[:0]


@case("skipped / merged threads are excluded from the metric entirely")
def t_skipped_threads_excluded():
    rows = [row(60, st_w=1, edits=2, commits=0, thread_status="skipped"),
            row(61, st_w=1, edits=2, commits=0, thread_status="merged"),
            row(62, st_w=1, edits=2, commits=0, thread_status="completed")]
    code, out = run(rows)
    assert code == 0, out
    assert "| 60 |" not in out and "| 61 |" not in out, out
    assert "| 62 | delivery |" in out, out


@case("a skipped / cancelled kanban task is excluded too")
def t_skipped_task_excluded():
    rows = [row(70, st_w=1, edits=1, commits=1, task="task_a", task_status="skipped"),
            row(71, st_w=1, edits=1, commits=1, task="task_b", task_status="cancelled"),
            row(72, st_w=1, edits=1, commits=1, task="task_c", task_status="done")]
    code, out = run(rows)
    assert code == 0, out
    assert "| 70 |" not in out and "| 71 |" not in out, out
    assert "| 72 | delivery |" in out, out


@case("skipped() helper matches the exclusion contract")
def t_skipped_helper():
    assert MOD.skipped("skipped", "done") and MOD.skipped("MERGED", None)
    assert MOD.skipped(None, "skipped") and MOD.skipped("completed", "Cancelled")
    assert not MOD.skipped("completed", "done")
    assert not MOD.skipped("", "") and not MOD.skipped(None, None)
    assert "skipped" in MOD.SKIP_THREAD_STATUS and "merged" in MOD.SKIP_THREAD_STATUS
    assert "cancelled" in MOD.SKIP_TASK_STATUS


@case("verification (review) + tok/state-op>100k -> WATCH only, exit 0")
def t_review_tokst():
    code, out = run([row(3, st_w=1, ptok=500000, step="review", task="task_x")])
    assert code == 0, out
    assert "thread 3 [verification]" in out and "tok/state-op" in out, out
    assert "BREACH" not in out, out


@case("adhoc + tok/state-op>100k -> WATCH only, exit 0")
def t_adhoc_tokst():
    code, out = run([row(4, st_w=1, ptok=500000, step="", task="")])
    assert code == 0, out
    assert "thread 4 [adhoc]" in out and "tok/state-op" in out, out
    assert "BREACH" not in out, out


@case("DELIVERY + tok/state-op>100k -> BREACH, exit 1")
def t_delivery_tokst():
    code, out = run([row(5, st_w=1, ptok=500000, step="running", task="task_x")])
    assert code == 1, out
    assert "thread 5 [delivery]" in out and "tok/state-op" in out, out


@case("duplicate_calls>0 gates EVERY thread class")
def t_dedup_all_classes():
    rows = [row(10, st_w=1, dedup=1, step="running", task="task_x"),
            row(11, st_w=1, dedup=1, step="testing", task="task_x"),
            row(12, st_w=1, dedup=1, step="", task="")]
    code, out = run(rows)
    assert code == 1, out
    for tid in (10, 11, 12):
        assert "thread %d [" % tid in out, out
    breach_lines = [ln for ln in out.splitlines() if ln.startswith("BREACH ")]
    assert len(breach_lines) == 3, out


@case("st = st_w + st_x (exec-class counted -> historical shift impossible)")
def t_state_op_is_the_sum():
    # 1 write + 9 exec-class ops, 500k tokens -> tok/st = 50k < 100k -> OK.
    # If the split dropped exec-class (st == st_w == 1) it would be 500k -> breach.
    code, out = run([row(20, st_w=1, st_x=9, ptok=500000, task="task_x")])
    assert code == 0, out
    assert "| OK |" in out, out


@case("write-class and exec-class are disjoint and non-empty")
def t_class_split_disjoint():
    assert set(MOD.WRITE_CLASS) & set(MOD.EXEC_CLASS) == set()
    assert "filesystem__write" in MOD.WRITE_CLASS
    assert "docker__compose" in MOD.EXEC_CLASS and "ssh__run" in MOD.EXEC_CLASS


@case("JSON exposes the calibrated per-thread fields")
def t_json_fields():
    code, out = run([row(30, st_w=2, st_x=3, edits=1, commits=1, task="task_x")],
                    argv=["--json"])
    assert code == 0, out
    m = json.loads(out)[0]
    assert m["thread_class"] == "delivery", m
    assert m["thread_status"] == "completed" and m["task_status"] == "done", m
    assert m["state_writes"] == 2 and m["state_execs"] == 3, m
    assert m["state_changing"] == 5, m
    assert "tokens_per_write_op" in m, m


@case("no zero-division when a delivery thread has no write op")
def t_no_zero_division():
    code, out = run([row(40, st_w=0, st_x=4, ptok=10, task="task_x")], argv=["--json"])
    assert code == 0, out
    assert json.loads(out)[0]["tokens_per_write_op"] is None


@case("the delivery ranking excludes verification/adhoc threads")
def t_delivery_ranking_only():
    rows = [row(50, st_w=1, ptok=500000, step="", task=""),          # adhoc, huge
            row(51, st_w=1, edits=1, commits=1, ptok=1000, task="task_x")]
    code, out = run(rows)
    assert code == 0, out
    assert "worst tok/state-op among DELIVERY threads" in out, out
    ranking = out.split("worst tok/state-op among DELIVERY threads")[1]
    assert "#51 " in ranking and "#50 " not in ranking, ranking


def main():
    failures = []
    for name, fn in CASES:
        try:
            fn()
            print("PASS  %s" % name)
        except AssertionError as exc:
            failures.append(name)
            print("FAIL  %s\n      %s" % (name, str(exc).replace("\n", " | ")[:500]))
        except Exception as exc:  # noqa: BLE001 - report, do not mask
            failures.append(name)
            print("FAIL  %s (error) %r" % (name, exc))
    print("efficiency-metrics self-test: %d case(s) checked, %d failure(s)"
          % (len(CASES), len(failures)))
    for f in failures:
        print("  - %s" % f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
