# noop-provider E2E efficiency gates

Scripted verification of the **agent EFFICIENCY contract** without a real LLM
(root-cause fix for the re-read / no-progress / ignored-stop-signal loop class,
incident thread 2874, post-mortem thread 2907).

| script | what it checks |
|---|---|
| `efficiency-gate.sh` | static/live asset gate: EFF-1..EFF-4 markers in `src/agent/efficiency.rs`, MEMORY.md contract, generated prompt contract (`/prompt/<channel>`), executor+tester template gates, daily-report loop-regression section, read-once skill, contract wiki page, no `min(base,12)`-style narrowing. **PASSED 21/21** (run with `OMNIAGENT_REPO`, `OMNI_DIR`, `CHANNEL` set correctly). |
| `efficiency-noop-gate.sh` | E2E against the noop model `test-tool-caller`: **A)** the same file read 4x with overlapping (not byte-identical) ranges must yield `[duplicate read` stubs and a thread duplicate-read counter > 0; **B)** a `[sub_cause]` reply merged into the still-processing thread must raise the `=== LIVE INTERRUPT ===` marker; **C)** 402/insufficient-balance/auth fatal classification + `cargo test --lib efficiency::`. **NOTE: not yet green — see below.** |
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

# noop E2E gate (needs the dev stack)
OMNI_DIR=/opt/omni MM_CHANNEL_NAME=test-channel TIMEOUT=120 \
  bash scripts/efficiency-noop-gate.sh
```

## Status

`efficiency-noop-gate.sh` still needs its two fixes validated live:

1. `mmctl` (>= 10) takes the channel as a **positional** argument — `mmctl --local post
   create <channel> --message '<json>'` (the earlier `--channel` form fails with
   `unknown flag: --channel`). Fixed in the current version, not yet re-run to green.
2. GATE C must be executed via `docker exec <agent-container> bash -lc "cd /app &&
   cargo test --lib efficiency::"` (inside the container; `docker compose exec` from the
   tool path resolves the wrong project/environment). Fixed, not yet re-run to green.

The core fix itself is verified independently: `cargo test --lib` = 966 passed / 0 failed
(repo `/opt/workspace/omniagent` @ `01f40b9`), and the live generated system prompt
contains the `EFFICIENCY CONTRACT` section.
