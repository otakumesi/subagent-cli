"""Project-local controller hint read/write helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .constants import SCHEMA_VERSION
from .paths import project_hint_path


def read_project_hint(workspace: Path) -> dict[str, Any] | None:
    hint_path = project_hint_path(workspace)
    if not hint_path.exists():
        return None
    try:
        payload = json.loads(hint_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def write_project_hint(workspace: Path, *, controller_id: str, label: str) -> Path:
    hint_path = project_hint_path(workspace)
    hint_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "controllerId": controller_id,
        "label": label,
        "workspaceKey": str(workspace),
    }
    hint_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return hint_path
