# subagent-cli

[![PyPI version](https://img.shields.io/pypi/v/subagent-cli)](https://pypi.org/project/subagent-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/subagent-cli)](https://pypi.org/project/subagent-cli/)
[![License](https://img.shields.io/github/license/otakumesi/subagent-cli)](LICENSE)
[![Publish to PyPI](https://github.com/otakumesi/subagent-cli/actions/workflows/publish-pypi.yml/badge.svg)](https://github.com/otakumesi/subagent-cli/actions/workflows/publish-pypi.yml)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-orange)](https://pypi.org/project/subagent-cli/)

Orchestrate worker agents from a parent controller, cleanly and safely.  
`subagent-cli` gives manager agents (for example Codex or Claude Code) a practical control plane for starting workers, sending turns, handling approvals, and continuing handoffs. 🤖

The command interface is protocol-agnostic, and the current runtime backend is ACP-based (`acp-stdio`).

## Why subagent-cli? 🚀
- Run multi-agent workflows from one place (`worker start` to `worker continue`).
- Keep control explicit with strict approval operations and structured event flow.
- Recover cleanly with runtime restart + session resume (`session/load`).
- Stay local-first and scriptable with a single CLI surface.

## Current Scope 🧭
- Alpha (`v0.1.x`)
- Local single-host focused
- Python 3.11+

## Features ✨
- Worker lifecycle: `start`, `list`, `show`, `inspect`, `stop`
- Turn operations: `send`, `watch`, `wait`, `approve`, `cancel`
- Handoff workflow: `worker handoff` and `worker continue`
- Strict approval flow with structured events
- ACP runtime integration (`acp-stdio`)

## Install 📦
With `uv` (recommended):
```bash
uv tool install subagent-cli
```

From PyPI:
```bash
pip install subagent-cli
```

From local artifacts:
```bash
pip install dist/subagent_cli-*.whl
```

## Quick Start ⚡
1. Bootstrap your local config.
```bash
subagent config init --scope user
```

2. Set launcher command/args/env in `~/.config/subagent/config.yaml`.

3. Initialize a controller in your workspace.
```bash
subagent controller init --cwd .
```

4. Render manager guidance.
```bash
subagent prompt render --target manager
```

5. Hand off from here to your manager agent (Codex / Claude Code).  
Use this instruction template:

```text
Act as the project manager for this repository.
Use subagent-cli as the control plane and progress this task by delegating to worker agents.

Required workflow:
1) Read and follow the output of `subagent prompt render --target manager`.
2) Check command help before execution (for example `subagent worker --help`, `subagent send --help`, `subagent approve --help`).
3) Break the task into small executable chunks.
4) Start/coordinate workers with subagent-cli.
5) Use send/watch/wait/approve to drive each turn.
6) Use handoff/continue when context gets large.
7) Verify results (tests or checks) before reporting completion.

Task to execute:
<your task here>
```

After handoff, the manager agent is expected to run the normal CLI lifecycle:
`worker start` -> `send` -> `watch` -> `approve` -> `handoff` -> `continue`

For local simulation/testing without a real ACP launcher:
```bash
subagent worker start --cwd . --debug-mode
```

## Troubleshooting 🛠️
- Ensure both the runtime and your manager/worker agent sandbox allow what your launcher needs.
- Some launchers require outbound network access, but agent sandbox policies can block network even when the host machine itself has connectivity.
- Preflight launcher availability:
```bash
subagent launcher probe <launcher-name> --json
```
- If `worker start` fails with `BACKEND_UNAVAILABLE`, inspect runtime logs under `~/.local/share/subagent/runtimes/` (or `$SUBAGENT_STATE_DIR/runtimes/` when overridden).
- For cut-down local testing without backend connectivity:
```bash
subagent worker start --cwd . --debug-mode
```

## Configuration ⚙️
- Resolution order: `--config` > `SUBAGENT_CONFIG` > nearest `<cwd-or-parent>/.subagent/config.yaml` > `~/.config/subagent/config.yaml`
- Generate user config: `subagent config init --scope user`
- Generate project config: `subagent config init --scope project --cwd .`
- Override config path: `SUBAGENT_CONFIG=/path/to/config.yaml`
- Example config: [config.example.yaml](config.example.yaml)

## State 💾
- Default state DB: `~/.local/share/subagent/state.db`
- Project hint file: `<workspace>/.subagent/controller.json`

## Documentation 📚
- Architecture note: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Examples: [docs/examples](docs/examples)
- Contributing and release process: [CONTRIBUTING.md](CONTRIBUTING.md)

## License
MIT ([LICENSE](LICENSE))
