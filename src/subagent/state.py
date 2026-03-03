"""Runtime state store backed by SQLite."""

from __future__ import annotations

import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Iterator

from .errors import SubagentError
from .paths import resolve_state_db_path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS controllers (
    controller_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    workspace_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS controller_instances (
    instance_id TEXT PRIMARY KEY,
    controller_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    token TEXT NOT NULL,
    pid INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    released_at TEXT,
    FOREIGN KEY (controller_id) REFERENCES controllers(controller_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_controller_active_instance
ON controller_instances(controller_id)
WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    controller_id TEXT NOT NULL,
    label TEXT NOT NULL,
    launcher TEXT NOT NULL,
    profile TEXT NOT NULL,
    packs_json TEXT NOT NULL,
    cwd TEXT NOT NULL,
    session_id TEXT,
    runtime_pid INTEGER,
    runtime_socket TEXT,
    state TEXT NOT NULL,
    recovery_state TEXT NOT NULL,
    active_turn_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    stopped_at TEXT,
    last_error TEXT,
    FOREIGN KEY (controller_id) REFERENCES controllers(controller_id)
);

CREATE INDEX IF NOT EXISTS idx_workers_controller_id
ON workers(controller_id);

CREATE TABLE IF NOT EXISTS worker_events (
    event_id TEXT PRIMARY KEY,
    worker_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL,
    turn_id TEXT,
    data_json TEXT NOT NULL,
    raw_json TEXT,
    FOREIGN KEY (worker_id) REFERENCES workers(worker_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_worker_events_seq
ON worker_events(worker_id, event_seq);

CREATE INDEX IF NOT EXISTS idx_worker_events_ts
ON worker_events(worker_id, ts);

CREATE TABLE IF NOT EXISTS approval_requests (
    request_id TEXT PRIMARY KEY,
    worker_id TEXT NOT NULL,
    turn_id TEXT,
    status TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    options_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decision TEXT,
    selected_option_id TEXT,
    selected_alias TEXT,
    note TEXT,
    FOREIGN KEY (worker_id) REFERENCES workers(worker_id)
);

CREATE INDEX IF NOT EXISTS idx_approval_requests_worker_status
ON approval_requests(worker_id, status, created_at);

CREATE TABLE IF NOT EXISTS handoff_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    worker_id TEXT NOT NULL,
    controller_id TEXT NOT NULL,
    source_turn_id TEXT,
    handoff_path TEXT NOT NULL,
    checkpoint_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (worker_id) REFERENCES workers(worker_id),
    FOREIGN KEY (controller_id) REFERENCES controllers(controller_id)
);

CREATE INDEX IF NOT EXISTS idx_handoff_snapshots_worker_created
ON handoff_snapshots(worker_id, created_at DESC);
"""

WORKER_STATE_STARTING = "starting"
WORKER_STATE_IDLE = "idle"
WORKER_STATE_RUNNING = "running"
WORKER_STATE_WAITING_APPROVAL = "waiting_approval"
WORKER_STATE_CANCELING = "canceling"
WORKER_STATE_STOPPED = "stopped"
WORKER_STATE_ERROR = "error"

WORKER_RUNTIME_STATES = {
    WORKER_STATE_STARTING,
    WORKER_STATE_IDLE,
    WORKER_STATE_RUNNING,
    WORKER_STATE_WAITING_APPROVAL,
    WORKER_STATE_CANCELING,
    WORKER_STATE_STOPPED,
    WORKER_STATE_ERROR,
}

WORKER_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    WORKER_STATE_STARTING: {WORKER_STATE_IDLE, WORKER_STATE_ERROR, WORKER_STATE_STOPPED},
    WORKER_STATE_IDLE: {WORKER_STATE_RUNNING, WORKER_STATE_ERROR, WORKER_STATE_STOPPED},
    WORKER_STATE_RUNNING: {
        WORKER_STATE_WAITING_APPROVAL,
        WORKER_STATE_CANCELING,
        WORKER_STATE_IDLE,
        WORKER_STATE_ERROR,
        WORKER_STATE_STOPPED,
    },
    WORKER_STATE_WAITING_APPROVAL: {
        WORKER_STATE_RUNNING,
        WORKER_STATE_CANCELING,
        WORKER_STATE_ERROR,
        WORKER_STATE_STOPPED,
    },
    WORKER_STATE_CANCELING: {WORKER_STATE_IDLE, WORKER_STATE_ERROR, WORKER_STATE_STOPPED},
    WORKER_STATE_ERROR: {WORKER_STATE_STOPPED},
    WORKER_STATE_STOPPED: set(),
}

APPROVAL_STATUS_PENDING = "pending"
APPROVAL_STATUS_DECIDED = "decided"
APPROVAL_STATUS_CANCELED = "canceled"


def utc_now() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat()


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _deserialize_worker_row(worker: dict[str, Any] | None) -> dict[str, Any] | None:
    if worker is None:
        return None
    packs_raw = worker.get("packs_json")
    packs: list[str] = []
    if isinstance(packs_raw, str):
        try:
            parsed = json.loads(packs_raw)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            packs = [str(item) for item in parsed]
    payload = dict(worker)
    payload["packs"] = packs
    payload.pop("packs_json", None)
    return payload


def _parse_json_field(value: Any, fallback: Any) -> Any:
    if not isinstance(value, str):
        return fallback
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return fallback
    return parsed


def _deserialize_event_row(event: dict[str, Any] | None) -> dict[str, Any] | None:
    if event is None:
        return None
    payload = dict(event)
    payload["data"] = _parse_json_field(payload.get("data_json"), {})
    payload["raw"] = _parse_json_field(payload.get("raw_json"), None)
    payload.pop("data_json", None)
    payload.pop("raw_json", None)
    return payload


def _deserialize_approval_row(request: dict[str, Any] | None) -> dict[str, Any] | None:
    if request is None:
        return None
    payload = dict(request)
    payload["options"] = _parse_json_field(payload.get("options_json"), [])
    payload.pop("options_json", None)
    return payload


@dataclass(slots=True)
class ControllerHandle:
    controller_id: str
    instance_id: str
    epoch: int
    token: str
    pid: int
    created_at: str

    def to_dict(self, include_token: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "controllerId": self.controller_id,
            "instanceId": self.instance_id,
            "epoch": self.epoch,
            "pid": self.pid,
            "createdAt": self.created_at,
        }
        if include_token:
            payload["token"] = self.token
        return payload


class StateStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path.expanduser().resolve() if db_path else resolve_state_db_path()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def bootstrap(self) -> None:
        with self.connection() as conn:
            conn.executescript(SCHEMA_SQL)
            self._run_migrations(conn)

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        self._ensure_column(conn, table_name="workers", column_name="active_turn_id", column_type="TEXT")
        self._ensure_column(conn, table_name="workers", column_name="runtime_pid", column_type="INTEGER")
        self._ensure_column(conn, table_name="workers", column_name="runtime_socket", column_type="TEXT")

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        *,
        table_name: str,
        column_name: str,
        column_type: str,
    ) -> None:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {str(row["name"]) for row in rows}
        if column_name in existing:
            return
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")

    def register_controller(self, controller_id: str, label: str, workspace_key: str) -> dict[str, Any]:
        now = utc_now()
        with self.connection() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO controllers(controller_id, label, workspace_key, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(controller_id) DO UPDATE SET
                        label = excluded.label,
                        workspace_key = excluded.workspace_key,
                        updated_at = excluded.updated_at
                    """,
                    (controller_id, label, workspace_key, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise SubagentError(
                    code="CONTROLLER_OWNERSHIP_CONFLICT",
                    message=(
                        "Workspace already belongs to another controller. "
                        "Use `controller attach --takeover` with the existing controller."
                    ),
                    details={"workspaceKey": workspace_key},
                ) from exc
            row = conn.execute(
                "SELECT * FROM controllers WHERE controller_id = ?",
                (controller_id,),
            ).fetchone()
        assert row is not None
        return _row_to_dict(row) or {}

    def get_controller(self, controller_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM controllers WHERE controller_id = ?",
                (controller_id,),
            ).fetchone()
        return _row_to_dict(row)

    def list_controllers(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM controllers ORDER BY created_at DESC, controller_id DESC"
            ).fetchall()
        return [_row_to_dict(row) or {} for row in rows]

    def get_controller_by_workspace(self, workspace_key: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM controllers WHERE workspace_key = ?",
                (workspace_key,),
            ).fetchone()
        return _row_to_dict(row)

    def get_active_instance(self, controller_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT instance_id, controller_id, epoch, token, pid, created_at
                FROM controller_instances
                WHERE controller_id = ? AND is_active = 1
                """,
                (controller_id,),
            ).fetchone()
        return _row_to_dict(row)

    def list_active_instances(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT instance_id, controller_id, epoch, token, pid, created_at
                FROM controller_instances
                WHERE is_active = 1
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [_row_to_dict(row) or {} for row in rows]

    def acquire_owner_handle(
        self,
        controller_id: str,
        *,
        takeover: bool,
        pid: int | None = None,
    ) -> ControllerHandle:
        effective_pid = pid if pid is not None else os.getpid()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            controller = conn.execute(
                "SELECT controller_id FROM controllers WHERE controller_id = ?",
                (controller_id,),
            ).fetchone()
            if controller is None:
                raise SubagentError(
                    code="CONTROLLER_NOT_FOUND",
                    message=f"Controller not found: {controller_id}",
                    details={"controllerId": controller_id},
                )
            active = conn.execute(
                """
                SELECT instance_id, epoch, token, pid
                FROM controller_instances
                WHERE controller_id = ? AND is_active = 1
                """,
                (controller_id,),
            ).fetchone()
            if active is not None and not takeover:
                raise SubagentError(
                    code="CONTROLLER_OWNERSHIP_CONFLICT",
                    message="Controller already has an active owner. Use --takeover to replace it.",
                    details={"controllerId": controller_id},
                )
            if active is not None and takeover:
                now = utc_now()
                conn.execute(
                    """
                    UPDATE controller_instances
                    SET is_active = 0, released_at = ?
                    WHERE controller_id = ? AND is_active = 1
                    """,
                    (now, controller_id),
                )

            max_epoch = conn.execute(
                "SELECT COALESCE(MAX(epoch), 0) AS max_epoch FROM controller_instances WHERE controller_id = ?",
                (controller_id,),
            ).fetchone()
            next_epoch = int(max_epoch["max_epoch"]) + 1 if max_epoch else 1
            token = secrets.token_urlsafe(24)
            instance_id = f"ci_{uuid.uuid4().hex[:12]}"
            created_at = utc_now()
            conn.execute(
                """
                INSERT INTO controller_instances(instance_id, controller_id, epoch, token, pid, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (instance_id, controller_id, next_epoch, token, effective_pid, created_at),
            )
        return ControllerHandle(
            controller_id=controller_id,
            instance_id=instance_id,
            epoch=next_epoch,
            token=token,
            pid=effective_pid,
            created_at=created_at,
        )

    def validate_handle(self, controller_id: str, epoch: int, token: str) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM controller_instances
                WHERE controller_id = ? AND epoch = ? AND token = ? AND is_active = 1
                """,
                (controller_id, epoch, token),
            ).fetchone()
        return row is not None

    def release_owner_handle(
        self,
        *,
        controller_id: str,
        epoch: int | None = None,
        token: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        controller = self.get_controller(controller_id)
        if controller is None:
            raise SubagentError(
                code="CONTROLLER_NOT_FOUND",
                message=f"Controller not found: {controller_id}",
                details={"controllerId": controller_id},
            )
        active = self.get_active_instance(controller_id)
        if active is None:
            return {
                "controllerId": controller_id,
                "released": False,
                "reason": "NO_ACTIVE_OWNER",
            }
        if not force:
            if epoch is None or token is None:
                raise SubagentError(
                    code="INVALID_CONTROLLER_HANDLE",
                    message="Release requires epoch and token, or use --force.",
                    details={"controllerId": controller_id},
                )
            if int(active["epoch"]) != int(epoch) or str(active["token"]) != str(token):
                raise SubagentError(
                    code="INVALID_CONTROLLER_HANDLE",
                    message="Controller handle is stale or invalid",
                    details={"controllerId": controller_id, "epoch": epoch},
                )
        released_at = utc_now()
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE controller_instances
                SET is_active = 0, released_at = ?
                WHERE controller_id = ? AND is_active = 1
                """,
                (released_at, controller_id),
            )
        return {
            "controllerId": controller_id,
            "released": True,
            "releasedAt": released_at,
            "instanceId": active["instance_id"],
            "epoch": active["epoch"],
        }

    def get_controller_status(self, controller_id: str) -> dict[str, Any]:
        controller = self.get_controller(controller_id)
        if controller is None:
            raise SubagentError(
                code="CONTROLLER_NOT_FOUND",
                message=f"Controller not found: {controller_id}",
                details={"controllerId": controller_id},
            )
        active = self.get_active_instance(controller_id)
        state = "active" if active is not None else "dormant"
        return {
            "controllerId": controller["controller_id"],
            "label": controller["label"],
            "workspaceKey": controller["workspace_key"],
            "state": state,
            "activeOwner": {
                "instanceId": active["instance_id"],
                "epoch": active["epoch"],
                "pid": active["pid"],
                "createdAt": active["created_at"],
            }
            if active
            else None,
        }

    def create_worker(
        self,
        *,
        controller_id: str,
        launcher: str,
        profile: str,
        packs: list[str],
        cwd: str,
        label: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        controller = self.get_controller(controller_id)
        if controller is None:
            raise SubagentError(
                code="CONTROLLER_NOT_FOUND",
                message=f"Controller not found: {controller_id}",
                details={"controllerId": controller_id},
            )
        worker_id = f"w_{uuid.uuid4().hex[:10]}"
        effective_session_id = session_id or f"sess_{uuid.uuid4().hex[:12]}"
        now = utc_now()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO workers(
                    worker_id, controller_id, label, launcher, profile, packs_json,
                    cwd, session_id, runtime_pid, runtime_socket,
                    state, recovery_state, active_turn_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    worker_id,
                    controller_id,
                    label,
                    launcher,
                    profile,
                    json.dumps(packs, ensure_ascii=False),
                    cwd,
                    effective_session_id,
                    None,
                    None,
                    WORKER_STATE_STARTING,
                    "restartable",
                    None,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE workers
                SET state = ?, updated_at = ?
                WHERE worker_id = ?
                """,
                (WORKER_STATE_IDLE, utc_now(), worker_id),
            )
            row = conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        return _deserialize_worker_row(_row_to_dict(row)) or {}

    def list_workers(self, *, controller_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM workers"
        params: tuple[Any, ...] = ()
        if controller_id is not None:
            query += " WHERE controller_id = ?"
            params = (controller_id,)
        query += " ORDER BY created_at DESC, worker_id DESC"
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        workers = [_deserialize_worker_row(_row_to_dict(row)) for row in rows]
        return [worker for worker in workers if worker is not None]

    def get_worker(self, worker_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        return _deserialize_worker_row(_row_to_dict(row))

    def update_worker_state(
        self,
        worker_id: str,
        *,
        next_state: str,
        allow_any_transition: bool = False,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        if next_state not in WORKER_RUNTIME_STATES:
            raise SubagentError(
                code="INVALID_WORKER_STATE",
                message=f"Unknown worker state: {next_state}",
                details={"state": next_state},
            )
        worker = self.get_worker(worker_id)
        if worker is None:
            raise SubagentError(
                code="WORKER_NOT_FOUND",
                message=f"Worker not found: {worker_id}",
                details={"workerId": worker_id},
            )
        current_state = str(worker["state"])
        if current_state == next_state:
            return worker
        if not allow_any_transition and next_state not in WORKER_ALLOWED_TRANSITIONS[current_state]:
            raise SubagentError(
                code="INVALID_WORKER_STATE_TRANSITION",
                message=f"Cannot transition worker from {current_state} to {next_state}",
                details={
                    "workerId": worker_id,
                    "currentState": current_state,
                    "nextState": next_state,
                },
            )
        now = utc_now()
        stopped_at = now if next_state == WORKER_STATE_STOPPED else None
        clear_active_turn = next_state in {
            WORKER_STATE_IDLE,
            WORKER_STATE_STOPPED,
            WORKER_STATE_ERROR,
        }
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE workers
                SET state = ?,
                    updated_at = ?,
                    stopped_at = COALESCE(?, stopped_at),
                    last_error = ?,
                    active_turn_id = CASE WHEN ? THEN NULL ELSE active_turn_id END
                WHERE worker_id = ?
                """,
                (next_state, now, stopped_at, last_error, int(clear_active_turn), worker_id),
            )
            row = conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        return _deserialize_worker_row(_row_to_dict(row)) or {}

    def stop_worker(self, worker_id: str, *, force: bool = False) -> dict[str, Any]:
        worker = self.get_worker(worker_id)
        if worker is None:
            raise SubagentError(
                code="WORKER_NOT_FOUND",
                message=f"Worker not found: {worker_id}",
                details={"workerId": worker_id},
            )
        if str(worker["state"]) == WORKER_STATE_STOPPED:
            return worker
        return self.update_worker_state(
            worker_id,
            next_state=WORKER_STATE_STOPPED,
            allow_any_transition=force,
        )

    def set_worker_active_turn(self, worker_id: str, turn_id: str | None) -> dict[str, Any]:
        worker = self.get_worker(worker_id)
        if worker is None:
            raise SubagentError(
                code="WORKER_NOT_FOUND",
                message=f"Worker not found: {worker_id}",
                details={"workerId": worker_id},
            )
        with self.connection() as conn:
            conn.execute(
                "UPDATE workers SET active_turn_id = ?, updated_at = ? WHERE worker_id = ?",
                (turn_id, utc_now(), worker_id),
            )
            row = conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        return _deserialize_worker_row(_row_to_dict(row)) or {}

    def set_worker_session_id(self, worker_id: str, session_id: str) -> dict[str, Any]:
        worker = self.get_worker(worker_id)
        if worker is None:
            raise SubagentError(
                code="WORKER_NOT_FOUND",
                message=f"Worker not found: {worker_id}",
                details={"workerId": worker_id},
            )
        with self.connection() as conn:
            conn.execute(
                "UPDATE workers SET session_id = ?, updated_at = ? WHERE worker_id = ?",
                (session_id, utc_now(), worker_id),
            )
            row = conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        return _deserialize_worker_row(_row_to_dict(row)) or {}

    def set_worker_runtime_endpoint(
        self,
        worker_id: str,
        *,
        runtime_pid: int | None,
        runtime_socket: str | None,
    ) -> dict[str, Any]:
        worker = self.get_worker(worker_id)
        if worker is None:
            raise SubagentError(
                code="WORKER_NOT_FOUND",
                message=f"Worker not found: {worker_id}",
                details={"workerId": worker_id},
            )
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE workers
                SET runtime_pid = ?, runtime_socket = ?, updated_at = ?
                WHERE worker_id = ?
                """,
                (runtime_pid, runtime_socket, utc_now(), worker_id),
            )
            row = conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        return _deserialize_worker_row(_row_to_dict(row)) or {}

    def clear_worker_runtime_endpoint(self, worker_id: str) -> dict[str, Any]:
        return self.set_worker_runtime_endpoint(
            worker_id,
            runtime_pid=None,
            runtime_socket=None,
        )

    def _resolve_event_cursor_seq(
        self,
        conn: sqlite3.Connection,
        *,
        worker_id: str,
        from_event_id: str | None,
    ) -> int:
        if from_event_id is None:
            return 0
        row = conn.execute(
            "SELECT event_seq FROM worker_events WHERE worker_id = ? AND event_id = ?",
            (worker_id, from_event_id),
        ).fetchone()
        if row is None:
            raise SubagentError(
                code="EVENT_NOT_FOUND",
                message=f"Event not found: {from_event_id}",
                details={"workerId": worker_id, "eventId": from_event_id},
            )
        return int(row["event_seq"])

    def append_worker_event(
        self,
        worker_id: str,
        *,
        event_type: str,
        data: dict[str, Any],
        turn_id: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        worker = self.get_worker(worker_id)
        if worker is None:
            raise SubagentError(
                code="WORKER_NOT_FOUND",
                message=f"Worker not found: {worker_id}",
                details={"workerId": worker_id},
            )
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            max_row = conn.execute(
                "SELECT COALESCE(MAX(event_seq), 0) AS max_seq FROM worker_events WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            next_seq = int(max_row["max_seq"]) + 1 if max_row else 1
            event_id = f"ev_{uuid.uuid4().hex[:12]}"
            ts = utc_now()
            conn.execute(
                """
                INSERT INTO worker_events(event_id, worker_id, event_seq, ts, event_type, turn_id, data_json, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    worker_id,
                    next_seq,
                    ts,
                    event_type,
                    turn_id,
                    json.dumps(data, ensure_ascii=False),
                    json.dumps(raw, ensure_ascii=False) if raw is not None else None,
                ),
            )
            row = conn.execute("SELECT * FROM worker_events WHERE event_id = ?", (event_id,)).fetchone()
        return _deserialize_event_row(_row_to_dict(row)) or {}

    def list_worker_events(
        self,
        worker_id: str,
        *,
        from_event_id: str | None = None,
        limit: int | None = None,
        since: str | None = None,
        turn_id: str | None = None,
        event_types: list[str] | None = None,
        tail: bool = False,
    ) -> list[dict[str, Any]]:
        worker = self.get_worker(worker_id)
        if worker is None:
            raise SubagentError(
                code="WORKER_NOT_FOUND",
                message=f"Worker not found: {worker_id}",
                details={"workerId": worker_id},
            )
        with self.connection() as conn:
            cursor_seq = self._resolve_event_cursor_seq(
                conn,
                worker_id=worker_id,
                from_event_id=from_event_id,
            )
            base_query = "SELECT * FROM worker_events WHERE worker_id = ? AND event_seq > ?"
            params: list[Any] = [worker_id, cursor_seq]
            if since is not None:
                base_query += " AND ts >= ?"
                params.append(since)
            if turn_id is not None:
                base_query += " AND turn_id = ?"
                params.append(turn_id)
            if event_types:
                normalized_types = [value for value in event_types if isinstance(value, str) and value]
                if normalized_types:
                    placeholders = ",".join("?" for _ in normalized_types)
                    base_query += f" AND event_type IN ({placeholders})"
                    params.extend(normalized_types)
            if limit is not None and tail:
                query = f"SELECT * FROM ({base_query} ORDER BY event_seq DESC LIMIT ?) ORDER BY event_seq ASC"
                params.append(limit)
            else:
                query = f"{base_query} ORDER BY event_seq ASC"
                if limit is not None:
                    query += " LIMIT ?"
                    params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
        events = [_deserialize_event_row(_row_to_dict(row)) for row in rows]
        return [event for event in events if event is not None]

    def get_latest_worker_event(self, worker_id: str) -> dict[str, Any] | None:
        worker = self.get_worker(worker_id)
        if worker is None:
            raise SubagentError(
                code="WORKER_NOT_FOUND",
                message=f"Worker not found: {worker_id}",
                details={"workerId": worker_id},
            )
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM worker_events WHERE worker_id = ? ORDER BY event_seq DESC LIMIT 1",
                (worker_id,),
            ).fetchone()
        return _deserialize_event_row(_row_to_dict(row))

    def create_approval_request(
        self,
        worker_id: str,
        *,
        turn_id: str | None,
        message: str,
        kind: str = "tool.call",
        options: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        worker = self.get_worker(worker_id)
        if worker is None:
            raise SubagentError(
                code="WORKER_NOT_FOUND",
                message=f"Worker not found: {worker_id}",
                details={"workerId": worker_id},
            )
        normalized_options = options or [
            {"id": "allow", "alias": "allow", "label": "Allow"},
            {"id": "deny", "alias": "deny", "label": "Deny"},
        ]
        request_id = f"ap_{uuid.uuid4().hex[:10]}"
        created_at = utc_now()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO approval_requests(
                    request_id, worker_id, turn_id, status, kind, message, options_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    worker_id,
                    turn_id,
                    APPROVAL_STATUS_PENDING,
                    kind,
                    message,
                    json.dumps(normalized_options, ensure_ascii=False),
                    created_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return _deserialize_approval_row(_row_to_dict(row)) or {}

    def get_approval_request(self, worker_id: str, request_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM approval_requests
                WHERE worker_id = ? AND request_id = ?
                """,
                (worker_id, request_id),
            ).fetchone()
        return _deserialize_approval_row(_row_to_dict(row))

    def list_pending_approval_requests(self, worker_id: str) -> list[dict[str, Any]]:
        worker = self.get_worker(worker_id)
        if worker is None:
            raise SubagentError(
                code="WORKER_NOT_FOUND",
                message=f"Worker not found: {worker_id}",
                details={"workerId": worker_id},
            )
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM approval_requests
                WHERE worker_id = ? AND status = ?
                ORDER BY created_at ASC
                """,
                (worker_id, APPROVAL_STATUS_PENDING),
            ).fetchall()
        requests = [_deserialize_approval_row(_row_to_dict(row)) for row in rows]
        return [request for request in requests if request is not None]

    def decide_approval_request(
        self,
        worker_id: str,
        request_id: str,
        *,
        decision: str,
        selected_option_id: str,
        selected_alias: str | None,
        note: str | None = None,
    ) -> dict[str, Any]:
        request = self.get_approval_request(worker_id, request_id)
        if request is None:
            raise SubagentError(
                code="APPROVAL_NOT_FOUND",
                message=f"Approval request not found: {request_id}",
                details={"workerId": worker_id, "requestId": request_id},
            )
        if str(request["status"]) != APPROVAL_STATUS_PENDING:
            raise SubagentError(
                code="APPROVAL_NOT_PENDING",
                message=f"Approval request is not pending: {request_id}",
                details={"requestId": request_id, "status": request["status"]},
            )
        now = utc_now()
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE approval_requests
                SET status = ?, decided_at = ?, decision = ?, selected_option_id = ?, selected_alias = ?, note = ?
                WHERE request_id = ? AND worker_id = ?
                """,
                (
                    APPROVAL_STATUS_DECIDED,
                    now,
                    decision,
                    selected_option_id,
                    selected_alias,
                    note,
                    request_id,
                    worker_id,
                ),
            )
            row = conn.execute(
                """
                SELECT *
                FROM approval_requests
                WHERE request_id = ? AND worker_id = ?
                """,
                (request_id, worker_id),
            ).fetchone()
        return _deserialize_approval_row(_row_to_dict(row)) or {}

    def register_handoff_snapshot(
        self,
        *,
        worker_id: str,
        source_turn_id: str | None,
        handoff_path: str,
        checkpoint_path: str,
    ) -> dict[str, Any]:
        worker = self.get_worker(worker_id)
        if worker is None:
            raise SubagentError(
                code="WORKER_NOT_FOUND",
                message=f"Worker not found: {worker_id}",
                details={"workerId": worker_id},
            )
        snapshot_id = f"hs_{uuid.uuid4().hex[:10]}"
        created_at = utc_now()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO handoff_snapshots(
                    snapshot_id, worker_id, controller_id, source_turn_id, handoff_path, checkpoint_path, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    worker_id,
                    worker["controller_id"],
                    source_turn_id,
                    handoff_path,
                    checkpoint_path,
                    created_at,
                ),
            )
            row = conn.execute(
                """
                SELECT *
                FROM handoff_snapshots
                WHERE snapshot_id = ?
                """,
                (snapshot_id,),
            ).fetchone()
        return _row_to_dict(row) or {}

    def get_latest_handoff_snapshot(self, worker_id: str) -> dict[str, Any] | None:
        worker = self.get_worker(worker_id)
        if worker is None:
            raise SubagentError(
                code="WORKER_NOT_FOUND",
                message=f"Worker not found: {worker_id}",
                details={"workerId": worker_id},
            )
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM handoff_snapshots
                WHERE worker_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (worker_id,),
            ).fetchone()
        return _row_to_dict(row)
