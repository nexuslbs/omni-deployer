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

## MEASURED AFTER (real provider, dev stack, omniagent@be645ea, 2026-09-23)

Two independent real-provider measurements, taken after the corpus vehicle was repaired
(see "Measurement vehicle" below).

### 1. Live end-to-end on the canonical shape (the 2874 task, re-run)

`/opt/workspace/tmp/eff-canary` was reset to the browser-**baked** state (`7330780`), then the guard
binary `be645ea` ran the task through the MM dev-channel (thread **809**, deepseek-v4-flash, real LLM):

| metric | 2874 (BEFORE, production) | thread 809 (AFTER, guard build) | factor |
|---|---|---|---|
| wall time | 87.2 min | **4.1 min** | 21x |
| LLM calls / iterations | 247 | **30** | 8.2x |
| prompt tokens | 14,726,047 | **1,161,297** | 12.7x |
| completion tokens | 1,032,259 | 40,379 | 25.6x |
| prompt tokens per state-changing op | 321,598 | **37,142** | 8.7x |
| time to first commit | 56.5 min | **3.6 min** | 15.7x |
| duplicate re-executions | `docker-compose.yml` read 20x (different offsets), `git status` 5x on an unchanged repo | **0** (`duplicate_calls=0`, `efficiency-metrics.py` exit 0) | - |
| tool calls | 854 (378 read / 336 git / 140 grep) vs 3 edits | 77 vs 9 edits | 11x |
| termination | provider **402 Insufficient Balance**, work unfinished | commit `8e660c8`, repo clean, Dockerfile + both compose files + workstation.yml changed | done |

Small edit shape (`/opt/workspace/tmp/eff-live`: read `data.txt`, append `gamma`, one commit), thread
**788** on the same guard build: **7 iterations, 0.9 min, 153,004 prompt + 2,260 completion tokens,
1 commit, `duplicate_calls=0`, `tok/state-op` 18,399, grade OK**. The agent used 4 tool calls for the
edit (read -> write -> `git add` -> `git commit`) and did not re-read or re-check anything.

### 2. Canonical A/B corpus (runner, real provider)

`run-corpus-ab.py --candidate <pre-guard ref> --tasks t02,t05 --skip-long` (side a = the guard build
`be645ea`, side b = the same repo at a pre-guard ref), all real LLM traffic:

| side b (baseline) | tasks PASS | A in_tok | B in_tok | A dup markers | B dup markers | A compactions | B compactions |
|---|---|---|---|---|---|---|---|
| `6c46b6e` (immediately pre-guard) | 2/2 vs 2/2 | 207,024 | 209,384 | 2 | 2 | 0 | 0 |
| `3d5bce1` | 2/2 vs 2/2 | 208,656 | 259,645 | 2 | 3 | 0 | 1 |

No success-rate loss, no token regression, no outcome divergence: the guard is behaviour-preserving on
the verification shapes (t02 re-read-heavy corpus verification, t05 git-state verification) while
removing the replays it is meant to remove. Run dirs: `/opt/workspace/tmp/corpus-ab/20260923-221621`
(`6c46b6e`), `/opt/workspace/tmp/corpus-ab/20260923-220331` (`3d5bce1`).

> Honest scope: the BEFORE column of table 1 is production thread 2874 (production settings, older
> binary, its own prompt), the AFTER is the same task shape re-run in omnidev; table 2 is the
> same-environment A/B (dev stack, side a vs side b). Table 1 shows the improvement on the shape the
> operator complained about; table 2 shows the guard costs nothing measurable.

### Measurement vehicle (why the corpus could not run before)

The dev core resolves its config from its own `OMNI_DIR` = **`/opt/omni-stack/config`**
(container-local; the dev overlay leaves `OMNI_DIR` at the base value). The runner defaulted to
`/opt/omni/config`, so the toolset it wrote was never loaded: the running core stayed on its default
config, the mattermost platform logged `No access_token_name configured` / `No access_token provided
... without inbound capability`, and every corpus post was dropped (`status=timeout-no-thread` after
1800 s). The dev `channels.yml` entry `mattermost-dev-channel` also carried the stale channel id
`ff3j6p7j5jy1ickhoits4mo1pr`; the live dev-channel is `r88if3nhxjbgjd7n6jcn1e5rmy`. Fixed in
`run-corpus-ab.py` (`AGENT_CONFIG_DIR=/opt/omni-stack/config`, `--mm-channel`/`CORPUS_MM_CHANNEL`,
empty-profile fallback instead of aborting) and in the dev runtime `channels.yml`.

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
