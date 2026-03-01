"""Shared helpers for approval option resolution."""

from __future__ import annotations

from typing import Any

from .errors import SubagentError


def resolve_option(
    request: dict[str, Any],
    *,
    decision: str | None,
    option_id: str | None,
    alias: str | None,
) -> tuple[str, str | None, str]:
    options = request.get("options")
    if not isinstance(options, list):
        options = []

    by_id: dict[str, dict[str, Any]] = {}
    by_alias: dict[str, dict[str, Any]] = {}
    for option in options:
        if not isinstance(option, dict):
            continue
        option_key = option.get("id")
        option_alias = option.get("alias")
        if isinstance(option_key, str):
            by_id[option_key] = option
        if isinstance(option_alias, str):
            by_alias[option_alias] = option

    selected: dict[str, Any] | None = None
    if option_id:
        selected = by_id.get(option_id)
    elif alias:
        selected = by_alias.get(alias)
    elif decision:
        selected = by_alias.get(decision) or by_id.get(decision)

    if selected is None:
        raise SubagentError(
            code="INVALID_APPROVAL_DECISION",
            message="Could not resolve approval option. Use --option-id or --alias.",
            details={
                "decision": decision,
                "optionId": option_id,
                "alias": alias,
            },
        )

    selected_option_id = str(selected.get("id"))
    selected_alias = selected.get("alias")
    selected_alias_value = str(selected_alias) if isinstance(selected_alias, str) else None
    resolved_decision = decision or selected_alias_value or selected_option_id
    return selected_option_id, selected_alias_value, resolved_decision

