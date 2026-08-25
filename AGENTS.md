# Omni-Deployer: AGENTS.md

Deployment orchestration and CI/CD for the OmniAgent stack (`deploy.py`, `omnidev.py`,
`omnistable.py`, `shared.py`).

---

## HARD RULE: Deploy execution NEVER uses a real LLM or real LLM keys

`deploy.py` (modes `dev`, `ci`, `hybrid`, `test`) is a **self-contained test harness**.
It must NEVER:

- call a real LLM (no DeepSeek, no opencode-go, no external API),
- seed, reference, or consume real LLM API keys (`DEEPSEEK_API_KEY`, `OPENCODE_API_KEY`),
- read `secrets.env` or touch the omniagent secret store.

All LLM interaction during deploy tests goes through the **noop provider** only:

- `noop` provider with model `test-tool-caller` — for scripted tool-call tests
  (kanban executor/tester/reviewer threads, non-blocking tasks, etc.),
- `noop-full` provider with model `test-model-1` — for provider round-trip tests.

The noop test provider definitions are sourced from **omni-plugins' root `models.yml`**
(plugin-less), not from the omniagent image — keep them there when extending tests.

Every test channel is patched to `current_provider=noop` /
`current_model=test-tool-caller` (or `noop-full`/`test-model-1`) before use and
restored afterwards. A real provider (e.g. `opencode-go`) must NEVER be the active
channel provider while tests run. The `resolve_thread_identity` channel-provider
override (omniagent `de3d34b`) guarantees executor threads resolve to the channel's
`noop`, not the profile default — do not regress that precedence.

### Why this exists

A previous change (`f77dde7`) seeded `DEEPSEEK_API_KEY` / `OPENCODE_API_KEY` from
`secrets.env` into the secret store before deploy tests "so kanban executor threads
don't 401". That is forbidden: the deploy must prove the stack works **without** any
real LLM credentials. The 401 root cause was provider resolution, fixed properly in
omniagent. If a test 401s or fails to reach an LLM, the fix is to make the test use
the noop provider — **never** to seed real keys.

## Secrets live ONLY in omnidev / omnistable

Real LLM keys (`DEEPSEEK_API_KEY`, `OPENCODE_API_KEY`, git app keys) are defined in
`secrets.env` and consumed **only** by:

- `omnidev.py setup` → `shared.setup()` (dev stack),
- `omnistable.py setup` → `shared.setup()` (stable stack).

`shared.setup()` is the ONLY caller of `configure_secret_refs()` (writes
`$secret:DEEPSEEK_API_KEY` / `$secret:OPENCODE_API_KEY` refs into `plugins.yml`) and
the `ensure_secret()` loop over `load_secrets_env()`. `deploy.py` must never call
`shared.setup()` — it calls only `shared.init()` + `shared.run_tests()`.

### Env file split (do not blur)

| File | Used by | Contains |
|---|---|---|
| `omni.env` | `deploy.py` (generated at run time) | compose settings, DB passwords — **NO LLM keys** |
| `omnidev.env` | `omnidev.py` | dev-stack env (tunnel token, etc.) |
| `omnistable.env` | `omnistable.py` | stable-stack env (tunnel token, etc.) |
| `secrets.env` | `shared.setup()` via omnidev/omnistable only | `DEEPSEEK_API_KEY`, `OPENCODE_API_KEY` — real keys |

`secrets.env` is never read by `deploy.py` — grep `load_secrets_env` /
`ensure_secret` / `configure_secret_refs` to confirm before changing anything.

## Repo hygiene (MANDATORY)

Never commit scratch helper/driver scripts (`_run_*.py`, `push_*.py`, `_g4x-*.py`, `.task*`,
`.push*`, `.smoke*`, `*.patch`, probe/diag scripts). Scratch helper scripts belong ONLY in
`OMNI_DIR/data/scripts/` or `omni-stack/data/scripts/` — both gitignored and never versioned.
Never create them inside the repo tree, and never `git add -A` blindly (stage only the files a
task actually changed). A committed scratch script or credential is an automatic review FAIL.

## Test flow (deploy.py)

1. Generates `omni.env` (no LLM keys) + seeds `remote.yml`.
2. Stops services, removes data volumes (clean slate).
3. `dev`: builds images; dev runs cargo pretests (fmt/clippy/unit) in the
   dev container. `hybrid` builds the production Dockerfile (its builder stage
   runs the same quality gates). `ci` SKIPS pretests — the production Dockerfile
   builder already ran the identical gates during the CI image build, so
   re-running them on the runner (cold cargo cache, 2-core) would duplicate
   work and blow the time budget.
4. Starts services, registers the remote noop provider, runs the integration test
   suite (`scripts/tests.py`, GROUP 1–49), then the shared tool tests
   (`shared.run_tests()`), twice (single pass in `ci` mode — the hosted runner's
   1h budget can't fit the double pass; dev/hybrid keep 2 passes).
5. Restores omni-stack tracked config to HEAD.

A successful run ends with `ALL TESTS PASSED (including shared tool tests)` and all
assertions green (the exact count grows as new groups are added — do not pin it).
Verify after every run: no "Seeding API secrets" line, no 401s, and step threads
read back `provider=noop` / `model=test-tool-caller`.

## Verification checklist (after any deploy change)

- [ ] `grep -n "load_secrets_env\|ensure_secret\|configure_secret_refs" deploy.py` → empty
- [ ] Run log contains no `Seeding API secrets` / `Secret DEEPSEEK` / `Secret OPENCODE`
- [ ] Run log contains no `401` / `unauthorized`
- [ ] Workflow threads in log show `provider: noop, model: test-tool-caller`
- [ ] Final summary: `ALL TESTS PASSED`, 0 failed
