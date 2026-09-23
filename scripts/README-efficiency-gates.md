# noop-provider E2E efficiency gates

Scripted verification of the **agent EFFICIENCY contract** without a real LLM
(root-cause fix for the **self-repetition** loop class, incident thread 2874,
post-mortem thread 2907). Operator correction 2026-09-23: **read-only
verification is dropped** — the defect is repetition, and the guard is generic
(opaque tool id + canonical arguments).

| script | what it checks |
|---|---|
| `efficiency-genericity-gate.py` | **C3**: scans the ADDED lines of the core production diff for a `plugin__tool` literal or a plugin-local convention. `#[cfg(test)]` modules inside production files are ignored and counted. Expect `0` violations. |
| `efficiency-gate.sh` | static/live asset gate: EFF-1..EFF-4 markers in `src/agent/efficiency.rs`, the C1 removals (no read-only verification machinery in `src/agent`/`src/mcp`, no `repeat_guard` in the plugin manifests), the C3 genericity, MEMORY.md contract, generated prompt contract (`/prompt/<channel>`), executor/tester template gates, daily-report Step 6b, read-once skill, contract wiki page, and no `min(base,12)`-style narrowing. |
| `efficiency-noop-gate.sh` | E2E against the dev stack: **GATE A** identical re-issued invocation -> payload-free `[duplicate call` stub + per-thread counter > 0; **GATE A2** the same call after a state change EXECUTES; **GATE B** a merged operator reply on a LIVE thread raises `LIVE INTERRUPT`; **GATE C** the efficiency unit suite + the structured provider fast-fail. |
| `efficiency-metrics.py` | per-thread counters from the DB: `tools / state_changing / edits / duplicate_calls / commits / tokens / tokens-per-state-op / wall / time-to-first-commit` (+ `--json`). Exit 1 on breach (`duplicate_calls > 0`, edits without commit, `tok/state-op > 100k`, edit-shaped non-delivery). **No read-only counter, no read:write ratio.** |

## Measured results (dev stack, 2026-09-23, omniagent@dfd11fa)

```
genericity-gate : PASS - 478 added production lines scanned, 0 plugin/tool-name literal
efficiency-gate : 28 passed, 0 failed            (SKIP_TESTS=1)
noop-gate       : 8 passed, 0 failed, 0 skipped  (GATE A 3 stubs / 1 executed; A2 executed; B LIVE INTERRUPT; C 10 unit tests)
```

## Real-provider measurements (2026-09-23, omniagent@be645ea)

The canonical A/B corpus and the live end-to-end run are now MEASURED (dev stack, real
DeepSeek provider); full tables in `docs/efficiency-before-after.md`.

- **Live end-to-end, canonical 2874 shape** (browser-free workstation image, repo
  `/opt/workspace/tmp/eff-canary` reset to the baked state): thread 809 - **4.1 min, 30
  iterations, 1.16M prompt tokens, 0 duplicate calls, commit `8e660c8`**. Incidents 2874:
  87.2 min / 247 calls / 14.7M prompt tokens / no commit.
- **Small edit shape** (`/opt/workspace/tmp/eff-live`): thread 788 - **0.9 min, 7
  iterations, 153k prompt tokens, 1 commit, `duplicate_calls=0`.**
- **Same-environment BEFORE/AFTER** on the canonical shape (binary swapped in place, identical
  prompt): no-guard `6c46b6e` thread 829 = 10.9 min / 3.00M prompt tokens / `tok/state-op` 104,924
  (BREACH) vs guard `be645ea` thread 809 = 4.1 min / 1.16M prompt tokens / 37,142 (OK); the older
  in-tree guard `3d5bce1` sample was faster (2.0 min / 514k) - one sample per side, variance is of
  the same order as the effect (see `docs/efficiency-before-after.md`, section 1b).
- **A/B corpus** `run-corpus-ab.py --tasks t02,t05 --skip-long`, side a = `be645ea`:
  2/2 PASS on both sides, 0 compactions, duplicate markers 2 vs 2 (`6c46b6e`) and 2 vs 3
  (`3d5bce1`) - no success-rate or token regression.

Vehicle fix (this is why the harness had failed): the dev core reads its config from its
own `OMNI_DIR` = `/opt/omni-stack/config`; the runner defaulted to `/opt/omni/config`, so
its toolset was never loaded and every corpus post was dropped (`timeout-no-thread`). The
default is corrected, `--mm-channel`/`CORPUS_MM_CHANNEL` was added, and an empty omni
profile in the checkout now falls back to the empty profile instead of aborting. The dev
runtime `channels.yml` also carried a stale dev-channel id and was corrected. When the omnidev
agent container is recreated its container-local data dir comes back empty (no config, no
profiles tree): `ensure_toolset` now seeds both from the mounted checkout, so the corpus posts are
accepted AND the generated prompt still carries the profile memory (the efficiency contract, which
lives in `profiles/omni/MEMORY.md`). Re-verified end to end on a wiped data dir: seeding reported
`plugins.yml=yes profiles_omni=yes` and the run completed `t02` PASS on both sides
(`/opt/workspace/tmp/corpus-ab/20260923-231046`).
