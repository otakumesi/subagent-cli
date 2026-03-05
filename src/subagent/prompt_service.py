"""Prompt rendering helpers for manager target."""

from __future__ import annotations

from typing import Any

from .config import SubagentConfig
from .constants import DEFAULT_WAIT_TIMEOUT_SECONDS, DEFAULT_WAIT_UNTIL

MANAGER_PROMPT_BASE = f"""You are a manager agent coordinating worker subagents with `subagent` CLI.

Read this quick workflow first:
1. Initialize controller in the workspace:
   subagent controller init --cwd <workspace>
2. Start a worker:
   subagent worker start --cwd <workspace> [--role <role>]
3. Send work and wait for a terminal-or-approval event:
   subagent send --worker-id <worker-id> --text "<instruction>" --json
4. If approval is requested:
   subagent approve --worker-id <worker-id> --request <request-id> --option-id <option-id>
5. Use watch only when detailed event streaming is needed:
   subagent watch --worker-id <worker-id> --follow --ndjson

Operational rules:
- Keep instructions short, concrete, and outcome-oriented.
- Use `--json` for machine-readable responses.
- For multiline or shell-sensitive text, prefer `--text-file` or `--text-stdin` over inline `--text`.
- Prefer `send` for task dispatch when you need a single round-trip result (`--no-wait` for fire-and-return).
- For long-running turns, set no-progress guards (`--wait-no-progress-timeout-seconds` on `send`, `--no-progress-timeout-seconds` on `wait`).
- If you need manual waits, use `wait` defaults (`--until {DEFAULT_WAIT_UNTIL}`, `--timeout-seconds {DEFAULT_WAIT_TIMEOUT_SECONDS:.0f}`), and add `--include-history` when you need to match past events.
- Ensure the runtime has required permissions for the chosen launcher (including network access when needed).
- Treat `waiting_approval` as a blocking state; resolve via `approve` or `cancel`.
- Use handoff flow for continuation: `worker handoff` -> `worker continue`.
- Prefer strict mode for production; use `--debug-mode` only for local simulation/testing.
"""

def _render_role_hints_section(config: SubagentConfig) -> str:
    lines: list[str] = []
    lines.append("Role hints:")
    lines.append("- Role names are hints only; any custom role name is allowed.")
    defaults_role = config.defaults.get("role")
    if isinstance(defaults_role, str) and defaults_role.strip():
        lines.append(f"- Default role: `{defaults_role.strip()}`")
    defaults_launcher = config.defaults.get("launcher")
    if isinstance(defaults_launcher, str) and defaults_launcher.strip():
        lines.append(f"- Fallback launcher: `{defaults_launcher.strip()}`")
    lines.append(
        "- Language defaults: "
        f"prompt=`{config.role_defaults.prompt_language}`, "
        f"response=`{config.role_defaults.response_language}`"
    )
    if not config.role_hints:
        lines.append("- No role hints defined in config.")
        return "\n".join(lines)
    lines.append("- Example role hints (not exhaustive):")
    for name in sorted(config.role_hints.keys()):
        role_hint = config.role_hints[name]
        hint_parts: list[str] = []
        if isinstance(role_hint.preferred_launcher, str) and role_hint.preferred_launcher.strip():
            hint_parts.append(f"preferredLauncher=`{role_hint.preferred_launcher.strip()}`")
        if isinstance(role_hint.prompt_language, str) and role_hint.prompt_language.strip():
            hint_parts.append(f"promptLanguage=`{role_hint.prompt_language.strip()}`")
        if isinstance(role_hint.response_language, str) and role_hint.response_language.strip():
            hint_parts.append(f"responseLanguage=`{role_hint.response_language.strip()}`")
        suffix = ", ".join(hint_parts) if hint_parts else "(no overrides)"
        lines.append(f"  - `{name}`: {suffix}")
        if isinstance(role_hint.delegation_hint, str) and role_hint.delegation_hint.strip():
            lines.append(f"    delegationHint: {role_hint.delegation_hint.strip()}")
        if role_hint.recommended_skills:
            joined_skills = ", ".join(f"`{skill}`" for skill in role_hint.recommended_skills)
            lines.append(f"    recommendedSkills: {joined_skills}")
    return "\n".join(lines)


def render_prompt(config: SubagentConfig) -> dict[str, Any]:
    prompt = MANAGER_PROMPT_BASE.rstrip()
    prompt = f"{prompt}\n\n{_render_role_hints_section(config)}"
    return {
        "target": "manager",
        "prompt": prompt,
    }
