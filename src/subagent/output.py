"""Output helpers for human and JSON responses."""

from __future__ import annotations

import json
from typing import Any, NoReturn

import typer

from .constants import SCHEMA_VERSION
from .errors import SubagentError


def ok_envelope(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "ok": True,
        "type": event_type,
        "data": data,
    }


def error_envelope(error: SubagentError) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "ok": False,
        "type": "error",
        "error": error.to_dict(),
    }


def emit_json(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def emit_error_and_exit(error: SubagentError, json_output: bool = False) -> NoReturn:
    if json_output:
        emit_json(error_envelope(error))
    else:
        typer.echo(f"{error.code}: {error.message}", err=True)
        details = error.details
        if isinstance(details, dict):
            recommended_action = details.get("recommendedAction")
            if isinstance(recommended_action, str) and recommended_action:
                typer.echo(f"Hint: {recommended_action}", err=True)
            recommended_command = details.get("recommendedCommand")
            if isinstance(recommended_command, str) and recommended_command:
                typer.echo(f"Try: {recommended_command}", err=True)
    raise typer.Exit(code=1)
