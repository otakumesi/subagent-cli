# subagent-cli

Initial v1 implementation scaffold based on `DEV_PLAN.md` and `SOFTWARE_DESIGN.md`.

## Install
- From PyPI (after publish):
`pip install subagent-cli`
- Local wheel install:
`pip install dist/subagent_cli-*.whl`

## Implemented commands
- `subagent launcher list/show`
- `subagent launcher probe`
- `subagent profile list/show`
- `subagent pack list/show`
- `subagent prompt render --target manager|worker`
- `subagent controller init/attach/status/recover/release`
- `subagent worker start/list/show/inspect/stop/handoff/continue`
- `subagent send/watch/wait/approve/cancel`
- `subagentd run/status` (minimal local daemon bootstrap)

## Backend execution
- `worker start` in strict mode launches a persistent per-worker ACP runtime process.
  - launcher backend must be `acp-stdio`
  - launcher command must be available (PATH or existing absolute path)
- Runtime initializes ACP once (`initialize` + `session/new`) and stores runtime endpoint metadata (`runtimePid`, `runtimeSocket`) in worker state.
- When the runtime is restarted, it first tries `session/load` with the stored `sessionId`; if resume is not available, it falls back to `session/new`.
- `send` supports `--debug-mode/--no-debug-mode`:
  - default (`--no-debug-mode`): strict runtime path; fails with `BACKEND_UNAVAILABLE` when runtime is unavailable
  - `--debug-mode`: local simulation mode
- In strict mode, runtime IPC (`send`/`approve`/`cancel`) retries once after automatic runtime restart when the socket is unreachable.
- Live ACP permission requests are surfaced as `approval.requested` and resume only after explicit `approve`.
- `--request-approval` remains a local waiting-approval simulation path for tests/debug.

## Tests
- Run with:
`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v`

## Contributing
- See [CONTRIBUTING.md](CONTRIBUTING.md) for development and release guidance.

## Input Contract
- Major commands support `--input <json-file|->`
- Duplicate field input from both flags and `--input` is rejected

## Config
- Default path: `~/.config/subagent/config.yaml`
- Override with: `SUBAGENT_CONFIG=/path/to/config.yaml`
- Starter config: `config.example.yaml`

## State
- Default state DB: `~/.local/share/subagent/state.db`
- Project hint file: `<workspace>/.subagent/controller.json`
