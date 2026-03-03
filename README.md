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

4. Hand off from here to your manager agent (Codex / Claude Code).  
Ask the manager agent to run `subagent prompt render --target manager` as its first step.  
Use this instruction template:

```text
Act as the manager and product lead for this repository.

Use subagent-cli as the control plane and progress this task by coordinating a small team of worker agents rather than acting as a solo implementer.

Your role:

* Define the objective, user value, constraints, scope, and success criteria.
* Break the work into small chunks with clear owners.
* Assign worker roles intentionally.
* Coordinate handoffs, reviews, and validation.
* Review worker proposals critically and give actionable feedback.
* Make explicit decisions on whether to proceed, revise, compare options, or reject.

Default behavior:

* Delegate by default when useful.
* Prefer small, verifiable increments.
* Prevent over-engineering and keep the team focused on the smallest valuable outcome.
* Do not accept worker output blindly; evaluate it for product fit, feasibility, scope, risk, and validation quality.

Before execution:

1. Run `subagent prompt render --target manager` and follow that output.
2. Check command help before execution (`subagent worker --help`, `subagent send --help`, `subagent approve --help`).
3. State:
   * your role
   * planned worker roles
   * task breakdown
   * success criteria
   * validation plan

During execution:

4. Start and coordinate workers with subagent-cli.
5. In every worker instruction, explicitly state which skills the worker should use for the task before implementation.
6. Use `send` as the default turn driver (`send` waits by default).
7. If `matchedEvent.type` is `approval.requested`, run `approve` and continue with `send`.
8. Use `watch` only when detailed event streaming or debugging is needed.
9. Use handoff/continue when context gets large.
10. Require workers to report: goal, findings, proposal, risks, validation, next step.
11. Respond to worker proposals with: decision, reason, what is good, what is missing, what should change, and next action.

Before reporting completion:

12. Verify results with tests or checks.
13. Confirm the final output is integrated, validated, and aligned with the task objective.

If worker startup or turn operations fail due to sandbox limits, request approval for out-of-sandbox execution and retry.

Task to execute:
<your task here>
```

After handoff, the manager agent's standard lifecycle is:
`worker start` -> `send` -> (`approve` -> `send` as needed) -> `handoff` -> `continue`

For a single command that sends and waits for terminal-or-approval events:
```bash
subagent send --worker <worker-id> --text "<instruction>" --json
```

Opt out of waiting when needed:
```bash
subagent send --worker <worker-id> --text "<instruction>" --no-wait --json
```

For multiline or shell-safe input (recommended):
```bash
cat > instruction.txt <<'TEXT'
Use commands like `echo hello` literally; do not execute them.
TEXT
subagent send --worker <worker-id> --text-file ./instruction.txt --json
```

stdin variant:
```bash
cat ./instruction.txt | subagent send --worker <worker-id> --text-stdin --json
```

Advanced automation can still use structured JSON input via `--input` (`workerId` required).

Manual wait mode (advanced cursor control) still exists:
```bash
subagent wait --worker <worker-id> --until turn_end --timeout-seconds 60 --json
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
- If `worker start` fails with `BACKEND_UNAVAILABLE`, inspect runtime logs under `<workspace>/.subagent/state/runtimes/` (default) or `$SUBAGENT_STATE_DIR/runtimes/` (when overridden).
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
