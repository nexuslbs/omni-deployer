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
