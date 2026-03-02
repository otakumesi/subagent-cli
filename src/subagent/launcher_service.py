"""Launcher helper operations (e.g., probe)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import shlex
from typing import Any

from .config import Launcher, SubagentConfig
from .errors import SubagentError


@dataclass(slots=True)
class ResolvedLauncherSpec:
    command: str
    args: list[str]
    available: bool
    resolved_path: str | None
    command_was_tokenized: bool


def resolve_launcher_spec(launcher: Launcher) -> ResolvedLauncherSpec:
    raw_command = launcher.command.strip()
    if not raw_command:
        raise SubagentError(
            code="CONFIG_PARSE_ERROR",
            message=f"`launchers.{launcher.name}.command` must be a non-empty string",
            details={"launcher": launcher.name},
        )
    try:
        command_tokens = shlex.split(raw_command)
    except ValueError:
        # Fall back to the raw command so probing/startup errors remain visible to users.
        command_tokens = [raw_command]

    command = command_tokens[0]
    args = [str(token) for token in command_tokens[1:]] + [str(arg) for arg in launcher.args]
    command_was_tokenized = len(command_tokens) > 1

    resolved_path: str | None = None
    available = False
    if "/" in command:
        resolved_candidate = Path(command).expanduser()
        if resolved_candidate.exists():
            resolved_path = str(resolved_candidate.resolve())
            available = True
    else:
        resolved_path = shutil.which(command)
        available = resolved_path is not None

    return ResolvedLauncherSpec(
        command=command,
        args=args,
        available=available,
        resolved_path=resolved_path,
        command_was_tokenized=command_was_tokenized,
    )


def probe_launcher(config: SubagentConfig, launcher_name: str) -> dict[str, Any]:
    launcher = config.launchers.get(launcher_name)
    if launcher is None:
        raise SubagentError(
            code="LAUNCHER_NOT_FOUND",
            message=f"Launcher not found: {launcher_name}",
            details={"launcher": launcher_name},
        )
    resolved = resolve_launcher_spec(launcher)
    return {
        "name": launcher.name,
        "backendKind": launcher.backend_kind,
        "command": launcher.command,
        "resolvedCommandPath": resolved.resolved_path,
        "available": resolved.available,
        "args": launcher.args,
        "effectiveCommand": resolved.command,
        "effectiveArgs": resolved.args,
        "commandWasTokenized": resolved.command_was_tokenized,
        "envKeys": sorted(launcher.env.keys()),
    }
