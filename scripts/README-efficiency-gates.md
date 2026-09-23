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

## Still open (tester scope)

The canonical A/B corpus (`scripts/run-corpus-ab.py`, browser-free-image task) with a
real provider — it needs a resolvable candidate ref in `/opt/workspace/omniagent` and a
corpus login path; the harness previously failed from the toolbox container
(`git: not found`, login 403). Report `duplicate_calls`, total tokens and
time-to-first-commit before/after.
