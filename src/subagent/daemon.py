"""Minimal local daemon process for subagent runtime bootstrap."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from .config import load_config
from .constants import DAEMON_STATUS_PATH
from .errors import SubagentError
from .output import emit_json
from .paths import resolve_state_dir
from .runtime_service import restart_worker_runtime, runtime_request
from .state import WORKER_STATE_STOPPED, StateStore

app = typer.Typer(help="subagentd: local control-plane daemon")


def _utc_now() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat()


def _status_path_for_state_dir(state_dir: Path) -> Path:
    default_parent = DAEMON_STATUS_PATH.parent
    if state_dir == default_parent:
        return DAEMON_STATUS_PATH
    return state_dir / "subagentd-status.json"


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _check_worker_health(
    store: StateStore,
    *,
    restart_attempts: dict[str, float],
    restart_cooldown_seconds: float,
) -> dict[str, Any]:
    summary: dict[str, int] = {
        "checked": 0,
        "healthy": 0,
        "unhealthy": 0,
        "restarted": 0,
        "restartFailed": 0,
    }
    items: list[dict[str, Any]] = []
    workers = store.list_workers()

    config = None
    config_error: dict[str, Any] | None = None
    try:
        config = load_config()
    except SubagentError as error:
        config_error = error.to_dict()

    now_mono = time.monotonic()
    for worker in workers:
        worker_id = str(worker["worker_id"])
        state = str(worker["state"])
        runtime_socket = worker.get("runtime_socket")
        runtime_pid = worker.get("runtime_pid")

        if state == WORKER_STATE_STOPPED:
            continue
        if not isinstance(runtime_socket, str) or not runtime_socket:
            continue

        summary["checked"] += 1
        pid_alive: bool | None = None
        if isinstance(runtime_pid, int):
            try:
                os.kill(runtime_pid, 0)
            except OSError:
                pid_alive = False
            else:
                pid_alive = True

        socket_exists = Path(runtime_socket).exists()
        reachable = False
        ping_error: dict[str, Any] | None = None
        if socket_exists and pid_alive is not False:
            try:
                runtime_request(
                    store,
                    worker_id=worker_id,
                    method="ping",
                    params={},
                    timeout_seconds=0.5,
                )
            except SubagentError as error:
                ping_error = error.to_dict()
            else:
                reachable = True

        if reachable:
            summary["healthy"] += 1
            items.append(
                {
                    "workerId": worker_id,
                    "state": state,
                    "healthy": True,
                }
            )
            continue

        summary["unhealthy"] += 1
        reason = {
            "pidAlive": pid_alive,
            "socketExists": socket_exists,
            "pingError": ping_error,
        }

        last_attempt = restart_attempts.get(worker_id)
        cooling_down = (
            last_attempt is not None and (now_mono - last_attempt) < restart_cooldown_seconds
        )
        if cooling_down:
            items.append(
                {
                    "workerId": worker_id,
                    "state": state,
                    "healthy": False,
                    "restarted": False,
                    "reason": reason,
                    "cooldown": True,
                }
            )
            continue

        restart_attempts[worker_id] = now_mono
        if config is None:
            summary["restartFailed"] += 1
            items.append(
                {
                    "workerId": worker_id,
                    "state": state,
                    "healthy": False,
                    "restarted": False,
                    "reason": reason,
                    "error": config_error
                    or {
                        "code": "CONFIG_NOT_AVAILABLE",
                        "message": "Config not available for runtime restart.",
                    },
                }
            )
            continue

        try:
            restarted = restart_worker_runtime(store, config, worker_id=worker_id, timeout_seconds=8.0)
        except SubagentError as error:
            summary["restartFailed"] += 1
            items.append(
                {
                    "workerId": worker_id,
                    "state": state,
                    "healthy": False,
                    "restarted": False,
                    "reason": reason,
                    "error": error.to_dict(),
                }
            )
            continue

        summary["restarted"] += 1
        items.append(
            {
                "workerId": worker_id,
                "state": state,
                "healthy": False,
                "restarted": True,
                "reason": reason,
                "runtime": restarted,
            }
        )

    return {
        "checkedAt": _utc_now(),
        "summary": summary,
        "items": items,
    }


@app.command("run")
def run_daemon(
    once: bool = typer.Option(
        False,
        "--once",
        help="Initialize state and exit immediately (bootstrap mode).",
    ),
    heartbeat_seconds: int = typer.Option(
        5,
        "--heartbeat-seconds",
        min=1,
        help="Heartbeat update interval when running foreground.",
    ),
    monitor_workers: bool = typer.Option(
        True,
        "--monitor-workers/--no-monitor-workers",
        help="Enable worker runtime health checks and restart attempts.",
    ),
    restart_cooldown_seconds: int = typer.Option(
        30,
        "--restart-cooldown-seconds",
        min=1,
        help="Minimum seconds between restart attempts per worker.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON response."),
) -> None:
    state_dir = resolve_state_dir()
    store = StateStore(state_dir / "state.db")
    store.bootstrap()
    status_path = _status_path_for_state_dir(state_dir)
    restart_attempts: dict[str, float] = {}
    worker_health = (
        _check_worker_health(
            store,
            restart_attempts=restart_attempts,
            restart_cooldown_seconds=float(restart_cooldown_seconds),
        )
        if monitor_workers
        else {
            "checkedAt": _utc_now(),
            "summary": {
                "checked": 0,
                "healthy": 0,
                "unhealthy": 0,
                "restarted": 0,
                "restartFailed": 0,
            },
            "items": [],
        }
    )
    base_payload = {
        "schemaVersion": "v1",
        "pid": os.getpid(),
        "stateDir": str(state_dir),
        "dbPath": str(store.db_path),
        "startedAt": _utc_now(),
        "lastHeartbeatAt": _utc_now(),
        "mode": "once" if once else "foreground",
        "monitorWorkers": monitor_workers,
        "workerHealth": worker_health,
    }
    _write_status(status_path, base_payload)

    if once:
        if json_output:
            emit_json(base_payload)
        else:
            typer.echo(f"subagentd initialized: {store.db_path}")
        return

    if json_output:
        emit_json(base_payload)
    else:
        typer.echo(f"subagentd running (pid={base_payload['pid']})")
    try:
        while True:
            time.sleep(heartbeat_seconds)
            base_payload["lastHeartbeatAt"] = _utc_now()
            if monitor_workers:
                base_payload["workerHealth"] = _check_worker_health(
                    store,
                    restart_attempts=restart_attempts,
                    restart_cooldown_seconds=float(restart_cooldown_seconds),
                )
            _write_status(status_path, base_payload)
    except KeyboardInterrupt:
        base_payload["stoppedAt"] = _utc_now()
        _write_status(status_path, base_payload)
        raise typer.Exit(code=0)


@app.command("status")
def daemon_status(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON response."),
) -> None:
    state_dir = resolve_state_dir()
    status_path = _status_path_for_state_dir(state_dir)
    if not status_path.exists():
        payload = {
            "schemaVersion": "v1",
            "running": False,
            "stateDir": str(state_dir),
            "statusFile": str(status_path),
        }
        if json_output:
            emit_json(payload)
        else:
            typer.echo("subagentd not initialized")
        raise typer.Exit(code=1)
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    pid = payload.get("pid")
    running = False
    if isinstance(pid, int):
        try:
            os.kill(pid, 0)
        except OSError:
            running = False
        else:
            running = True
    payload["running"] = running
    if json_output:
        emit_json(payload)
    else:
        typer.echo(
            f"subagentd running={running} pid={payload.get('pid')} "
            f"startedAt={payload.get('startedAt')}"
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
