"""Helpers for `--input` JSON contract and duplicate field protection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import SubagentError


def load_input_payload(input_path: str | None) -> dict[str, Any] | None:
    if input_path is None:
        return None
    if input_path == "-":
        import sys

        raw = sys.stdin.read()
    else:
        raw = Path(input_path).read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SubagentError(
            code="INVALID_INPUT",
            message="--input must contain valid JSON",
            details={"error": str(error)},
        ) from error
    if not isinstance(payload, dict):
        raise SubagentError(
            code="INVALID_INPUT",
            message="--input JSON must be an object",
        )
    return payload


def reject_duplicates(
    payload: dict[str, Any] | None,
    *,
    flag_values: dict[str, Any],
    value_is_default: dict[str, bool],
    mapping: dict[str, str],
) -> None:
    if payload is None:
        return
    for json_field, flag_name in mapping.items():
        if json_field not in payload:
            continue
        if flag_name not in flag_values:
            continue
        if value_is_default.get(flag_name, True):
            continue
        raise SubagentError(
            code="INVALID_INPUT",
            message=f"Field `{json_field}` is provided by both --input and flags.",
            details={"field": json_field, "flag": flag_name},
        )


def read_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SubagentError(
            code="INVALID_INPUT",
            message=f"`{key}` must be a string",
        )
    return value


def read_bool(payload: dict[str, Any], key: str) -> bool | None:
    if key not in payload:
        return None
    value = payload[key]
    if not isinstance(value, bool):
        raise SubagentError(
            code="INVALID_INPUT",
            message=f"`{key}` must be a boolean",
        )
    return value


def read_string_list(payload: dict[str, Any], key: str) -> list[str] | None:
    if key not in payload:
        return None
    value = payload[key]
    if not isinstance(value, list):
        raise SubagentError(
            code="INVALID_INPUT",
            message=f"`{key}` must be a list of strings",
        )
    out: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str):
            raise SubagentError(
                code="INVALID_INPUT",
                message=f"`{key}[{idx}]` must be a string",
            )
        out.append(item)
    return out


def read_blocks(payload: dict[str, Any], key: str = "blocks") -> list[dict[str, Any]] | None:
    if key not in payload:
        return None
    value = payload[key]
    if not isinstance(value, list):
        raise SubagentError(
            code="INVALID_INPUT",
            message=f"`{key}` must be a list",
        )
    blocks: list[dict[str, Any]] = []
    for idx, item in enumerate(value):
        if not isinstance(item, dict):
            raise SubagentError(
                code="INVALID_INPUT",
                message=f"`{key}[{idx}]` must be an object",
            )
        blocks.append(item)
    return blocks
