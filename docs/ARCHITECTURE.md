# subagent v1 Architecture Memo

## Layers
- CLI surface: `subagent` Typer commands for config/registry, controller ownership, worker lifecycle, and turn operations.
- Local control plane: `subagentd` process for bootstrap/status, heartbeat, worker runtime health checks, and auto-restart attempts.
- Runtime state: sqlite-backed `StateStore` for controllers, controller instances, workers, event journal, approval requests, and handoff snapshots.
- Runtime adapter: per-worker ACP runtime process (`subagent.worker_runtime`) with unix-socket IPC (`runtime_service`).
- Config registry: loader for `launchers` / `roleHints` / `roleDefaults` / `defaults` from config.

## Current v1 Scope (Implemented)
- `config`: `init` (user/project config template generation)
- `launcher/role`: `list`, `show`
- `launcher`: `probe`
- `prompt`: `render` (manager)
- `controller`: `init`, `attach`, `status`, `recover`, `release`
- `worker`: `start`, `list`, `show`, `inspect`, `stop`, `handoff`, `continue`
- turn operations: `send`, `watch`, `wait`, `approve`, `cancel`
- `subagentd`: `run`, `status`
- normalized event journal and approval queue
- default `config init` template includes ACP launchers for `codex`, `claude-code`, `gemini`, `opencode`, `cline`, `github-copilot`, `kiro`
- persistent `acp-stdio` worker runtime:
  - `worker start` launches runtime (`initialize` -> `session/new`)
  - restart path attempts `session/load` using stored `sessionId`, then falls back to `session/new`
  - `send` uses runtime IPC to execute `session/prompt`
  - `cancel` propagates to runtime (`session/cancel`)
  - live permission flow pauses on `session/request_permission` and resumes via `approve`
  - runtime IPC retries once with auto-restart when runtime socket is unreachable
- explicit local simulation mode (`--debug-mode` or `--request-approval`)
- handoff store with `handoff.md` + `checkpoint.json`
- `--input` JSON contract (major commands) with duplicate-field rejection
- owner handle model: `controllerId + epoch + token`
- workspace-scoped runtime state by default: `<workspace-root>/.subagent/state` (or `SUBAGENT_STATE_DIR` override)
- project-local hint: `<workspace>/.subagent/controller.json`
- versioned envelope for JSON responses

## Current Limitations
- local single-host control plane only (no multi-host orchestration/scheduling)
- no queued turn execution; `send` is rejected while worker is busy
- session resume is best-effort (`session/load` fallback to `session/new`, no guaranteed bit-perfect resurrection)
- commands that need implicit state path fail with `WORKSPACE_ROOT_NOT_FOUND` when workspace root cannot be inferred
