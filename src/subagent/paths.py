"""Filesystem path resolution helpers."""

from __future__ import annotations

import os
from pathlib import Path

from .constants import (
    DEFAULT_CONFIG_PATH,
    ENV_CONFIG_PATH,
    ENV_STATE_DIR,
    PROJECT_HINT_DIRNAME,
    PROJECT_HINT_FILENAME,
)
from .errors import SubagentError


def resolve_workspace_path(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()).expanduser().resolve()


def _discover_project_config_path(cwd: Path | None = None) -> Path | None:
    start = resolve_workspace_path(cwd)
    for current in [start, *start.parents]:
        candidate = current / PROJECT_HINT_DIRNAME / "config.yaml"
        if candidate.exists():
            return candidate
    return None


def _discover_workspace_root_path(cwd: Path | None = None) -> Path | None:
    start = resolve_workspace_path(cwd)
    for current in [start, *start.parents]:
        if (current / PROJECT_HINT_DIRNAME / "config.yaml").exists():
            return current
        if (current / PROJECT_HINT_DIRNAME / PROJECT_HINT_FILENAME).exists():
            return current
        if (current / ".git").exists():
            return current
    return None


def resolve_workspace_root_path(cwd: Path | None = None) -> Path:
    discovered = _discover_workspace_root_path(cwd)
    if discovered is not None:
        return discovered
    raise SubagentError(
        code="WORKSPACE_ROOT_NOT_FOUND",
        message=(
            "Workspace root could not be determined. "
            "Run from inside a workspace, pass --cwd where supported, "
            "or set SUBAGENT_STATE_DIR."
        ),
        details={"cwd": str(resolve_workspace_path(cwd))},
    )


def resolve_config_path(config_path: Path | None = None, *, prefer_project: bool = True) -> Path:
    if config_path is not None:
        return config_path.expanduser().resolve()
    env_value = os.environ.get(ENV_CONFIG_PATH)
    if env_value:
        return Path(env_value).expanduser().resolve()
    if prefer_project:
        discovered = _discover_project_config_path()
        if discovered is not None:
            return discovered
    return DEFAULT_CONFIG_PATH


def resolve_state_dir(state_dir: Path | None = None, *, workspace: Path | None = None) -> Path:
    if state_dir is not None:
        return state_dir.expanduser().resolve()
    env_value = os.environ.get(ENV_STATE_DIR)
    if env_value:
        return Path(env_value).expanduser().resolve()
    workspace_root = resolve_workspace_path(workspace) if workspace is not None else resolve_workspace_root_path()
    return workspace_root / PROJECT_HINT_DIRNAME / "state"


def resolve_state_db_path(state_dir: Path | None = None, *, workspace: Path | None = None) -> Path:
    resolved_state_dir = resolve_state_dir(state_dir, workspace=workspace)
    return resolved_state_dir / "state.db"


def resolve_handoffs_dir(state_dir: Path | None = None, *, workspace: Path | None = None) -> Path:
    resolved_state_dir = resolve_state_dir(state_dir, workspace=workspace)
    return resolved_state_dir / "handoffs"


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def project_hint_path(workspace: Path) -> Path:
    return workspace / PROJECT_HINT_DIRNAME / PROJECT_HINT_FILENAME
