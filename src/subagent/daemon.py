"""Minimal local daemon process for subagent runtime bootstrap."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from .constants import DAEMON_STATUS_PATH
from .output import emit_json
from .paths import resolve_state_dir
from .state import StateStore

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
    json_output: bool = typer.Option(False, "--json", help="Emit JSON response."),
) -> None:
    state_dir = resolve_state_dir()
    store = StateStore(state_dir / "state.db")
    store.bootstrap()
    status_path = _status_path_for_state_dir(state_dir)
    base_payload = {
        "schemaVersion": "v1",
        "pid": os.getpid(),
        "stateDir": str(state_dir),
        "dbPath": str(store.db_path),
        "startedAt": _utc_now(),
        "lastHeartbeatAt": _utc_now(),
        "mode": "once" if once else "foreground",
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
