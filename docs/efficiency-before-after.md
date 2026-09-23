# Efficiency before / after — self-repetition loop class (incident 2874)

Canonical shape: **"reapply the browser-free image"** (telegram thread 2874) — a ~3-file
change (delete ~50 Dockerfile lines + compose edits) that was expected to finish in minutes.

## BEFORE (measured on the production DB, thread 2874)

| metric | value |
|---|---|
| wall time | **87.2 min** |
| LLM calls | **247** (1444 messages) |
| prompt tokens | **14,726,047** |
| completion tokens | **1,032,259** |
| prompt tokens per state-changing op | **321,598** |
| time to first commit | **56.5 min** |
| calls per edit | **~55** (378 `filesystem__read`, 336 `git__run_command`, 140 `filesystem__grep` vs 3 edits) |
| worst repetitions | `docker-compose.yml` offset 262 **20×**, `docker-compose.dev.yml` offset 127 **14×**, `config/workstation.dev.yml` **11×**, `Dockerfile` **9×**, `git diff --stat HEAD` **6×**, `git status` **5× on an unchanged repo** |
| termination | provider **402 Insufficient Balance** — the loop burned the balance |
| operator stop signal | "why are you taking so long" merged as a sub_cause at 11:24:33 → **35 more minutes**, ~7.1M more prompt tokens, 88 more reads, 157 more git commands |

Why both previously-DONE guards missed it:

* `task_omnidev_agent_anti_repetition_compaction` shipped an **exact raw-args hash over
  a descriptor-declared read-only set**: a different offset/limit (every one of the 20
  `docker-compose.yml` reads paged differently) hashed differently, and the declared set
  could never be right for `docker`/`ssh`/`git` (their commands may or may not read).
* `task_omnidev_fix_running_thread_silently_ignores_a` merged the operator message into
  the live thread as **context only** — the running iteration kept going.

## AFTER (this change, verified on the dev stack with the noop provider)

Core: `omniagent@dfd11fa` — `src/agent/efficiency.rs` (`CallLedger`),
`src/agent/main_loop.rs`, `src/error.rs`, `src/llm/mod.rs`. Read-only verification was
**removed** (operator correction C1); the guard is generic: **opaque tool id + canonical
arguments** (JSON keys sorted recursively, reserved core key stripped).

### What the guard does to the 2874 pattern

| gate | scenario | observed result |
|---|---|---|
| A | the SAME invocation issued 4× in sequence (the `docker-compose.yml` 20× shape) | 1 executed, **3 payload-free `[duplicate call ...]` stubs**, `duplicate_calls=3` — 75 % of the replays eliminated, with **zero bytes re-injected** |
| A2 | the same invocation, then a state change (note write), then the same invocation again | the third call **EXECUTED normally** — invalidation works, genuine repeats survive |
| B | operator reply merged into a LIVE scripted thread | `=== LIVE INTERRUPT ===` raised on the running thread (`LIVE INTERRUPT for thread ...` in the engine log), sub_cause row present |
| C | unit suite | 10 passed (`CallLedger` genericity with the invented id `zorp__frobnicate`, canonicalisation, invalidation, `force_repeat`, structured 401/402/403 fast-fail) |
| C3 | genericity | `efficiency-genericity-gate.py`: 478 added production lines in the core diff, **0** plugin/tool-name literal (134 added lines inside `#[cfg(test)]` modules ignored and reported) |
| static | `efficiency-gate.sh` (SKIP_TESTS=1) | 28 passed, 0 failed |
| prompt | `GET /prompt/main` on the live build | contains the **EFFICIENCY CONTRACT** and the `[duplicate call ...]` wording; contains **no** `read:write` accounting |

Engine log from the live run (`[efficiency] thread=743 ...`):

```
[efficiency] thread=743 executed_calls=2 duplicate_calls=0 tracked_invocations=2 invalidations=0
[efficiency] duplicate invocation blocked (thread 743): [duplicate call - `filesystem__read` with these exact arguments was already ...
[efficiency] thread=743 executed_calls=2 duplicate_calls=1 tracked_invocations=2 invalidations=0
```

### Expected effect on the 2874 shape

The 20 identical `docker-compose.yml` reads collapse to 1 executed + 19 stubs in the
first minutes (instead of 20 full payloads), and the operator's "why so slow" now lands as
a directive on the running thread instead of 35 further minutes of silence. The remaining
server-side A/B (`run-corpus-ab.py` on the real task with a real provider) is the tester's
measurement gate; the numbers to report are `duplicate_calls`, total tokens and
time-to-first-commit from `scripts/efficiency-metrics.py`.

## How to reproduce every number above

```bash
# 1) unit + genericity + static assets (no LLM)
python3 omni-deployer/scripts/efficiency-genericity-gate.py /opt/workspace/omniagent origin/main~1
SKIP_TESTS=1 OMNIAGENT_REPO=/opt/workspace/omniagent OMNI_DIR=/opt/omni \
  bash omni-deployer/scripts/efficiency-gate.sh

# 2) E2E against the dev stack (noop provider, no LLM cost)
MM_TEAM=omni MM_CHANNEL=test-channel bash omni-deployer/scripts/efficiency-noop-gate.sh

# 3) per-thread counters
python3 omni-deployer/scripts/efficiency-metrics.py            # last 24h
THREADS=2874 python3 omni-deployer/scripts/efficiency-metrics.py
```
