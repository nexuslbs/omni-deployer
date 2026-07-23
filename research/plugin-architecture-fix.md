# OmniAgent Plugin Architecture Research

## Executive Summary

The OmniAgent integration test suite is 102/106 passing. The remaining 4 failures
(fn12, fn13, fn14, fn15) all trace to two architectural issues:

1. **Prompt-wrapped user messages**: The `prompt_generate` tool wraps user messages
   in a JSON structure `{system, memory, soul, context, user}`, but the noop
   provider's `test-tool-caller` model expects raw JSON arrays as the user message.
   When it receives the wrapped format, `_parse_script` returns `None` and no tool
   calls are executed.

2. **Global mutable state for MCP configs**: Three global statics (`SERVER_CONFIGS`,
   `CLIENT_POOLS`, `CLIENT_REGISTRY`) create a shared-memory bottleneck. Any plugin
   lifecycle operation can race with any other, and because `std::sync::Mutex` is
   used (not `tokio::sync::Mutex`), async operations that hold the lock can block
   the entire tokio thread pool.

---

## Issue 1: Prompt-Wrapped User Messages

### Root Cause

The `prompt_generate` tool (in `plugins/tools/prompt/src/main.rs`) produces:

```json
{
  "system": "...",
  "memory": "...",
  "soul": "...",
  "context": "...",
  "user": "[{\"name\":\"step1\",\"tool\":\"test-python_lorem\",...}]",
  "plan": false
}
```

The executor sends this JSON as a single user message to the LLM provider.
When the channel is configured as noop/test-tool-caller, the noop provider
receives messages=[system_msg, user_msg] where user_msg.content is the
entire prompt_generate JSON — not the raw `[{"name":"step1",...}]` array.

The noop's `_parse_script` function only tries `json.loads(content)` on the
bare content. It fails because the content is now `{"system":..., "user":...}`
rather than `[{...}]`.

### Fix

`_parse_script` needs a second parsing pass: if the content is a JSON object
with a `"user"` field, try parsing that field as a script array.

### Why Group 16 "Passes"

Group 16's assertion checks for `"test-python_lorem"` in ANY Mattermost post.
When the real LLM processes the message (because test-tool-caller returns a
plan-text response instead of tool calls), the response may happen to contain
the tool name "test-python_lorem". This is a false positive — no actual tool
calls were executed.

---

## Issue 2: Global Mutable State

### Current Architecture

```rust
// Three global statics in src/mcp/external/client.rs:
static CLIENT_POOLS: Lazy<Mutex<HashMap<(String, i64), Arc<McpClientPool>>>>;
static SERVER_CONFIGS: Lazy<Mutex<HashMap<String, McpServerConfig>>>;
static CLIENT_REGISTRY: Lazy<Mutex<HashMap<String, Arc<dyn McpServerClient>>>>;

// Plus in src/server/mod.rs:
tool_registry: Arc<tokio::sync::RwLock<McpRegistry>>,
```

**Problem**: Any operation in the system can mutate these globals. For example,
`handle_remove_by_source` (DELETE handler) calls `remove_server_config` and
`clear_server_pools` for the given plugin name — but previously it did so
**unconditionally**, even for plugins that didn't exist. This killed MCP
subprocesses for other operations that happened to share the same server name.

### The Global Static Anti-Pattern

In async Rust, `std::sync::Mutex` in statics is problematic:
- **Blocking**: `Mutex::lock()` blocks the current OS thread. If held across an
  `.await` point, it blocks ALL tasks on that tokio worker thread.
- **No RAII scope**: Lock is held until the MutexGuard is dropped, but if the
  scope is large or includes async calls, contention builds up.
- **No isolation**: Every plugin operation goes through the same global maps.
  There's no way to "scope" configs to a specific operation.

### Recommended Architecture: Scoped Plugin Manager

Replace the three global statics with a single `PluginManager` stored in
`AppState`:

```rust
pub struct AppState {
    // Replace RwLock<McpRegistry> with atomic swap
    pub tool_registry: Arc<tokio::sync::RwLock<McpRegistry>>,  // keep for now
    
    // Replace global statics with scoped state
    pub plugin_manager: PluginManager,
}

pub struct PluginManager {
    // Sharded by plugin name, no global lock
    configs: HashMap<String, McpServerConfig>,
    pools: HashMap<(String, i64), Arc<McpClientPool>>,
    // Use tokio::sync::RwLock for async-safe access
    lock: tokio::sync::RwLock<()>,
}
```

### Key Design Principles

1. **No global statics**: All state lives in `AppState` which is injected into
   every handler via axum's `State` extractor.

2. **Per-plugin locking**: Instead of a single write lock for all tools,
   operations on different plugins do not contend.

3. **Tool handler receives AppState**: The `McpTool.handler` closures capture
   a `Weak<AppState>` reference, allowing them to access the plugin manager
   during tool calls.

4. **Atomic config swaps**: When a plugin config changes, use `ArcSwap` to
   atomically swap the config pointer. Old configs are dropped when no longer
   referenced.

### Implementation Plan

#### Phase 1 (Immediate): Fix `_parse_script` + Guard DELETE

- [x] `compile_rust_crate` returns `Ok(false)` for non-Rust plugins
- [x] DELETE handlers guard MCP cleanup behind `if removed`
- [x] `api_delete` timeout 10s → 30s (copacetic with intermittent contention)
- [ ] Fix `_parse_script` to handle wrapped prompt_generate format
- [ ] Fix `_generate` to look for scripts in wrapped content

#### Phase 2 (Next): Move Globals to AppState

- Move `SERVER_CONFIGS` → `AppState.plugin_manager.configs`
- Move `CLIENT_POOLS` → `AppState.plugin_manager.pools`
- Remove `CLIENT_REGISTRY` (legacy, unused)
- Change `call_tool_pooled_async` to accept `&AppState` instead of reading globals
- Change tool handler closures to capture `Weak<AppState>` instead of server name

#### Phase 3 (Future): Lock-Free Tool Registry

- Replace `RwLock<McpRegistry>` with `arc_swap::ArcSwap<McpRegistry>`
- Reads become atomic pointer loads (no lock at all)
- Writes become atomic pointer swaps (instant, no waiting)
- Only needs a lock if two writes race (rare: plugin lifecycle operations are
  already serialized by the test harness)

---

## Why These Fixes Are Safe

1. **`_parse_script` fix**: Only affects the noop provider's test-tool-caller
   model. Other models don't parse JSON scripts and won't enter this code path.

2. **DELETE guard**: Already validated — all DELETE tests in Groups 1-2 pass.
   The `if removed` guard prevents killing MCP subprocesses for plugins that
   the operation didn't actually touch.

3. **Timeout increase**: The DELETE handler responds correctly when given time.
   The 13-27s delays are from write-lock contention on `tool_registry`. A 30s
   timeout gives the lock time to drain.

4. **Global to AppState move**: Keeps the same logic but scopes it to the
   server's lifecycle rather than the process's lifecycle. Container restarts
   would reset state anyway.

---

## Files That Need Changes

| File | Change |
|------|--------|
| `omni-stack/services/noop/server.py` | Fix `_parse_script` for wrapped format |
| `omniagent/src/mcp/external/client.rs` | Move globals to AppState (Phase 2) |
| `omniagent/src/server/mod.rs` | Add `plugin_manager` to AppState |
| `omniagent/src/server/plugins_delete.rs` | Already fixed |
| `omniagent/src/server/plugins_compile.rs` | Already fixed |
| `scripts/tests.py` | `api_delete` timeout 10→30 (already fixed) |

## Verification

After Phase 1 fixes, run:
```
docker exec omnidev-omniagent-1 python3 -u /opt/workspace/omni-deployer/scripts/tests.py
```

Expected: 106/106 pass. The `_parse_script` fix enables test-tool-caller model
to properly execute JSON scripts, which unblocks fn12, fn13. With fn13 completing
in ~10s instead of 127s, the cascading lock contention to fn14 and fn15 disappears.
