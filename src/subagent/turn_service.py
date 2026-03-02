"""Turn operations over worker runtime state and event journal."""

from __future__ import annotations

import time
import uuid
from typing import Any

from .approval_utils import resolve_option
from .config import SubagentConfig
from .constants import DEFAULT_WAIT_TIMEOUT_SECONDS, RUNTIME_STARTUP_TIMEOUT_SECONDS
from .errors import SubagentError
from .runtime_service import restart_worker_runtime, runtime_request
from .state import (
    WORKER_STATE_IDLE,
    WORKER_STATE_RUNNING,
    WORKER_STATE_WAITING_APPROVAL,
    StateStore,
)

WAIT_EVENT_TYPES: set[str] = {
    "turn.started",
    "message.sent",
    "progress.message",
    "progress.update",
    "approval.requested",
    "approval.decided",
    "turn.completed",
    "turn.failed",
    "turn.canceled",
}
WAIT_UNTIL_ALIASES: dict[str, set[str]] = {
    "turn_end": {
        "turn.completed",
        "turn.failed",
        "turn.canceled",
        "approval.requested",
    },
}


def _normalize_event(event: dict[str, Any], *, include_raw: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schemaVersion": "v1",
        "eventId": event["event_id"],
        "ts": event["ts"],
        "workerId": event["worker_id"],
        "type": event["event_type"],
        "data": event["data"],
    }
    turn_id = event.get("turn_id")
    if turn_id:
        payload["turnId"] = turn_id
    if include_raw and event.get("raw") is not None:
        payload["raw"] = event["raw"]
    return payload


def _ensure_worker_sendable(worker: dict[str, Any]) -> None:
    state = str(worker["state"])
    if state != WORKER_STATE_IDLE:
        raise SubagentError(
            code="WORKER_BUSY",
            message="worker has an active turn",
            retryable=False,
            details={"state": state},
        )


def _parse_until_set(until: str | None) -> set[str]:
    if until is None:
        return set()
    trimmed = until.strip()
    if not trimmed or trimmed in {"*", "any"}:
        return set()
    raw_parts = [part.strip() for part in trimmed.split(",") if part.strip()]
    if not raw_parts:
        return set()
    if any(part in {"*", "any"} for part in raw_parts):
        return set()
    expanded: set[str] = set()
    unknown: list[str] = []
    for part in raw_parts:
        alias_target = WAIT_UNTIL_ALIASES.get(part)
        if alias_target is not None:
            expanded.update(alias_target)
            continue
        if part in WAIT_EVENT_TYPES:
            expanded.add(part)
            continue
        unknown.append(part)
    if unknown:
        raise SubagentError(
            code="INVALID_ARGUMENT",
            message="`until` contains unknown event type(s)",
            details={
                "unknown": sorted(set(unknown)),
                "aliases": sorted(WAIT_UNTIL_ALIASES.keys()),
                "knownEventTypes": sorted(WAIT_EVENT_TYPES),
            },
        )
    return expanded


def _begin_turn(
    store: StateStore,
    *,
    worker_id: str,
    text: str,
    blocks: list[dict[str, Any]] | None,
    runtime_kind: str,
) -> tuple[str, dict[str, Any]]:
    turn_id = f"turn_{uuid.uuid4().hex[:10]}"
    store.update_worker_state(worker_id, next_state=WORKER_STATE_RUNNING)
    store.set_worker_active_turn(worker_id, turn_id)

    store.append_worker_event(
        worker_id,
        event_type="turn.started",
        turn_id=turn_id,
        data={
            "turnId": turn_id,
            "input": {
                "text": text,
                "blocksCount": len(blocks or []),
            },
        },
        raw={"runtime": runtime_kind, "phase": "turn.started"},
    )
    message_event = store.append_worker_event(
        worker_id,
        event_type="message.sent",
        turn_id=turn_id,
        data={
            "text": text,
            "blocks": blocks or [],
        },
        raw={"runtime": runtime_kind, "phase": "message.sent"},
    )
    return turn_id, message_event


def _complete_turn(
    store: StateStore,
    *,
    worker_id: str,
    turn_id: str,
    runtime_kind: str,
    outcome: str = "completed",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store.update_worker_state(worker_id, next_state=WORKER_STATE_IDLE)
    payload = {
        "turnId": turn_id,
        "outcome": outcome,
        "state": WORKER_STATE_IDLE,
    }
    if details:
        payload.update(details)
    return store.append_worker_event(
        worker_id,
        event_type="turn.completed",
        turn_id=turn_id,
        data=payload,
        raw={"runtime": runtime_kind, "phase": "turn.completed"},
    )


def _simulate_send_message(
    store: StateStore,
    *,
    worker_id: str,
    text: str,
    blocks: list[dict[str, Any]] | None,
    request_approval: bool,
) -> dict[str, Any]:
    turn_id, message_event = _begin_turn(
        store,
        worker_id=worker_id,
        text=text,
        blocks=blocks,
        runtime_kind="local",
    )
    if request_approval:
        request = store.create_approval_request(
            worker_id,
            turn_id=turn_id,
            message=f"Approval requested for turn {turn_id}",
        )
        store.update_worker_state(worker_id, next_state=WORKER_STATE_WAITING_APPROVAL)
        approval_event = store.append_worker_event(
            worker_id,
            event_type="approval.requested",
            turn_id=turn_id,
            data={
                "requestId": request["request_id"],
                "kind": request["kind"],
                "message": request["message"],
                "options": request["options"],
            },
            raw={"runtime": "local", "phase": "approval.requested"},
        )
        return {
            "workerId": worker_id,
            "turnId": turn_id,
            "state": WORKER_STATE_WAITING_APPROVAL,
            "requestId": request["request_id"],
            "eventId": approval_event["event_id"],
            "acceptedEventId": message_event["event_id"],
        }

    store.append_worker_event(
        worker_id,
        event_type="progress.message",
        turn_id=turn_id,
        data={
            "role": "assistant",
            "text": "STATUS: turn accepted and completed in local runtime.",
        },
        raw={"runtime": "local", "phase": "progress.message"},
    )
    completed_event = _complete_turn(
        store,
        worker_id=worker_id,
        turn_id=turn_id,
        runtime_kind="local",
    )
    return {
        "workerId": worker_id,
        "turnId": turn_id,
        "state": WORKER_STATE_IDLE,
        "eventId": completed_event["event_id"],
        "acceptedEventId": message_event["event_id"],
    }


def _send_via_runtime(
    store: StateStore,
    *,
    worker_id: str,
    text: str,
    blocks: list[dict[str, Any]] | None,
    config: SubagentConfig | None,
) -> dict[str, Any]:
    turn_id, message_event = _begin_turn(
        store,
        worker_id=worker_id,
        text=text,
        blocks=blocks,
        runtime_kind="acp-stdio",
    )
    try:
        runtime_result = _runtime_request_with_restart(
            store,
            config=config,
            worker_id=worker_id,
            method="start_turn",
            params={
                "turnId": turn_id,
                "text": text,
                "blocks": blocks or [],
            },
            timeout_seconds=3600.0,
        )
    except SubagentError as error:
        store.append_worker_event(
            worker_id,
            event_type="turn.failed",
            turn_id=turn_id,
            data={"turnId": turn_id, "error": error.to_dict()},
            raw={"runtime": "acp-stdio", "phase": "turn.failed"},
        )
        store.update_worker_state(
            worker_id,
            next_state=WORKER_STATE_IDLE,
            last_error=error.message,
        )
        raise

    state = runtime_result.get("state")
    if state == WORKER_STATE_WAITING_APPROVAL:
        request_id = runtime_result.get("requestId")
        if not isinstance(request_id, str):
            raise SubagentError(
                code="BACKEND_PROTOCOL_ERROR",
                message="Runtime response missing approval requestId.",
                details={"response": runtime_result},
            )
        latest_event = store.get_latest_worker_event(worker_id)
        return {
            "workerId": worker_id,
            "turnId": turn_id,
            "state": WORKER_STATE_WAITING_APPROVAL,
            "requestId": request_id,
            "eventId": latest_event["event_id"] if latest_event else None,
            "acceptedEventId": message_event["event_id"],
        }
    if state != WORKER_STATE_IDLE:
        raise SubagentError(
            code="BACKEND_PROTOCOL_ERROR",
            message="Runtime returned an unknown state.",
            details={"response": runtime_result},
        )
    event_id = runtime_result.get("eventId")
    if not isinstance(event_id, str):
        latest_event = store.get_latest_worker_event(worker_id)
        event_id = str(latest_event["event_id"]) if latest_event is not None else ""
    return {
        "workerId": worker_id,
        "turnId": turn_id,
        "state": WORKER_STATE_IDLE,
        "eventId": event_id,
        "acceptedEventId": message_event["event_id"],
    }


def _runtime_request_with_restart(
    store: StateStore,
    *,
    config: SubagentConfig | None,
    worker_id: str,
    method: str,
    params: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    try:
        return runtime_request(
            store,
            worker_id=worker_id,
            method=method,
            params=params,
            timeout_seconds=timeout_seconds,
        )
    except SubagentError as error:
        if error.code != "BACKEND_UNAVAILABLE" or config is None:
            raise
    restart_worker_runtime(
        store,
        config,
        worker_id=worker_id,
        timeout_seconds=RUNTIME_STARTUP_TIMEOUT_SECONDS,
    )
    return runtime_request(
        store,
        worker_id=worker_id,
        method=method,
        params=params,
        timeout_seconds=timeout_seconds,
    )


def send_message(
    store: StateStore,
    *,
    worker_id: str,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
    request_approval: bool = False,
    config: SubagentConfig | None = None,
    execution_mode: str = "strict",
) -> dict[str, Any]:
    worker = store.get_worker(worker_id)
    if worker is None:
        raise SubagentError(
            code="WORKER_NOT_FOUND",
            message=f"Worker not found: {worker_id}",
            details={"workerId": worker_id},
        )
    _ensure_worker_sendable(worker)
    if execution_mode not in {"strict", "simulate"}:
        raise SubagentError(
            code="INVALID_ARGUMENT",
            message=f"Unknown execution mode: {execution_mode}",
            details={"executionMode": execution_mode},
        )
    if request_approval:
        return _simulate_send_message(
            store,
            worker_id=worker_id,
            text=text,
            blocks=blocks,
            request_approval=True,
        )
    if execution_mode == "simulate":
        return _simulate_send_message(
            store,
            worker_id=worker_id,
            text=text,
            blocks=blocks,
            request_approval=False,
        )
    return _send_via_runtime(
        store,
        worker_id=worker_id,
        text=text,
        blocks=blocks,
        config=config,
    )


def watch_events(
    store: StateStore,
    *,
    worker_id: str,
    from_event_id: str | None = None,
    follow: bool = False,
    timeout_seconds: float = 1.0,
    include_raw: bool = False,
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    cursor = from_event_id
    if not follow:
        events = store.list_worker_events(worker_id, from_event_id=cursor)
        return [_normalize_event(event, include_raw=include_raw) for event in events]

    deadline = time.monotonic() + timeout_seconds
    while True:
        events = store.list_worker_events(worker_id, from_event_id=cursor)
        if events:
            cursor = str(events[-1]["event_id"])
            collected.extend(_normalize_event(event, include_raw=include_raw) for event in events)
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    return collected


def wait_for_event(
    store: StateStore,
    *,
    worker_id: str,
    until: str,
    from_event_id: str | None = None,
    timeout_seconds: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if timeout_seconds < 0:
        raise SubagentError(
            code="INVALID_ARGUMENT",
            message="`timeoutSeconds` must be >= 0",
            details={"timeoutSeconds": timeout_seconds},
        )
    until_set = _parse_until_set(until)
    deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None
    cursor = from_event_id
    while True:
        events = store.list_worker_events(worker_id, from_event_id=cursor)
        if events:
            for event in events:
                event_type = str(event["event_type"])
                if not until_set or event_type in until_set:
                    return _normalize_event(event)
            cursor = str(events[-1]["event_id"])
        if deadline is not None and time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    worker = store.get_worker(worker_id)
    worker_state = str(worker.get("state")) if worker is not None else "unknown"
    latest_event_payload: dict[str, Any] | None = None
    if worker is not None:
        latest_event = store.get_latest_worker_event(worker_id)
        if latest_event is not None:
            latest_event_payload = {
                "eventId": latest_event["event_id"],
                "type": latest_event["event_type"],
                "turnId": latest_event.get("turn_id"),
                "ts": latest_event["ts"],
            }
    raise SubagentError(
        code="WAIT_TIMEOUT",
        message=f"No event matched `{until}` before timeout",
        retryable=True,
        details={
            "workerId": worker_id,
            "until": until,
            "timeoutSeconds": timeout_seconds,
            "workerState": worker_state,
            "latestEvent": latest_event_payload,
        },
    )


def find_last_assistant_message(
    store: StateStore,
    *,
    worker_id: str,
    turn_id: str | None,
    from_event_id: str | None = None,
) -> str | None:
    events = store.list_worker_events(worker_id, from_event_id=from_event_id)
    for event in reversed(events):
        if turn_id is not None and str(event.get("turn_id") or "") != turn_id:
            continue
        if str(event.get("event_type")) != "progress.message":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        if data.get("role") != "assistant":
            continue
        text = data.get("text")
        if isinstance(text, str) and text:
            return text
    return None


def cancel_turn(
    store: StateStore,
    *,
    worker_id: str,
    reason: str | None = None,
    config: SubagentConfig | None = None,
) -> dict[str, Any]:
    worker = store.get_worker(worker_id)
    if worker is None:
        raise SubagentError(
            code="WORKER_NOT_FOUND",
            message=f"Worker not found: {worker_id}",
            details={"workerId": worker_id},
        )
    state = str(worker["state"])
    if state not in {WORKER_STATE_RUNNING, WORKER_STATE_WAITING_APPROVAL}:
        raise SubagentError(
            code="WORKER_NOT_RUNNING",
            message="worker has no active turn to cancel",
            details={"workerId": worker_id, "state": state},
        )
    runtime_socket = worker.get("runtime_socket")
    if isinstance(runtime_socket, str) and runtime_socket:
        return _runtime_request_with_restart(
            store,
            config=config,
            worker_id=worker_id,
            method="cancel_turn",
            params={"reason": reason or "canceled by manager"},
            timeout_seconds=120.0,
        )
    turn_id = worker.get("active_turn_id")
    store.update_worker_state(worker_id, next_state="canceling")
    canceled_event = store.append_worker_event(
        worker_id,
        event_type="turn.canceled",
        turn_id=str(turn_id) if turn_id else None,
        data={
            "turnId": turn_id,
            "reason": reason or "canceled by manager",
        },
        raw={"runtime": "local", "phase": "turn.canceled"},
    )
    store.update_worker_state(worker_id, next_state=WORKER_STATE_IDLE)
    return {
        "workerId": worker_id,
        "state": WORKER_STATE_IDLE,
        "eventId": canceled_event["event_id"],
        "turnId": turn_id,
    }


def approve_request(
    store: StateStore,
    *,
    worker_id: str,
    request_id: str,
    decision: str | None = None,
    option_id: str | None = None,
    alias: str | None = None,
    note: str | None = None,
    config: SubagentConfig | None = None,
) -> dict[str, Any]:
    worker = store.get_worker(worker_id)
    if worker is None:
        raise SubagentError(
            code="WORKER_NOT_FOUND",
            message=f"Worker not found: {worker_id}",
            details={"workerId": worker_id},
        )
    runtime_socket = worker.get("runtime_socket")
    if isinstance(runtime_socket, str) and runtime_socket:
        return _runtime_request_with_restart(
            store,
            config=config,
            worker_id=worker_id,
            method="approve",
            params={
                "requestId": request_id,
                "decision": decision,
                "optionId": option_id,
                "alias": alias,
                "note": note,
            },
            timeout_seconds=120.0,
        )
    if str(worker["state"]) != WORKER_STATE_WAITING_APPROVAL:
        raise SubagentError(
            code="WORKER_NOT_WAITING_APPROVAL",
            message="worker is not waiting for approval",
            details={"workerId": worker_id, "state": worker["state"]},
        )
    request = store.get_approval_request(worker_id, request_id)
    if request is None:
        raise SubagentError(
            code="APPROVAL_NOT_FOUND",
            message=f"Approval request not found: {request_id}",
            details={"workerId": worker_id, "requestId": request_id},
        )
    selected_option_id, selected_alias, resolved_decision = resolve_option(
        request,
        decision=decision,
        option_id=option_id,
        alias=alias,
    )
    decided = store.decide_approval_request(
        worker_id,
        request_id,
        decision=resolved_decision,
        selected_option_id=selected_option_id,
        selected_alias=selected_alias,
        note=note,
    )
    turn_id = request.get("turn_id")
    store.append_worker_event(
        worker_id,
        event_type="approval.decided",
        turn_id=str(turn_id) if turn_id else None,
        data={
            "requestId": request_id,
            "decision": resolved_decision,
            "optionId": selected_option_id,
            "alias": selected_alias,
            "note": note,
        },
        raw={"runtime": "local", "phase": "approval.decided"},
    )
    store.update_worker_state(worker_id, next_state=WORKER_STATE_RUNNING)
    outcome = "approved" if selected_option_id in {"allow", "approve", "yes"} else "rejected"
    completed_event = store.append_worker_event(
        worker_id,
        event_type="turn.completed",
        turn_id=str(turn_id) if turn_id else None,
        data={
            "turnId": turn_id,
            "outcome": outcome,
            "state": WORKER_STATE_IDLE,
        },
        raw={"runtime": "local", "phase": "turn.completed"},
    )
    store.update_worker_state(worker_id, next_state=WORKER_STATE_IDLE)
    return {
        "workerId": worker_id,
        "requestId": request_id,
        "decision": decided["decision"],
        "optionId": decided["selected_option_id"],
        "alias": decided["selected_alias"],
        "state": WORKER_STATE_IDLE,
        "eventId": completed_event["event_id"],
    }
