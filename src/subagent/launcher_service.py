"""Launcher helper operations (e.g., probe)."""

from __future__ import annotations

import shutil
from typing import Any

from .config import SubagentConfig
from .errors import SubagentError


def probe_launcher(config: SubagentConfig, launcher_name: str) -> dict[str, Any]:
    launcher = config.launchers.get(launcher_name)
    if launcher is None:
        raise SubagentError(
            code="LAUNCHER_NOT_FOUND",
            message=f"Launcher not found: {launcher_name}",
            details={"launcher": launcher_name},
        )
    command_path = shutil.which(launcher.command)
    available = command_path is not None
    return {
        "name": launcher.name,
        "backendKind": launcher.backend_kind,
        "command": launcher.command,
        "resolvedCommandPath": command_path,
        "available": available,
        "args": launcher.args,
        "envKeys": sorted(launcher.env.keys()),
    }
