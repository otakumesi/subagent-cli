# subagent-cli

`subagent-cli` is a protocol-agnostic CLI for orchestrating worker agents from a parent controller.
In practice, it is a control-plane CLI that a manager agent (for example Codex or Claude Code) can use to launch and coordinate other worker agents.
The CLI surface is protocol-agnostic, while the current runtime implementation is ACP-based (`acp-stdio`).

## Status
- Alpha (`v0.1.x`)
- Local single-host focused
- Python 3.11+

## Features
- Worker lifecycle: start, list, show, inspect, stop
- Turn operations: send, watch, wait, approve, cancel
- Handoff workflow: `worker handoff` and `worker continue`
- Strict approval flow with structured events
- ACP runtime integration (`acp-stdio`) with runtime restart + session resume (`session/load`)

## Install
- From PyPI:
`pip install subagent-cli`
- From local artifacts:
`pip install dist/subagent_cli-*.whl`

## Quick Start
1. Prepare config:
`mkdir -p ~/.config/subagent && cp config.example.yaml ~/.config/subagent/config.yaml`
2. Initialize a controller in your workspace:
`subagent controller init --cwd .`
3. Start a worker:
`subagent worker start --cwd .`
4. Send an instruction:
`subagent send --worker <worker-id> --text "Investigate failing tests"`
5. Watch events:
`subagent watch --worker <worker-id> --ndjson`

For local simulation/testing without a real ACP launcher:
`subagent worker start --cwd . --debug-mode`

## Troubleshooting
- Ensure the runtime has the permissions required by your launcher. Some launchers need outbound network access.
- Preflight launcher availability:
`subagent launcher probe <launcher-name> --json`
- If `worker start` fails with `BACKEND_UNAVAILABLE`, inspect runtime logs under:
`~/.local/share/subagent/runtimes/` (or `$SUBAGENT_STATE_DIR/runtimes/` when overridden)
- For local cut-down testing without backend connectivity, use:
`subagent worker start --cwd . --debug-mode`

## Configuration
- Default config path: `~/.config/subagent/config.yaml`
- Override config path: `SUBAGENT_CONFIG=/path/to/config.yaml`
- Example config: [config.example.yaml](config.example.yaml)

## State
- Default state DB: `~/.local/share/subagent/state.db`
- Project hint file: `<workspace>/.subagent/controller.json`

## Documentation
- Architecture note: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Examples: [docs/examples](docs/examples)
- Contributing and release process: [CONTRIBUTING.md](CONTRIBUTING.md)

## License
MIT ([LICENSE](LICENSE))
