"""Worker lifecycle service helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Launcher, SubagentConfig
from .controller_service import read_env_handle, resolve_controller_id
from .errors import SubagentError
from .launcher_service import resolve_launcher_spec
from .paths import resolve_workspace_path
from .runtime_service import launch_worker_runtime, stop_worker_runtime
from .state import WORKER_STATE_ERROR, StateStore

TERMINAL_TURN_EVENT_TYPES = ("turn.completed", "turn.failed", "turn.canceled")
ACTIVE_RUNTIME_STATES = {"running", "waiting_approval", "canceling"}


def resolve_worker_controller_id(
    store: StateStore,
    *,
    workspace: Path,
    explicit_controller_id: str | None = None,
) -> str:
    env_handle = read_env_handle()
    env_controller_id: str | None = None
    if env_handle is not None:
        if env_handle.get("valid") is False:
            raise SubagentError(
                code="INVALID_CONTROLLER_HANDLE",
                message="SUBAGENT_CTL_EPOCH is not a valid integer",
            )
        env_controller_id = str(env_handle["controllerId"])

    target_controller_id = explicit_controller_id or env_controller_id
    if target_controller_id is None:
        target_controller_id = resolve_controller_id(
            store,
            workspace,
            explicit_controller_id=None,
        )
    if target_controller_id is None:
        raise SubagentError(
            code="CONTROLLER_NOT_FOUND",
            message=(
                "Controller could not be resolved. Set SUBAGENT_CTL_* env vars, "
                "use --controller-id, or run `subagent controller init` first."
            ),
            details={"workspaceKey": str(workspace), "stateDbPath": str(store.db_path)},
        )

    if env_handle is not None and "epoch" in env_handle and "token" in env_handle:
        if env_controller_id != target_controller_id:
            raise SubagentError(
                code="INVALID_CONTROLLER_HANDLE",
                message="SUBAGENT_CTL_ID does not match target controller",
                details={
                    "envControllerId": env_controller_id,
                    "targetControllerId": target_controller_id,
                },
            )
        valid = store.validate_handle(
            target_controller_id,
            int(env_handle["epoch"]),
            str(env_handle["token"]),
        )
        if not valid:
            raise SubagentError(
                code="INVALID_CONTROLLER_HANDLE",
                message="Controller handle is stale or invalid",
                details={"controllerId": target_controller_id},
            )
    elif env_handle is not None:
        raise SubagentError(
            code="INVALID_CONTROLLER_HANDLE",
            message="Incomplete controller handle in environment",
            details={"env": env_handle},
        )

    controller = store.get_controller(target_controller_id)
    if controller is None:
        raise SubagentError(
            code="CONTROLLER_NOT_FOUND",
            message=(
                f"Controller not found: {target_controller_id}. "
                "If running from another directory, pass --cwd <workspace> "
                "or set SUBAGENT_STATE_DIR."
            ),
            details={
                "controllerId": target_controller_id,
                "workspaceKey": str(workspace),
                "stateDbPath": str(store.db_path),
            },
        )
    return target_controller_id


def _resolve_launcher(config: SubagentConfig, launcher: str | None) -> str:
    value = launcher
    if value is None:
        default_launcher = config.defaults.get("launcher")
        if isinstance(default_launcher, str) and default_launcher:
            value = default_launcher
    if value is None:
        raise SubagentError(
            code="LAUNCHER_NOT_FOUND",
            message="Launcher is required. Set --launcher or defaults.launcher in config.",
        )
    if value not in config.launchers:
        raise SubagentError(
            code="LAUNCHER_NOT_FOUND",
            message=f"Launcher not found: {value}",
            details={"launcher": value},
        )
    return value


def _resolve_profile(config: SubagentConfig, profile: str | None) -> str:
    value = profile
    if value is None:
        default_profile = config.defaults.get("profile")
        if isinstance(default_profile, str) and default_profile:
            value = default_profile
    if value is None:
        raise SubagentError(
            code="PROFILE_NOT_FOUND",
            message="Profile is required. Set --profile or defaults.profile in config.",
        )
    if value not in config.profiles:
        raise SubagentError(
            code="PROFILE_NOT_FOUND",
            message=f"Profile not found: {value}",
            details={"profile": value},
        )
    return value


def _resolve_packs(config: SubagentConfig, profile_name: str, packs: list[str]) -> list[str]:
    resolved: list[str] = []
    if packs:
        resolved = packs
    else:
        profile = config.profiles[profile_name]
        if profile.default_packs:
            resolved = list(profile.default_packs)
        else:
            defaults_packs = config.defaults.get("packs")
            if isinstance(defaults_packs, list):
                resolved = [str(item) for item in defaults_packs]
    for pack_name in resolved:
        if pack_name not in config.packs:
            raise SubagentError(
                code="PACK_NOT_FOUND",
                message=f"Pack not found: {pack_name}",
                details={"pack": pack_name},
            )
    return resolved


def start_worker(
    store: StateStore,
    config: SubagentConfig,
    *,
    workspace: Path,
    worker_cwd: Path,
    controller_id: str | None,
    launcher: str | None,
    profile: str | None,
    packs: list[str],
    label: str | None,
    debug_mode: bool = False,
) -> dict[str, Any]:
    resolved_workspace = resolve_workspace_path(workspace)
    target_controller_id = resolve_worker_controller_id(
        store,
        workspace=resolved_workspace,
        explicit_controller_id=controller_id,
    )
    target_launcher = _resolve_launcher(config, launcher)
    target_profile = _resolve_profile(config, profile)
    target_packs = _resolve_packs(config, target_profile, packs)
    resolved_cwd = resolve_workspace_path(worker_cwd)
    resolved_label = label or "worker"

    worker = store.create_worker(
        controller_id=target_controller_id,
        launcher=target_launcher,
        profile=target_profile,
        packs=target_packs,
        cwd=str(resolved_cwd),
        label=resolved_label,
    )
    launcher_entry = config.launchers[target_launcher]
    if not debug_mode:
        if launcher_entry.backend_kind != "acp-stdio":
            raise SubagentError(
                code="BACKEND_LAUNCHER",
                message=f"Unsupported backend kind for runtime: {launcher_entry.backend_kind}",
                details={
                    "launcher": target_launcher,
                    "backendKind": launcher_entry.backend_kind,
                    "reasonCategory": "launcher",
                    "recommendedAction": "Use an `acp-stdio` launcher for worker runtime.",
                },
            )
        resolved = resolve_launcher_spec(launcher_entry)
        if not resolved.available:
            raise SubagentError(
                code="BACKEND_LAUNCHER",
                message=f"Launcher command not available: {launcher_entry.command}",
                details={
                    "launcher": target_launcher,
                    "command": launcher_entry.command,
                    "effectiveCommand": resolved.command,
                    "effectiveArgs": resolved.args,
                    "reasonCategory": "launcher",
                    "recommendedAction": (
                        "Install/fix the launcher command and run "
                        f"`subagent launcher probe {target_launcher} --json` before retrying."
                    ),
                },
            )
        runtime_launcher = Launcher(
            name=launcher_entry.name,
            backend_kind=launcher_entry.backend_kind,
            command=resolved.command,
            args=resolved.args,
            env=dict(launcher_entry.env),
        )
        try:
            launch_worker_runtime(
                store,
                worker_id=str(worker["worker_id"]),
                launcher=runtime_launcher,
                cwd=str(resolved_cwd),
            )
        except SubagentError as error:
            store.update_worker_state(
                str(worker["worker_id"]),
                next_state=WORKER_STATE_ERROR,
                allow_any_transition=True,
                last_error=error.message,
            )
            raise
        refreshed = store.get_worker(str(worker["worker_id"]))
        if refreshed is not None:
            worker = refreshed
    return {
        "workerId": worker["worker_id"],
        "controllerId": worker["controller_id"],
        "sessionId": worker["session_id"],
        "launcher": worker["launcher"],
        "profile": worker["profile"],
        "packs": worker["packs"],
        "cwd": worker["cwd"],
        "label": worker["label"],
        "state": worker["state"],
        "recoveryState": worker["recovery_state"],
        "runtimePid": worker.get("runtime_pid"),
        "runtimeSocket": worker.get("runtime_socket"),
        "createdAt": worker["created_at"],
    }


def list_workers(
    store: StateStore,
    *,
    controller_id: str | None = None,
) -> list[dict[str, Any]]:
    workers = [_resync_stale_runtime_state(store, row) for row in store.list_workers(controller_id=controller_id)]
    return [
        {
            "workerId": row["worker_id"],
            "controllerId": row["controller_id"],
            "label": row["label"],
            "launcher": row["launcher"],
            "profile": row["profile"],
            "state": row["state"],
            "cwd": row["cwd"],
            "sessionId": row["session_id"],
            "activeTurnId": row.get("active_turn_id"),
            "runtimePid": row.get("runtime_pid"),
            "runtimeSocket": row.get("runtime_socket"),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "stoppedAt": row["stopped_at"],
        }
        for row in workers
    ]


def show_worker(store: StateStore, worker_id: str) -> dict[str, Any]:
    worker = store.get_worker(worker_id)
    if worker is None:
        raise SubagentError(
            code="WORKER_NOT_FOUND",
            message=f"Worker not found: {worker_id}",
            details={"workerId": worker_id, "stateDbPath": str(store.db_path)},
        )
    worker = _resync_stale_runtime_state(store, worker)
    return {
        "workerId": worker["worker_id"],
        "controllerId": worker["controller_id"],
        "label": worker["label"],
        "launcher": worker["launcher"],
        "profile": worker["profile"],
        "packs": worker["packs"],
        "cwd": worker["cwd"],
        "sessionId": worker["session_id"],
        "activeTurnId": worker.get("active_turn_id"),
        "runtimePid": worker.get("runtime_pid"),
        "runtimeSocket": worker.get("runtime_socket"),
        "state": worker["state"],
        "recoveryState": worker["recovery_state"],
        "createdAt": worker["created_at"],
        "updatedAt": worker["updated_at"],
        "stoppedAt": worker["stopped_at"],
        "lastError": worker["last_error"],
    }


def _resync_stale_runtime_state(store: StateStore, row: dict[str, Any]) -> dict[str, Any]:
    state = str(row.get("state") or "")
    worker_id = str(row.get("worker_id") or "")
    if not worker_id or state not in ACTIVE_RUNTIME_STATES:
        return row
    active_turn_raw = row.get("active_turn_id")
    if not isinstance(active_turn_raw, str) or not active_turn_raw:
        return row
    terminal_events = store.list_worker_events(
        worker_id,
        turn_id=active_turn_raw,
        event_types=list(TERMINAL_TURN_EVENT_TYPES),
        limit=1,
        tail=True,
    )
    if not terminal_events:
        return row
    return store.update_worker_state(
        worker_id,
        next_state="idle",
        allow_any_transition=True,
    )


def stop_worker(store: StateStore, worker_id: str, *, force: bool = False) -> dict[str, Any]:
    worker = store.get_worker(worker_id)
    if worker is None:
        raise SubagentError(
            code="WORKER_NOT_FOUND",
            message=f"Worker not found: {worker_id}",
            details={"workerId": worker_id, "stateDbPath": str(store.db_path)},
        )
    if worker.get("runtime_socket"):
        stop_worker_runtime(store, worker_id=worker_id, reason="worker stopped by manager")
    worker = store.stop_worker(worker_id, force=force)
    return {
        "workerId": worker["worker_id"],
        "controllerId": worker["controller_id"],
        "state": worker["state"],
        "stoppedAt": worker["stopped_at"],
        "updatedAt": worker["updated_at"],
    }


def inspect_worker(
    store: StateStore,
    worker_id: str,
    *,
    events_limit: int = 20,
    since: str | None = None,
    turn_id: str | None = None,
    event_types: list[str] | None = None,
) -> dict[str, Any]:
    worker = show_worker(store, worker_id)
    row = store.get_worker(worker_id)
    assert row is not None
    pending_approvals = store.list_pending_approval_requests(worker_id)
    latest_handoff = store.get_latest_handoff_snapshot(worker_id)
    events = store.list_worker_events(
        worker_id,
        limit=events_limit,
        since=since,
        turn_id=turn_id,
        event_types=event_types,
        tail=True,
    )
    event_items = [
        {
            "eventId": event["event_id"],
            "ts": event["ts"],
            "type": event["event_type"],
            "turnId": event.get("turn_id"),
            "data": event["data"],
        }
        for event in events
    ]

    recovery_state = "restartable"
    if latest_handoff is not None:
        recovery_state = "handoff_available"
    if str(row["state"]) == "stopped" and latest_handoff is None:
        recovery_state = "lost"
    worker["recoveryState"] = recovery_state
    return {
        "worker": worker,
        "pendingApprovals": [
            {
                "requestId": req["request_id"],
                "turnId": req.get("turn_id"),
                "kind": req["kind"],
                "message": req["message"],
                "options": req["options"],
                "createdAt": req["created_at"],
            }
            for req in pending_approvals
        ],
        "latestHandoff": (
            {
                "snapshotId": latest_handoff["snapshot_id"],
                "handoffPath": latest_handoff["handoff_path"],
                "checkpointPath": latest_handoff["checkpoint_path"],
                "createdAt": latest_handoff["created_at"],
            }
            if latest_handoff is not None
            else None
        ),
        "events": event_items,
    }
