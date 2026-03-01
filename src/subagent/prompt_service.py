"""Prompt rendering helpers for manager/worker targets."""

from __future__ import annotations

from typing import Any

from .config import Pack, Profile, SubagentConfig
from .errors import SubagentError

MANAGER_PROMPT_BASE = """You are a manager agent coordinating worker subagents.
Use `subagent` CLI commands to delegate, monitor, and continue work safely.
Prefer short, actionable instructions and preserve handoff context."""


def _render_worker_prompt(profile: Profile, packs: list[Pack]) -> str:
    lines: list[str] = []
    if profile.bootstrap.strip():
        lines.append(profile.bootstrap.strip())
    else:
        lines.append("You are a worker subagent.")
    lines.append("Use STATUS:, ASK:, BLOCKED:, and DONE: prefixes when helpful.")
    lines.append("Keep updates concise and action-oriented.")
    if packs:
        lines.append("")
        lines.append("Additional pack instructions:")
        for pack in packs:
            lines.append(f"- Pack `{pack.name}`: {pack.description or '(no description)'}")
            if pack.prompt.strip():
                lines.append(pack.prompt.strip())
    return "\n".join(lines).strip()


def render_prompt(
    config: SubagentConfig,
    *,
    target: str,
    profile_name: str | None = None,
    pack_names: list[str] | None = None,
) -> dict[str, Any]:
    if target not in {"manager", "worker"}:
        raise SubagentError(
            code="INVALID_ARGUMENT",
            message=f"Unknown prompt target: {target}",
            details={"target": target},
        )

    if target == "manager":
        return {
            "target": "manager",
            "prompt": MANAGER_PROMPT_BASE,
        }

    selected_profile_name = profile_name
    if selected_profile_name is None:
        default_profile = config.defaults.get("profile")
        if isinstance(default_profile, str) and default_profile:
            selected_profile_name = default_profile
    if selected_profile_name is None:
        raise SubagentError(
            code="PROFILE_NOT_FOUND",
            message="Profile is required for worker prompt rendering.",
        )
    profile = config.profiles.get(selected_profile_name)
    if profile is None:
        raise SubagentError(
            code="PROFILE_NOT_FOUND",
            message=f"Profile not found: {selected_profile_name}",
            details={"profile": selected_profile_name},
        )

    selected_pack_names = list(pack_names or [])
    if not selected_pack_names:
        selected_pack_names = list(profile.default_packs)
        if not selected_pack_names:
            defaults_packs = config.defaults.get("packs")
            if isinstance(defaults_packs, list):
                selected_pack_names = [str(item) for item in defaults_packs]

    packs: list[Pack] = []
    for pack_name in selected_pack_names:
        pack = config.packs.get(pack_name)
        if pack is None:
            raise SubagentError(
                code="PACK_NOT_FOUND",
                message=f"Pack not found: {pack_name}",
                details={"pack": pack_name},
            )
        packs.append(pack)

    prompt = _render_worker_prompt(profile, packs)
    return {
        "target": "worker",
        "profile": profile.name,
        "packs": [pack.name for pack in packs],
        "prompt": prompt,
    }
