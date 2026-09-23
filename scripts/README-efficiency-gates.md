# noop-provider E2E efficiency gates

Scripted verification of the **agent EFFICIENCY contract** without a real LLM
(root-cause fix for the re-read / no-progress / ignored-stop-signal loop class,
incident thread 2874, post-mortem thread 2907).

| script | what it checks |
|---|---|
| `efficiency-gate.sh` | static/live asset gate: EFF-1..EFF-4 markers in `src/agent/efficiency.rs`, MEMORY.md contract, generated prompt contract (`/prompt/<channel>`), executor+tester template gates, daily-report loop-regression section, read-once skill, contract wiki page, no `min(base,12)`-style narrowing. **PASSED: 20 passed, 0 failed** with `SKIP_TESTS=1` (`OMNIAGENT_REPO=/opt/workspace/omniagent OMNI_DIR=/opt/omni CHANNEL=main`); 21 checks with the cargo test gate enabled. |
| `efficiency-noop-gate.sh` | E2E against the noop model `test-tool-caller`: **A)** the same file read with overlapping (not byte-identical) ranges must yield `[duplicate read` stubs and a thread duplicate-read counter > 0; **B)** a reply merged into the still-processing thread must raise the live-interrupt marker; **C)** 402/insufficient-balance/auth fatal classification + `cargo test --lib efficiency::`. **GREEN: 5 passed, 0 failed, 0 skipped** (run inside `omnidev-toolbox-1`, 2026-09-23). |
| `efficiency-metrics.py` | **observability**: per-thread counters straight from the `messages` table — `tools / read_only / git / state_changing / edits / duplicate_reads / commits / read:write ratio / tokens / tokens-per-state-op / wall / time-to-first-commit` + a `grade` reason; exit 1 on breach (`duplicate_reads > 0`, `r:w > 5`, edits without commit, `tokens/state-op > 100k`). Runs on any PG container (`PG_CONTAINER=`, `HOURS=` or `THREADS=`, `--json`). Wired into daily-report Step 6b. Before/after report: `docs/efficiency-before-after.md`. |
| `efficiency-noop-gate.py` | same gates driven from inside the omniagent container through the test harness (`tests._wf_dedicated_channel()`, `tests._mm_login()`); currently blocked by a 403 on the harness login path. |

## Requirements

Dev stack UP: `noop-provider` + `mattermost` + `omniagent` (project `omnidev`,
`/opt/omni/docker-compose.yml` + `docker-compose.dev.yml`, env
`/opt/workspace/omni-deployer/omnidev.env`). The noop harness replays the **first user
message** as a JSON array of `{tool, arguments}` calls via the model
`test-tool-caller`; the dedicated channel is Mattermost `test-channel` in team `omni`
(omniagent channel `test-channel`, provider `noop`, model `test-tool-caller`).

```bash
# static + live-prompt asset gate (all assets)
cd /opt/workspace/omni-deployer
SKIP_TESTS=1 OMNIAGENT_REPO=/opt/workspace/omniagent OMNI_DIR=/opt/omni CHANNEL=main \
  bash scripts/efficiency-gate.sh          # => 21 passed, 0 failed

# noop E2E gate (needs the dev stack) - run INSIDE the dev toolbox container,
# because the gate drives docker exec + psql on the sibling dev containers:
AGENT_CONTAINER=omnidev-omniagent-1 MM_CONTAINER=omnidev-mattermost-1 \
PG_CONTAINER=omnidev-postgres-1 TOOLBOX_CONTAINER=omnidev-toolbox-1 \
MM_TEAM=omni MM_CHANNEL=test-channel TIMEOUT=180 \
  bash scripts/efficiency-noop-gate.sh          # => 5 passed, 0 failed

# per-thread efficiency counters (prod DB)
PG_CONTAINER=omni-stack-postgres-1 python3 scripts/efficiency-metrics.py
```

## Status

**GREEN.** `efficiency-noop-gate.sh` = **5 passed, 0 failed, 0 skipped** on the live dev
noop stack (GATE A dup stub rows=1 + per-thread counter >= 1; GATE B live interrupt with
sub-prompt merge rows=1 and the matching agent-log WARN; GATE C unit suite + fatal-provider
classification). Two harness fixes were needed and are in tree:

1. `mmctl` (>= 10) takes the channel as a **positional** argument — the REST poster
   `scripts/noop-gate-post.py` is used instead (`mmctl --local post create` is broken on
   this server).
2. GATE C executes via `docker exec <agent-container> bash -lc "cd /app && cargo test --lib
   efficiency::"` (inside the container; `docker compose exec` from the tool path resolves
   the wrong project/environment).
3. GATE B's reply had to be posted 1 s (not 12 s) after the scripted prompt, otherwise the
   scripted noop thread had already completed and the reply opened a new thread instead of
   merging into the live one.

The core fix itself is verified independently: `cargo test --lib` = 966 passed / 0 failed
(repo `/opt/workspace/omniagent` @ `a70cace`), and the live generated system prompt
contains the `EFFICIENCY CONTRACT` section.

## Open (honest gap)

The **live real-provider A/B** on the canonical `reapply the browser-free image` task could
NOT be run: no provider API key is present in the dev or prod agent containers
(`DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `OPENCODE_API_KEY` all unset; direct provider probes
return 401), and the previously used account returns `402 Payment Required: Insufficient
Balance`. So the scripted gates above prove the **mechanism**, but the token-reduction claim
(zero duplicate reads / `r:w < 5` / >= 10x tokens) is **not yet measured on a live run**.
Re-run `scripts/run-corpus-ab.py` plus `scripts/efficiency-metrics.py --json` on the resulting
thread ids once a funded key is available, and paste the two rows into
`docs/efficiency-before-after.md`.

## LIVE measurement (real provider) — 2026-09-23

Gate: live end-to-end on the dev stack with a real provider on a small edit task.

Recipe actually used:
1. Fund/provide a provider key in the dev secret store (`secrets.name =
   DEEPSEEK_API_KEY`, `config/models.yml:12` resolves `$secret:DEEPSEEK_API_KEY`).
2. Point a dev channel at it (`eff-live`, `provider: deepseek`,
   `model: deepseek-v4-flash`, `omni-root/config/channels.yml:151`) — see
   `scripts/eff-live-setup.py` for the channel/fixture preparation, and
   `scripts/post-by-channel-id.py` to post the task into the channel by id.
3. Fixture repo `/opt/workspace/tmp/eff-live` (`data.txt`, commit `5e73dad`); task =
   append `gamma` + commit.
4. Measure: `PG_CONTAINER=omnidev-postgres-1 THREADS=<id> python3 efficiency-metrics.py --json`.

Result — thread 294, `grade: OK` (exit 0): 8 tool calls, 1 read-only, 1 edit, 1 commit,
**0 duplicate reads**, read:write **0.20**, 124,456 tokens, 24,891 tokens/state-op,
0.8 min wall, time-to-first-commit 0.4 min. Commit `1449275` landed in the fixture repo
inside the thread window. Before/after table: `docs/efficiency-before-after.md`.

Note: a commit made through `git__run_command` argv (`git commit …`) is now counted as a
commit by `efficiency-metrics.py`; before that fix the live thread was graded
`BREACH: edits without commit` although the commit existed.

Still open: the canonical A/B corpus (`scripts/run-corpus-ab.py`, browser-free-image
task) is not run — it requires a resolvable candidate ref in `/opt/workspace/omniagent`
and a corpus login path that currently fails from the toolbox container
(`git: not found`, harness login 403).
