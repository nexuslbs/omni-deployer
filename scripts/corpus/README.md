# Canonical A/B task corpus (omniagent internal plan, workstream T, item T-4)

The primary A/B evidence harness for the periodic smartness/efficiency cycle
described in `Projects/Omniagent/Omniagent-Internal-Improvement-Plan.md`
(workstream §3T, candidate S11). Every later prompt, compaction, memory,
notes or search change is compared against current `main` by running this
corpus in the omnidev dev stack and diffing the §3T gates.

## What is here

- `t01-*.json` ... `t08-*.json`: the canonical task corpus. 8 tasks, one JSON
  file each, covering the §3T waste shapes:

  | id | shape | what it exercises |
  |----|-------|-------------------|
  | t01 | project-layout discovery | locate 5 components in the omniagent repo by bounded search |
  | t02 | re-read-heavy verification | verify corpus directory claims; read each file once, note facts |
  | t03 | search-recall | retrieve a run-seeded token with `search_messages` |
  | t04 | project-layout discovery | locate 5 components in the omni-deployer repo |
  | t05 | re-read-heavy verification | git-state verification of omni-deployer + cycle-gates.sql |
  | t06 | long task (>150 iterations) | per-file knowledge index of ~30 Rust sources with notes discipline |
  | t07 | fresh-thread follow-up | write side: post a run-seeded witness token |
  | t08 | fresh-thread follow-up | recall side: fresh thread must retrieve the t07 witness via search |

  Each file declares: `id`, `shape`, `title`, `prompt` (may contain the
  placeholders `{TOKEN}`, `{WITNESS}` and `{WITNESS_PREFIX}`, filled per run
  and per side by the runner), `expected` (substring markers, optional regex,
  optional token equality), `timeout_min`, `target_iterations` and a `note`.

- `run-corpus-ab.py` (one directory up, `scripts/`): the runner. Executes the
  corpus against the running omnidev core, measures the §3T gates per task
  and per side, and emits a markdown gate comparison table plus JSON.

## How to run

Run from a docker.sock-capable host that is on the omnidev compose network
(e.g. `docker exec -it omnidev-toolbox-1 bash`, which has docker, python3 and
`/opt/workspace` mounted), with the omnidev stack up:

```
python3 /opt/workspace/omni-deployer/scripts/run-corpus-ab.py --help
python3 /opt/workspace/omni-deployer/scripts/run-corpus-ab.py            # identity A/B (main vs main)
python3 /opt/workspace/omni-deployer/scripts/run-corpus-ab.py --candidate my-branch --skip-long
python3 /opt/workspace/omni-deployer/scripts/run-corpus-ab.py --list     # validate corpus, no run
```

- Default mode is an IDENTITY A/B (both sides run the currently deployed
  omnidev binary). It proves corpus stability: every task must complete with
  the same outcome on both sides, and the gate table shows the two columns.
- `--candidate <git-ref>` builds the omnidev `omniagent-dev` image from a git
  worktree of `/opt/workspace/omniagent` at that ref, recreates the omnidev
  omniagent container, runs side b against the candidate binary, then
  rebuilds and restores the baseline image. Production stacks are never
  touched; only the omnidev dev project is rebuilt.
- The runner temporarily enables the standard tool plugins (filesystem,
  search, notes, git, memory) plus the deepseek provider config on the
  omnidev core for the run (`/opt/omni-stack/config/plugins.yml`,
  in-container and ephemeral), restarts the omnidev omniagent container once
  so the config is loaded, and restores the original file afterwards. Use
  `--no-toolset` to skip.

## What the runner emits

A gate comparison table per §3T:

- T-1 helpfulness: task status/outcome, iterations, duplicate-read markers,
  compaction events, retrieval calls, grounded responses, token totals.
- T-2 speed: search p50/p95 latency probes and prompt-generate build ms on
  sample threads (measured per side).
- T-3 resources: DB size, spill/threads dir sizes, container RSS before and
  after each side.

Output lands in `/opt/workspace/tmp/corpus-ab/<run-timestamp>/`
(`report.md`, `results.json`, task final answers, toolset backup).

## Corpus stability contract

Each task has a defined expected outcome (its JSON `expected` block). A run
side PASSES a task when the final agent message satisfies the expectation.
An identity run (main vs main) must yield the same outcome per task on both
sides; a real A/B run must show no success-rate loss on the candidate while
iterations/tokens/resources move per the §1 gate direction.
