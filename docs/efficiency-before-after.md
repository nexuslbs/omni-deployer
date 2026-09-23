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
