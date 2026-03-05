"""Handoff artifact generation and continue flow."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from .config import SubagentConfig
from .errors import SubagentError
from .paths import resolve_handoffs_dir, resolve_workspace_path
from .state import StateStore
from .turn_service import send_message
from .worker_service import start_worker


def _safe_text(value: Any, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _pick_task_from_events(events: list[dict[str, Any]]) -> str:
    for event in events:
        if str(event.get("event_type")) != "message.sent":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        text = data.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return "Continue the previous worker task safely."


def _pick_turn_id(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        turn_id = event.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            return turn_id
    return None


def _build_handoff_markdown(
    *,
    worker: dict[str, Any],
    task: str,
    completed_lines: list[str],
    pending_lines: list[str],
    risk_lines: list[str],
    handoff_path: Path,
    checkpoint_path: Path,
) -> str:
    files_of_interest = "- (not captured in v1 fallback handoff)"
    commands_run = "- (not captured in v1 fallback handoff)"
    completed = "\n".join(completed_lines or ["- No completed steps captured yet."])
    pending = "\n".join(pending_lines or ["- No explicit pending items captured."])
    risks = "\n".join(risk_lines or ["- No explicit risks captured."])
    recommended = (
        f"- Run `subagent worker continue --from-worker {worker['worker_id']}` to continue with a new worker."
    )
    artifacts = "\n".join(
        [
            f"- {handoff_path}",
            f"- {checkpoint_path}",
        ]
    )
    lines = [
        "# Handoff",
        "",
        "## Task",
        task,
        "",
        "## Goal",
        "Continue from the latest known context and complete the pending work safely.",
        "",
        "## Current Status",
        f"Worker `{worker['worker_id']}` is currently `{worker['state']}`.",
        "",
        "## Completed",
        completed,
        "",
        "## Pending",
        pending,
        "",
        "## Files of Interest",
        files_of_interest,
        "",
        "## Commands Run",
        commands_run,
        "",
        "## Risks / Notes",
        risks,
        "",
        "## Recommended Next Step",
        recommended,
        "",
        "## Artifacts",
        artifacts,
        "",
    ]
    return "\n".join(lines)


def create_handoff(
    store: StateStore,
    *,
    worker_id: str,
    handoffs_dir: Path | None = None,
) -> dict[str, Any]:
    worker = store.get_worker(worker_id)
    if worker is None:
        raise SubagentError(
            code="WORKER_NOT_FOUND",
            message=f"Worker not found: {worker_id}",
            details={"workerId": worker_id, "stateDbPath": str(store.db_path)},
        )
    events = store.list_worker_events(worker_id)
    task = _pick_task_from_events(events)
    source_turn_id = _pick_turn_id(events)

    completed_lines: list[str] = []
    pending_lines: list[str] = []
    for event in events:
        event_type = str(event.get("event_type"))
        turn_id = event.get("turn_id")
        if event_type == "turn.completed":
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            outcome = _safe_text(data.get("outcome"), "completed")
            completed_lines.append(f"- Turn `{turn_id}` completed with outcome `{outcome}`.")
        if event_type == "approval.requested":
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            request_id = _safe_text(data.get("requestId"), "unknown")
            pending_lines.append(f"- Approval decision pending for request `{request_id}`.")

    pending_requests = store.list_pending_approval_requests(worker_id)
    risk_lines = [
        f"- Pending approval request `{req['request_id']}` may block continuation."
        for req in pending_requests
    ]

    if handoffs_dir is None:
        # Keep handoff artifacts colocated with the active state DB.
        root = store.db_path.parent / "handoffs"
    else:
        root = resolve_handoffs_dir(handoffs_dir)
    snapshot_id = f"hs_{uuid.uuid4().hex[:10]}"
    snapshot_dir = root / worker_id / snapshot_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    handoff_path = snapshot_dir / "handoff.md"
    checkpoint_path = snapshot_dir / "checkpoint.json"

    markdown = _build_handoff_markdown(
        worker=worker,
        task=task,
        completed_lines=completed_lines,
        pending_lines=pending_lines,
        risk_lines=risk_lines,
        handoff_path=handoff_path,
        checkpoint_path=checkpoint_path,
    )
    handoff_path.write_text(markdown, encoding="utf-8")

    checkpoint = {
        "schemaVersion": "v1",
        "workerId": worker["worker_id"],
        "controllerId": worker["controller_id"],
        "launcher": worker["launcher"],
        "role": worker["role"],
        "cwd": worker["cwd"],
        "state": "handoff_ready",
        "sourceTurnId": source_turn_id,
        "handoffPath": str(handoff_path),
        "artifacts": [str(handoff_path)],
        "filesChanged": [],
    }
    checkpoint_path.write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    snapshot = store.register_handoff_snapshot(
        worker_id=worker_id,
        source_turn_id=source_turn_id,
        handoff_path=str(handoff_path),
        checkpoint_path=str(checkpoint_path),
    )
    return {
        "snapshotId": snapshot["snapshot_id"],
        "workerId": worker_id,
        "handoffPath": str(handoff_path),
        "checkpointPath": str(checkpoint_path),
        "sourceTurnId": source_turn_id,
    }


def _load_checkpoint_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def resolve_handoff_input(
    store: StateStore,
    *,
    from_worker: str | None,
    from_handoff: Path | None,
    handoffs_dir: Path | None = None,
) -> dict[str, Any]:
    if bool(from_worker) == bool(from_handoff):
        raise SubagentError(
            code="INVALID_ARGUMENT",
            message="Specify exactly one of --from-worker or --from-handoff.",
        )

    if from_worker:
        latest = store.get_latest_handoff_snapshot(from_worker)
        if latest is None:
            created = create_handoff(store, worker_id=from_worker, handoffs_dir=handoffs_dir)
            handoff_path = Path(created["handoffPath"]).expanduser().resolve()
            checkpoint_path = Path(created["checkpointPath"]).expanduser().resolve()
            checkpoint = _load_checkpoint_if_exists(checkpoint_path)
            return {
                "sourceWorkerId": from_worker,
                "handoffPath": handoff_path,
                "checkpointPath": checkpoint_path,
                "checkpoint": checkpoint,
            }
        handoff_path = Path(str(latest["handoff_path"])).expanduser().resolve()
        checkpoint_path = Path(str(latest["checkpoint_path"])).expanduser().resolve()
        checkpoint = _load_checkpoint_if_exists(checkpoint_path)
        return {
            "sourceWorkerId": from_worker,
            "handoffPath": handoff_path,
            "checkpointPath": checkpoint_path,
            "checkpoint": checkpoint,
        }

    assert from_handoff is not None
    handoff_path = from_handoff.expanduser().resolve()
    if not handoff_path.exists():
        raise SubagentError(
            code="HANDOFF_NOT_FOUND",
            message=f"Handoff file not found: {handoff_path}",
            details={"handoffPath": str(handoff_path)},
        )
    checkpoint_path = handoff_path.parent / "checkpoint.json"
    checkpoint = _load_checkpoint_if_exists(checkpoint_path)
    source_worker_id = checkpoint.get("workerId")
    source_worker = str(source_worker_id) if isinstance(source_worker_id, str) else None
    return {
        "sourceWorkerId": source_worker,
        "handoffPath": handoff_path,
        "checkpointPath": checkpoint_path if checkpoint_path.exists() else None,
        "checkpoint": checkpoint,
    }


def continue_worker(
    store: StateStore,
    config: SubagentConfig,
    *,
    from_worker: str | None,
    from_handoff: Path | None,
    launcher: str | None,
    role: str | None,
    cwd: Path | None,
    label: str | None,
    controller_id: str | None,
    handoffs_dir: Path | None = None,
    debug_mode: bool = False,
    execution_mode: str = "strict",
) -> dict[str, Any]:
    source = resolve_handoff_input(
        store,
        from_worker=from_worker,
        from_handoff=from_handoff,
        handoffs_dir=handoffs_dir,
    )
    checkpoint = source.get("checkpoint", {})
    if not isinstance(checkpoint, dict):
        checkpoint = {}

    source_handoff_path = Path(source["handoffPath"])
    source_worker_id = source.get("sourceWorkerId")
    checkpoint_launcher = checkpoint.get("launcher")
    checkpoint_role = checkpoint.get("role")
    if checkpoint_role is None:
        checkpoint_role = checkpoint.get("profile")
    checkpoint_cwd = checkpoint.get("cwd")
    checkpoint_controller = checkpoint.get("controllerId")

    target_launcher = launcher or (str(checkpoint_launcher) if isinstance(checkpoint_launcher, str) else None)
    target_role = role or (str(checkpoint_role) if isinstance(checkpoint_role, str) else None)

    target_cwd = resolve_workspace_path(cwd if cwd is not None else Path(str(checkpoint_cwd)) if isinstance(checkpoint_cwd, str) else Path.cwd())
    target_controller = controller_id or (
        str(checkpoint_controller) if isinstance(checkpoint_controller, str) else None
    )
    target_label = label or (
        f"continued-from-{source_worker_id}" if isinstance(source_worker_id, str) and source_worker_id else "continued-worker"
    )

    started = start_worker(
        store,
        config,
        workspace=target_cwd,
        worker_cwd=target_cwd,
        controller_id=target_controller,
        launcher=target_launcher,
        role=target_role,
        label=target_label,
        debug_mode=debug_mode,
    )

    prompt_text = (
        "Read the handoff document and continue from the previous worker context. "
        "Validate with the repository state and proceed with the next safe step."
    )
    blocks = [
        {
            "type": "resource_link",
            "resource": {
                "uri": source_handoff_path.as_uri(),
                "mimeType": "text/markdown",
            },
        }
    ]
    turn = send_message(
        store,
        worker_id=str(started["workerId"]),
        text=prompt_text,
        blocks=blocks,
        request_approval=False,
        config=config,
        execution_mode=execution_mode,
    )
    return {
        "sourceWorkerId": source_worker_id,
        "sourceHandoffPath": str(source_handoff_path),
        "checkpointPath": str(source["checkpointPath"]) if source.get("checkpointPath") else None,
        "worker": started,
        "bootstrapTurn": turn,
    }
