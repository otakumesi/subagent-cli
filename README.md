# subagent-cli

`subagent-cli` is a protocol-agnostic CLI for orchestrating worker agents from a parent controller.

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
