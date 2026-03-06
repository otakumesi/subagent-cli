# subagent-cli

Language: **English** | [日本語](README.ja.md)

[![PyPI version](https://img.shields.io/pypi/v/subagent-cli)](https://pypi.org/project/subagent-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/subagent-cli)](https://pypi.org/project/subagent-cli/)
[![License](https://img.shields.io/github/license/otakumesi/subagent-cli)](LICENSE)
[![Publish to PyPI](https://github.com/otakumesi/subagent-cli/actions/workflows/publish-pypi.yml/badge.svg)](https://github.com/otakumesi/subagent-cli/actions/workflows/publish-pypi.yml)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-orange)](https://pypi.org/project/subagent-cli/)

Orchestrate coding agents from another coding agent, cleanly and safely.  
`subagent-cli` turns a manager coding agent (for example Codex or Claude Code) into a practical control plane for starting worker coding agents, sending turns, handling approvals, and continuing handoffs. 🤖

The command interface is protocol-agnostic, and the current runtime backend is ACP-based (`acp-stdio`).

## Why subagent-cli? 🚀
Coordinating multiple coding agents is harder than it looks.
Many tools centralize this behind a single `/subagent`-style command.
But in real workflows, you may want to use different agents for different roles.

subagent-cli exists for that split.
It separates responsibilities and routes communication through ACP to keep multi-agent collaboration simple and explicit.

For example, you can use Claude Code as the manager, start Codex for code review, and start Gemini for implementation.

## Concrete Use Cases 🧪
- Flaky test investigation: split reproduction, root-cause analysis, and fix proposals across multiple workers.
- Parallel code review / research workers: run reviewer-focused and research-focused workers in parallel, then merge outputs in the manager turn.
- Parent crash -> handoff -> continue: recover from manager interruption by resuming from handoff context instead of restarting from scratch.

Note: the sample prompt in Quick Start is project-manager oriented. For the use cases above, adjust manager role instructions and worker prompts to match your workflow.

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

## Terminal Demo 🎬
A real `Gemini -> Codex` workflow, with Gemini as manager and Codex as worker.

[![asciicast](https://asciinema.org/a/813713.svg)](https://asciinema.org/a/813713)

## Quick Start ⚡
1. Bootstrap your local config.
```bash
subagent config init --scope user
```

2. Set launcher command/args/env in `~/.config/subagent/config.yaml`.

3. Initialize a controller in your workspace (run once per workspace, by you or your manager agent).
```bash
subagent controller init --cwd .
```

4. Install the recommended manager skill (after this repository is pushed to GitHub).
```bash
npx skills add "github/otakumesi/subagent-cli"
```

5. Hand off from here to your manager agent (Codex / Claude Code).  
Use an instruction like this:

```text
<what to do that you want>

Use subagent commands to delegate tasks to a team of agents and coordinate their progress.
Use the "subagent-manager" skill.
```

After handoff, the manager agent's standard lifecycle is:
`worker start` -> `send` -> (`approve` -> `send` as needed) -> `handoff` -> `continue`

For a single command that sends and waits for terminal-or-approval events:
```bash
subagent send --worker-id <worker-id> --text "<instruction>" --json
```

Opt out of waiting when needed:
```bash
subagent send --worker-id <worker-id> --text "<instruction>" --no-wait --json
```

For multiline or shell-safe input (recommended):
```bash
cat > instruction.txt <<'TEXT'
Use commands like `echo hello` literally; do not execute them.
TEXT
subagent send --worker-id <worker-id> --text-file ./instruction.txt --json
```

stdin variant:
```bash
cat ./instruction.txt | subagent send --worker-id <worker-id> --text-stdin --json
```

Advanced automation can still use structured JSON input via `--input` (JSON key: `workerId`; CLI flag: `--worker-id`).

Manual wait mode (advanced cursor control) still exists:
```bash
subagent wait --worker-id <worker-id> --until turn_end --timeout-seconds 60 --json
```

To include historical events in matching:
```bash
subagent wait --worker-id <worker-id> --include-history --until turn_end --timeout-seconds 60 --json
```

For local simulation/testing without a real ACP launcher:
```bash
subagent worker start --cwd . --debug-mode
```

## Troubleshooting 🛠️
- Ensure both the runtime and your manager/worker agent sandbox allow what your launcher needs.
- Some launchers require outbound network access, but agent sandbox policies can block network even when the host machine itself has connectivity.
- If state path resolution fails, run commands from inside your workspace root (or set `SUBAGENT_STATE_DIR` explicitly).
- Preflight launcher availability:
```bash
subagent launcher probe <launcher-name> --json
```
- If `worker start` fails with a backend error (for example `BACKEND_TIMEOUT`, `BACKEND_SOCKET_UNREACHABLE`, `BACKEND_LAUNCHER`), inspect runtime logs under `<workspace>/.subagent/state/runtimes/` (default) or `$SUBAGENT_STATE_DIR/runtimes/` (when overridden).
- For cut-down local testing without backend connectivity:
```bash
subagent worker start --cwd . --debug-mode
```

## Configuration ⚙️
- Resolution order: `--config` > `SUBAGENT_CONFIG` > nearest `<cwd-or-parent>/.subagent/config.yaml` > `~/.config/subagent/config.yaml`
- Generate user config: `subagent config init --scope user`
- Generate project config: `subagent config init --scope project --cwd .`
- `config init` defaults: `codex` -> `npx -y @zed-industries/codex-acp`, `claude-code` -> `npx -y @zed-industries/claude-agent-acp`, `gemini` -> `npx -y @google/gemini-cli --experimental-acp`, `opencode` -> `opencode acp`, `cline` -> `npx -y cline --acp`, `github-copilot` -> `npx -y @github/copilot-language-server --acp`, `kiro` -> `npx -y @kirodotdev/cli acp`
- Override config path: `SUBAGENT_CONFIG=/path/to/config.yaml`
- Example config: [config.example.yaml](config.example.yaml)
- Launchers support either split style (`command: npx`, `args: ["-y", "..."]`) or inline style (`command: "npx -y ..."`) for probe/start/restart.

## State 💾
- Default state DB: `<workspace>/.subagent/state/state.db`
- Override state dir: `SUBAGENT_STATE_DIR=/path/to/state-dir`
- If workspace root cannot be detected and no override is set, commands fail with `WORKSPACE_ROOT_NOT_FOUND`
- Project hint file: `<workspace>/.subagent/controller.json`

## Documentation 📚
- Architecture note: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Examples: [docs/examples](docs/examples)
- Contributing and release process: [CONTRIBUTING.md](CONTRIBUTING.md)

## License
MIT ([LICENSE](LICENSE))
