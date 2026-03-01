# subagent v1 Architecture Memo

## Layers
- CLI surface: `subagent` Typer commands for controller/config operations.
- Local control plane: `subagentd` minimal bootstrap/status process.
- Runtime state: sqlite-backed `StateStore` for controller ownership.
- Config registry: loader for `launchers` / `profiles` / `packs` from config.

## Current v1 Scope (Implemented)
- `launcher/profile/pack`: `list`, `show`
- `launcher`: `probe`
- `prompt`: `render` (manager/worker)
- `controller`: `init`, `attach`, `status`, `recover`, `release`
- `worker`: `start`, `list`, `show`, `inspect`, `stop`, `handoff`, `continue`
- turn operations: `send`, `watch`, `wait`, `approve`, `cancel`
- normalized event journal and approval queue
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
- project-local hint: `<workspace>/.subagent/controller.json`
- versioned envelope for JSON responses

## Current Limitations
- local single-host control plane only (`subagentd` is minimal bootstrap/status)
- no queued turn execution; `send` is rejected while worker is busy
- resume strategy is handoff-first (no bit-perfect backend session resurrection)
