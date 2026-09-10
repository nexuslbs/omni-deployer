# omni-deployer/tests - service regression test harnesses

These harnesses test behaviour of the omni-stack / omni-root services. The code
under test stays in the stack repos (services/toolbox, services/vector), so the
harnesses locate it via a checkout default (the `../omni-stack` sibling of this
repo, falling back to `/opt/workspace/omni-stack`) or an explicit variable.

- `test_git_sync_hook.sh` - toolbox `backup.sh` / `restore_backup.sh` git-sync
  hook gates (stubbed docker/curl/rclone sandbox; needs bash).
  Run: `bash tests/test_git_sync_hook.sh` (optionally `TOOLBOX_DIR=...`).
- `run_tests.sh` + `level_token_test.toml` - vector `remap_logs` level-token
  derivation suite. Concatenates the real `services/vector/transforms.toml`
  with the test fragment and runs `vector test`. Needs a `vector` binary.
  Run: `VECTOR_SRC=... sh tests/run_tests.sh` (optionally `VECTOR_BIN=...`).

- `test_remote_yml_source_of_truth.py` - plugin listing regression guard:
  a plugin declared ONLY in `config/remote.yml` (no `plugins.yml` entry, not
  cloned) must still appear in `GET /api/plugins` (dashboard Platforms page).
  Runs against a live dev stack, restores the config it touches.
  Run (host): `python3 tests/test_remote_yml_source_of_truth.py`
  Run (container): `docker exec -i omnidev-omniagent-1 python3 - < tests/test_remote_yml_source_of_truth.py`

## test_omni_stack_repo_hygiene.py (omni-stack repo hygiene, WS4)

Regression guard for the operator rule (2026-09-10, thread 1614): the
omni-stack repo must contain NO `config/`, `profile(s)/` or `plugin(s)/`
directories (only transiently during a run, never committed) and those paths
must NOT be gitignored, so a transient presence stays visible in `git status`.

```sh
python3 tests/test_omni_stack_repo_hygiene.py                 # inspect /opt/workspace/omni-stack
python3 tests/test_omni_stack_repo_hygiene.py --repo-dir DIR   # inspect another checkout
OMNI_STACK_HYGIENE_STRICT=1 python3 tests/test_omni_stack_repo_hygiene.py  # also fail on untracked leftovers
```

Exit 0 = clean; exit 1 = a forbidden path is tracked, or `.gitignore` hides
one (also fails, with `--strict`, when such a path is left on disk without
being tracked). Demonstrated: PASS on the clean omni-stack checkout, FAIL on a
synthetic repo with tracked `config/`+`plugins/` and a `.gitignore` hiding
`profiles/`.
