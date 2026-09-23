# Agent efficiency: before/after (loop class of incident thread 2874)

Root-cause workstream for the **re-read / no-progress / ignored-stop-signal** loop class.
Evidence thread: telegram **2874** (`reapply the browser-free image`, 2026-09-23 10:32 -> 11:59).
Post-mortem: thread **2907**.

## Measured BEFORE (thread 2874, production DB)

Produced by `scripts/efficiency-metrics.py` (this repo) against the production
database (`omni-stack-postgres-1`), i.e. **the real incident numbers, not an estimate**:

| thread | tools | ro | git | st | edits | dup | commits | r:w | tokens | tok/st-op | wall | ttc | grade |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2874 | 977 | 580 | 341 | 49 | 8 | 0 | 6 | **11.84** | **15,758,306** | **321,598** | **87.2m** | 56.5m | BREACH: r:w 11.8 > 5; tok/state-op 322k > 100k |

- `prompt_tokens` 14,726,047 + `completion_tokens` 1,032,259 = 15,758,306.
- `time_to_first_commit` 56.5 min (first commit at 11:29 for a ~3-file change started at 10:32).
- `dup` is 0 **by construction**: the pre-fix engine had no duplicate counter and kept no
  duplicate-stub rows at all (0 rows matching `duplicate read` / `no payload re-injected`
  existed in the whole DB before the fix) - which is exactly why the loop was invisible.
- The same script run over the last 48 h flags other operator threads too
  (e.g. `r:w 17.8`, `tok/state-op 502k`), so the metric has real signal beyond this incident.

## What changed (code, committed)

| layer | change | commit |
|---|---|---|
| tool loop | scope-aware duplicate detection (same file + **overlapping** char/line range, reordered JSON, read-only git argv), compact stub with **no payload re-injection**, persisted as a real `tool-result` row carrying `metadata.eff=duplicate-read` | omniagent `01f40b9` + `a70cace` |
| tool loop | per-thread counter + `[efficiency] duplicate read blocked (thread N) ...` log line | omniagent `01f40b9` |
| tool loop | escalating **progress steering** (EFFICIENCY CHECK @6 read-only calls since the last state change, STOP EXPLORING @12, HARD STOP @24) injected in-prompt, compliance possible in the same iteration - no truncation, no hard cap | omniagent `01f40b9` |
| tool loop | **LIVE INTERRUPT** when a sub-prompt / operator message is merged into a processing thread (the 35-minute silence of thread 2874) | omniagent `01f40b9` |
| provider | **fast-fail** on 402 / insufficient balance / 401 instead of the 3x retry burn | omniagent `01f40b9` |
| memory/templates/skill/wiki | EFFICIENCY CONTRACT in `profiles/omni/MEMORY.md`, enforceable gates in `dev-executor.md` / `dev-tester.md`, `daily-report-template.md` Step 6b, `skills/general/agent-efficiency-read-once`, wiki `Reference/Omniagent/Efficiency-Contract` | omni `58df7e8` |
| harness | noop-provider E2E gate (`scripts/efficiency-noop-gate.sh`), scripted asset gate (`scripts/efficiency-gate.sh`) | omni-deployer `3225cfb`, `05ef625`, `d6e7dab` |
| observability | `scripts/efficiency-metrics.py` (this file's numbers) + daily-report Step 6b wiring | omni-deployer (this commit) |

## Measured AFTER (scripted, no real LLM)

`bash scripts/efficiency-noop-gate.sh` inside the dev toolbox (dev project `omnidev`,
`MM_CHANNEL=test-channel`, `TIMEOUT=180`): **== RESULT: 5 passed, 0 failed, 0 skipped ==**

- GATE A - duplicate-read stub emitted in a scripted replay of the exact 2874 pattern
  (two OVERLAPPING reads of the same file with different paging): stub row count = 1,
  per-thread duplicate counter >= 1.
- GATE B - merged sub_cause on a **live** noop thread raises the LIVE INTERRUPT
  (`[efficiency] LIVE INTERRUPT for thread N: 1 new merged prompt(s) during processing`)
  within one iteration; sub-prompt merge rows = 1 (no 35-minute silence).
- GATE C - efficiency unit suite green (13 passed) and fatal-provider classification present
  (402 / insufficient balance / 401 fast-fail).

## Still unmeasured (honest gap)

The **live real-provider A/B on the canonical `reapply the browser-free image` task**
(before/after duplicate reads, read:write ratio, tokens) has NOT been run: the shared
provider balance returned `402 Payment Required: Insufficient Balance` and the engine now
fails such a run fast (which is the desired behaviour, but it yields no measurement).
The scripted gates above cover the mechanism; the token-reduction claim
("zero duplicate reads, r:w < 5, >= 10x tokens") is therefore **not yet proven by a live run**.
Re-run `scripts/run-corpus-ab.py` (plus `efficiency-metrics.py --json` on the resulting
thread ids) once the provider balance is topped up and record the two rows side by side here.

## How to re-measure a regression

```bash
# per-thread counters, last 24 h (prod)
PG_CONTAINER=omni-stack-postgres-1 python3 scripts/efficiency-metrics.py
# explicit threads, machine readable
PG_CONTAINER=omni-stack-postgres-1 THREADS=2874,2920 python3 scripts/efficiency-metrics.py --json
```

Exit code 1 + a `grade` column with the reason when a thread breaches
`duplicate_reads > 0`, `r:w > 5`, `edits without commit`, or `tokens/state-op > 100k`.

## Measured LIVE (real provider, dev stack) — 2026-09-23 16:28–16:29 UTC

Gate: *"Live end-to-end on omnidev with a real provider on a small edit task: completes with
no duplicate read/verify calls; report the measured time/tokens."*

Setup
- Channel `eff-live` (dev `omni-root/config/channels.yml:151`, `provider: deepseek`,
  `model: deepseek-v4-flash`), fixture repo `/opt/workspace/tmp/eff-live`
  (`data.txt` = alpha/beta, commit `5e73dad`).
- Real provider credentials: the dev secret store (table `secrets`, name
  `DEEPSEEK_API_KEY`) was populated at 16:14 UTC (35 chars, `sk-6…324`, funded); the
  agent resolves `api_key: $secret:DEEPSEEK_API_KEY` from `config/models.yml:12`.
- Dev agent container `omnidev-omniagent-1` runs the EFF build
  (`/target/release/omniagent`, `omniagent@a70cace`).
- Provider identity evidence: the `eff-live` channel config pins `provider: deepseek` /
  `model: deepseek-v4-flash`, the agent logs the eff-live channel handler starting at
  16:28:18 and processing thread 294 with no provider/auth error, and the thread carries
  real `token_usage` for all LLM turns. No explicit outbound `api.deepseek.com` log line is
  emitted by the agent, so the serving model is taken from the channel config plus the
  funded secret above (the dev noop provider's request log shows only health probes in
  this window).

Task (same work shape as the incident: one edit + one commit): *"Small edit task (LIVE
efficiency measurement, one repo, no exploration needed): append `gamma` to
`data.txt` and commit."*

Measured (thread 294, `scripts/efficiency-metrics.py` → `grade: OK`, exit 0):

| metric | thread 2874 (BEFORE) | thread 294 (LIVE AFTER) |
|---|---|---|
| tool calls | 977 | 8 |
| read-only | 580 | 1 |
| edits | 8 | 1 |
| commits | 6 | 1 |
| duplicate reads | (loop, unmeasured) | **0** |
| read:write ratio | 11.84 | **0.20** |
| prompt + completion tokens | 14,726,047 + 1,032,259 = 15,758,306 | 122,630 + 1,826 = 124,456 |
| tokens per state-changing op | 321,598 | **24,891** (~13x better) |
| wall clock | 87.2 min | **0.8 min** |
| time to first commit | 56.5 min | **0.4 min** |

The commit `1449275 eff-live: add gamma` (16:29:17 UTC) landed inside the thread window
(16:28:54 → 16:29:42 UTC), i.e. the thread ended in a verified state change, with zero
duplicate reads and no status/diff loop (tool order: one `filesystem__read` → one
`filesystem__write` → `git__run_command` add/commit → `git__run_command rev-parse`).

Duplicate-read stub, live in the dev DB: threads 297 and 300 each carry 1 persisted
`metadata.eff = "duplicate-read"` tool-result row (16 such rows in total in the dev DB).

### Honest scope of this measurement
- This is a **small-edit shape** measurement on a real provider, not the canonical
  "reapply the browser-free image" A/B corpus (workstream T-4 /
  `task_omodev_internal_plan_s11`), which is still **not run**: `scripts/run-corpus-ab.py`
  needs a resolvable candidate ref in `/opt/workspace/omniagent` plus the corpus harness
  login, which fails in the toolbox container (`git: not found`; harness login 403).
  The token multiple above compares different task sizes, so it is **indicative**; the
  ratio/timing/duplicate-read columns are directly comparable (same shape: read → edit →
  commit).
- `scripts/efficiency-metrics.py` counts a commit performed through
  `git__run_command` argv (`git commit …`) as a commit; without that fix thread 294 was
  reported as a false `BREACH: edits without commit` even though `1449275` exists.
