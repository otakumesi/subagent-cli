"""Long-lived per-worker ACP runtime process."""

from __future__ import annotations

import argparse
import json
import os
import socket
import threading
from pathlib import Path
from typing import Any

from . import __version__
from .acp_client import AcpStdioClient
from .approval_utils import resolve_option
from .constants import RUNTIME_STARTUP_TIMEOUT_SECONDS
from .errors import SubagentError
from .state import (
    WORKER_STATE_IDLE,
    WORKER_STATE_RUNNING,
    WORKER_STATE_WAITING_APPROVAL,
    StateStore,
)


def _extract_session_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise SubagentError(
            code="BACKEND_PROTOCOL_ERROR",
            message="Backend returned invalid session response.",
            details={"response": payload},
        )
    session_id = payload.get("sessionId")
    if isinstance(session_id, str) and session_id:
        return session_id
    raise SubagentError(
        code="BACKEND_PROTOCOL_ERROR",
        message="Backend response is missing sessionId.",
        details={"response": payload},
    )


def _extract_text_chunks(payload: Any) -> list[str]:
    chunks: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            node_type = node.get("type")
            text = node.get("text")
            if isinstance(text, str) and (not isinstance(node_type, str) or node_type == "text"):
                chunks.append(text)
            for value in node.values():
                walk(value)
            return
        if isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return chunks


def _normalize_permission_options(params: dict[str, Any]) -> list[dict[str, Any]]:
    options_payload = params.get("options")
    if not isinstance(options_payload, list):
        return []
    options: list[dict[str, Any]] = []
    for item in options_payload:
        if not isinstance(item, dict):
            continue
        option_id = item.get("optionId")
        if not isinstance(option_id, str) or not option_id:
            option_id = item.get("id")
        if not isinstance(option_id, str) or not option_id:
            continue
        label = item.get("name")
        options.append(
            {
                "id": option_id,
                "alias": option_id.lower(),
                "label": str(label) if isinstance(label, str) and label else option_id,
                "kind": item.get("kind"),
            }
        )
    return options


def _build_prompt_blocks(text: str, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if text.strip():
        output.append({"type": "text", "text": text})
    output.extend(blocks)
    return output


class WorkerRuntime:
    def __init__(
        self,
        *,
        db_path: Path,
        worker_id: str,
        socket_path: Path,
        launcher_command: str,
        launcher_args: list[str],
        launcher_env: dict[str, str],
        cwd: Path,
    ) -> None:
        self.store = StateStore(db_path)
        self.worker_id = worker_id
        self.socket_path = socket_path
        self.launcher_command = launcher_command
        self.launcher_args = launcher_args
        self.launcher_env = launcher_env
        self.cwd = cwd

        self.client: AcpStdioClient | None = None
        self.session_id: str = ""
        self.server_socket: socket.socket | None = None
        self._shutdown_requested = False

        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._turn_thread: threading.Thread | None = None
        self._active_turn_id: str | None = None
        self._turn_result: dict[str, Any] | None = None
        self._turn_error: SubagentError | None = None
        self._pending_permission: dict[str, Any] | None = None
        self._cancel_requested = False
        self._cancel_reason = "canceled by manager"

    def run(self) -> int:
        self.store.bootstrap()
        worker = self.store.get_worker(self.worker_id)
        if worker is None:
            raise SubagentError(
                code="WORKER_NOT_FOUND",
                message=f"Worker not found: {self.worker_id}",
                details={"workerId": self.worker_id},
            )
        self.client = AcpStdioClient(
            command=self.launcher_command,
            args=self.launcher_args,
            cwd=self.cwd,
            env=self.launcher_env,
        )
        try:
            self.client.request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientInfo": {"name": "subagent-cli", "version": __version__},
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                },
                timeout_seconds=RUNTIME_STARTUP_TIMEOUT_SECONDS,
            )
            existing_session_id = worker.get("session_id")
            resumed_session = False
            if isinstance(existing_session_id, str) and existing_session_id:
                try:
                    session_payload = self.client.request(
                        "session/load",
                        {"sessionId": existing_session_id},
                        timeout_seconds=RUNTIME_STARTUP_TIMEOUT_SECONDS,
                    )
                except SubagentError:
                    session_payload = self.client.request(
                        "session/new",
                        {"cwd": str(self.cwd), "mcpServers": []},
                        timeout_seconds=RUNTIME_STARTUP_TIMEOUT_SECONDS,
                    )
                else:
                    resumed_session = True
            else:
                session_payload = self.client.request(
                    "session/new",
                    {"cwd": str(self.cwd), "mcpServers": []},
                    timeout_seconds=RUNTIME_STARTUP_TIMEOUT_SECONDS,
                )
            self.session_id = _extract_session_id(session_payload)
            self.store.set_worker_session_id(self.worker_id, self.session_id)
            self.store.append_worker_event(
                self.worker_id,
                event_type="progress.update",
                turn_id=None,
                data={"method": "session.load" if resumed_session else "session.new"},
                raw={
                    "runtime": "acp-stdio",
                    "phase": "session.start",
                    "sessionId": self.session_id,
                },
            )
            self.store.set_worker_runtime_endpoint(
                self.worker_id,
                runtime_pid=os.getpid(),
                runtime_socket=str(self.socket_path),
            )
            self.store.update_worker_state(
                self.worker_id,
                next_state=WORKER_STATE_IDLE,
                allow_any_transition=True,
            )
            return self._serve()
        finally:
            try:
                self.store.clear_worker_runtime_endpoint(self.worker_id)
            except Exception:
                pass
            if self.client is not None:
                self.client.close()
            if self.server_socket is not None:
                try:
                    self.server_socket.close()
                except Exception:
                    pass
            if self.socket_path.exists():
                try:
                    self.socket_path.unlink()
                except OSError:
                    pass

    def _serve(self) -> int:
        if self.socket_path.exists():
            self.socket_path.unlink()
        self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_socket.bind(str(self.socket_path))
        self.server_socket.listen(8)
        while not self._shutdown_requested:
            try:
                conn, _ = self.server_socket.accept()
            except OSError:
                if self._shutdown_requested:
                    break
                raise
            thread = threading.Thread(
                target=self._handle_connection_socket,
                args=(conn,),
                daemon=True,
                name=f"worker-runtime-socket-{self.worker_id}",
            )
            thread.start()
        return 0

    def _handle_connection_socket(self, conn: socket.socket) -> None:
        with conn:
            response = self._handle_connection(conn)
            try:
                conn.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            except OSError:
                # The client may disconnect while runtime is shutting down.
                return

    def _handle_connection(self, conn: socket.socket) -> dict[str, Any]:
        try:
            data = self._read_line(conn)
            request = json.loads(data)
            if not isinstance(request, dict):
                raise SubagentError(code="INVALID_ARGUMENT", message="Runtime request must be an object.")
            method = request.get("method")
            params = request.get("params")
            if not isinstance(method, str) or not method:
                raise SubagentError(code="INVALID_ARGUMENT", message="Runtime request missing method.")
            if not isinstance(params, dict):
                params = {}
            result = self._dispatch(method, params)
            return {"ok": True, "result": result}
        except SubagentError as error:
            return {"ok": False, "error": error.to_dict()}
        except Exception as error:  # pragma: no cover - defensive
            err = SubagentError(
                code="BACKEND_RUNTIME_ERROR",
                message=f"Runtime internal error: {error}",
            )
            return {"ok": False, "error": err.to_dict()}

    def _read_line(self, conn: socket.socket) -> str:
        chunks: list[bytes] = []
        while True:
            block = conn.recv(4096)
            if not block:
                break
            chunks.append(block)
            if b"\n" in block:
                break
        if not chunks:
            raise SubagentError(code="INVALID_ARGUMENT", message="Runtime request is empty.")
        return b"".join(chunks).decode("utf-8", errors="replace").strip()

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "ping":
            return {"workerId": self.worker_id, "sessionId": self.session_id}
        if method == "start_turn":
            return self._handle_start_turn(params)
        if method == "approve":
            return self._handle_approve(params)
        if method == "cancel_turn":
            return self._handle_cancel_turn(params)
        if method == "stop":
            return self._handle_stop(params)
        raise SubagentError(
            code="INVALID_ARGUMENT",
            message=f"Unknown runtime method: {method}",
            details={"method": method},
        )

    def _handle_start_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        turn_id = params.get("turnId")
        text = params.get("text")
        blocks = params.get("blocks")
        if not isinstance(turn_id, str) or not turn_id:
            raise SubagentError(code="INVALID_ARGUMENT", message="`turnId` is required.")
        if not isinstance(text, str):
            raise SubagentError(code="INVALID_ARGUMENT", message="`text` must be a string.")
        if not isinstance(blocks, list):
            raise SubagentError(code="INVALID_ARGUMENT", message="`blocks` must be a list.")
        normalized_blocks: list[dict[str, Any]] = []
        for index, item in enumerate(blocks):
            if not isinstance(item, dict):
                raise SubagentError(
                    code="INVALID_ARGUMENT",
                    message=f"`blocks[{index}]` must be an object.",
                )
            normalized_blocks.append(item)

        with self._cv:
            if self._active_turn_id is not None:
                raise SubagentError(
                    code="WORKER_BUSY",
                    message="worker has an active turn",
                    details={"workerId": self.worker_id, "turnId": self._active_turn_id},
                )
            # Re-sync persistent state with this runtime turn. This is critical after
            # runtime restarts, where startup may have set the worker row back to idle.
            self.store.update_worker_state(self.worker_id, next_state=WORKER_STATE_RUNNING)
            self.store.set_worker_active_turn(self.worker_id, turn_id)
            self._active_turn_id = turn_id
            self._turn_result = None
            self._turn_error = None
            self._pending_permission = None
            self._cancel_requested = False
            self._cancel_reason = "canceled by manager"
            self._turn_thread = threading.Thread(
                target=self._run_turn,
                args=(turn_id, text, normalized_blocks),
                daemon=True,
                name=f"worker-turn-{self.worker_id}",
            )
            self._turn_thread.start()

            while True:
                if self._turn_error is not None:
                    raise self._turn_error
                if self._pending_permission is not None:
                    return {
                        "workerId": self.worker_id,
                        "turnId": turn_id,
                        "state": WORKER_STATE_WAITING_APPROVAL,
                        "requestId": self._pending_permission["request_id"],
                    }
                if self._turn_result is not None:
                    return self._turn_result
                self._cv.wait(timeout=0.1)

    def _run_turn(self, turn_id: str, text: str, blocks: list[dict[str, Any]]) -> None:
        assert self.client is not None

        def on_notification(method: str, params: dict[str, Any]) -> None:
            if method != "session/update":
                self.store.append_worker_event(
                    self.worker_id,
                    event_type="progress.update",
                    turn_id=turn_id,
                    data={"method": method},
                    raw={
                        "runtime": "acp-stdio",
                        "phase": "notification",
                        "method": method,
                        "params": params,
                    },
                )
                return
            update = params.get("update")
            text_chunks = _extract_text_chunks(update)
            if not text_chunks:
                self.store.append_worker_event(
                    self.worker_id,
                    event_type="progress.update",
                    turn_id=turn_id,
                    data={"method": "session/update"},
                    raw={"runtime": "acp-stdio", "phase": "session.update", "update": update},
                )
                return
            for chunk in text_chunks:
                self.store.append_worker_event(
                    self.worker_id,
                    event_type="progress.message",
                    turn_id=turn_id,
                    data={"role": "assistant", "text": chunk},
                    raw={"runtime": "acp-stdio", "phase": "session.update", "update": update},
                )

        def on_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method != "session/request_permission":
                raise SubagentError(
                    code="BACKEND_PROTOCOL_ERROR",
                    message=f"Unsupported backend request method: {method}",
                    details={"method": method},
                )
            options = _normalize_permission_options(params)
            message = "Backend requested permission for a tool call."
            tool_call = params.get("toolCall")
            if isinstance(tool_call, dict):
                kind = tool_call.get("kind")
                if isinstance(kind, str) and kind:
                    message = f"Backend requested permission for `{kind}`."
            request: dict[str, Any] | None = None
            try:
                request = self.store.create_approval_request(
                    self.worker_id,
                    turn_id=turn_id,
                    message=message,
                    kind="tool.call",
                    options=options or None,
                )
                self.store.update_worker_state(self.worker_id, next_state=WORKER_STATE_WAITING_APPROVAL)
                self.store.append_worker_event(
                    self.worker_id,
                    event_type="approval.requested",
                    turn_id=turn_id,
                    data={
                        "requestId": request["request_id"],
                        "kind": request["kind"],
                        "message": request["message"],
                        "options": request["options"],
                    },
                    raw={
                        "runtime": "acp-stdio",
                        "phase": "approval.requested",
                        "request": params,
                    },
                )
                with self._cv:
                    self._pending_permission = {
                        "request_id": request["request_id"],
                        "response": None,
                    }
                    self._cv.notify_all()
                    while True:
                        pending = self._pending_permission
                        if pending is None:
                            raise SubagentError(
                                code="BACKEND_RUNTIME_ERROR",
                                message="Pending permission state was lost.",
                            )
                        response = pending.get("response")
                        if isinstance(response, dict):
                            self._pending_permission = None
                            break
                        self._cv.wait(timeout=0.1)
                self.store.update_worker_state(self.worker_id, next_state=WORKER_STATE_RUNNING)
                return response
            except Exception:
                # Avoid orphan pending approvals when request wiring fails mid-flight.
                if request is not None:
                    request_id = request.get("request_id")
                    if isinstance(request_id, str) and request_id:
                        try:
                            self.store.cancel_approval_request(
                                self.worker_id,
                                request_id,
                                decision="runtime_error",
                                note="approval flow aborted before a valid response was applied",
                            )
                        except Exception:
                            pass
                raise

        try:
            response = self.client.request(
                "session/prompt",
                {
                    "sessionId": self.session_id,
                    "prompt": _build_prompt_blocks(text, blocks),
                },
                timeout_seconds=3600.0,
                on_notification=on_notification,
                on_request=on_request,
            )
            stop_reason = "completed"
            if isinstance(response, dict):
                raw_reason = response.get("stopReason")
                if isinstance(raw_reason, str) and raw_reason:
                    stop_reason = raw_reason

            with self._cv:
                canceled = self._cancel_requested
                cancel_reason = self._cancel_reason
            if canceled:
                canceled_event = self.store.append_worker_event(
                    self.worker_id,
                    event_type="turn.canceled",
                    turn_id=turn_id,
                    data={"turnId": turn_id, "reason": cancel_reason},
                    raw={"runtime": "acp-stdio", "phase": "turn.canceled"},
                )
                with self._cv:
                    self._active_turn_id = None
                self.store.update_worker_state(self.worker_id, next_state=WORKER_STATE_IDLE)
                result = {
                    "workerId": self.worker_id,
                    "turnId": turn_id,
                    "state": WORKER_STATE_IDLE,
                    "eventId": canceled_event["event_id"],
                }
            else:
                completed_event = self.store.append_worker_event(
                    self.worker_id,
                    event_type="turn.completed",
                    turn_id=turn_id,
                    data={
                        "turnId": turn_id,
                        "outcome": "completed",
                        "state": WORKER_STATE_IDLE,
                        "stopReason": stop_reason,
                    },
                    raw={"runtime": "acp-stdio", "phase": "turn.completed"},
                )
                with self._cv:
                    self._active_turn_id = None
                self.store.update_worker_state(self.worker_id, next_state=WORKER_STATE_IDLE)
                result = {
                    "workerId": self.worker_id,
                    "turnId": turn_id,
                    "state": WORKER_STATE_IDLE,
                    "eventId": completed_event["event_id"],
                    "stopReason": stop_reason,
                }
            with self._cv:
                self._turn_result = result
                self._cv.notify_all()
        except SubagentError as error:
            self.store.append_worker_event(
                self.worker_id,
                event_type="turn.failed",
                turn_id=turn_id,
                data={"turnId": turn_id, "error": error.to_dict()},
                raw={"runtime": "acp-stdio", "phase": "turn.failed"},
            )
            with self._cv:
                self._active_turn_id = None
            self.store.update_worker_state(
                self.worker_id,
                next_state=WORKER_STATE_IDLE,
                last_error=error.message,
            )
            with self._cv:
                self._turn_error = error
                self._cv.notify_all()

    def _wait_for_turn_terminal(self) -> dict[str, Any]:
        with self._cv:
            while True:
                if self._turn_error is not None:
                    raise self._turn_error
                if self._turn_result is not None:
                    return self._turn_result
                self._cv.wait(timeout=0.1)

    def _handle_approve(self, params: dict[str, Any]) -> dict[str, Any]:
        request_id = params.get("requestId")
        decision = params.get("decision")
        option_id = params.get("optionId")
        alias = params.get("alias")
        note = params.get("note")
        if not isinstance(request_id, str) or not request_id:
            raise SubagentError(code="INVALID_ARGUMENT", message="`requestId` is required.")
        if decision is not None and not isinstance(decision, str):
            raise SubagentError(code="INVALID_ARGUMENT", message="`decision` must be a string.")
        if option_id is not None and not isinstance(option_id, str):
            raise SubagentError(code="INVALID_ARGUMENT", message="`optionId` must be a string.")
        if alias is not None and not isinstance(alias, str):
            raise SubagentError(code="INVALID_ARGUMENT", message="`alias` must be a string.")
        if note is not None and not isinstance(note, str):
            raise SubagentError(code="INVALID_ARGUMENT", message="`note` must be a string.")

        with self._cv:
            pending = self._pending_permission
            if pending is None:
                raise SubagentError(
                    code="APPROVAL_NOT_FOUND",
                    message=f"Approval request not found: {request_id}",
                    details={"workerId": self.worker_id, "requestId": request_id},
                )
            pending_id = pending.get("request_id")
            if str(pending_id) != request_id:
                raise SubagentError(
                    code="APPROVAL_NOT_FOUND",
                    message=f"Approval request not found: {request_id}",
                    details={"workerId": self.worker_id, "requestId": request_id, "pendingRequestId": pending_id},
                )

        request = self.store.get_approval_request(self.worker_id, request_id)
        if request is None:
            raise SubagentError(
                code="APPROVAL_NOT_FOUND",
                message=f"Approval request not found: {request_id}",
                details={"workerId": self.worker_id, "requestId": request_id},
            )
        selected_option_id, selected_alias, resolved_decision = resolve_option(
            request,
            decision=decision,
            option_id=option_id,
            alias=alias,
        )
        decided = self.store.decide_approval_request(
            self.worker_id,
            request_id,
            decision=resolved_decision,
            selected_option_id=selected_option_id,
            selected_alias=selected_alias,
            note=note,
        )
        turn_id = request.get("turn_id")
        self.store.append_worker_event(
            self.worker_id,
            event_type="approval.decided",
            turn_id=str(turn_id) if isinstance(turn_id, str) else None,
            data={
                "requestId": request_id,
                "decision": decided["decision"],
                "optionId": decided["selected_option_id"],
                "alias": decided["selected_alias"],
                "note": note,
            },
            raw={"runtime": "acp-stdio", "phase": "approval.decided"},
        )
        self.store.update_worker_state(self.worker_id, next_state=WORKER_STATE_RUNNING)

        response_payload: dict[str, Any] = {"outcome": {"outcome": "selected", "optionId": selected_option_id}}
        if selected_option_id in {"cancel", "cancelled"} or selected_alias in {"cancel", "cancelled"}:
            response_payload = {"outcome": {"outcome": "cancelled"}}

        with self._cv:
            pending = self._pending_permission
            if pending is None:
                raise SubagentError(
                    code="APPROVAL_NOT_FOUND",
                    message=f"Approval request not found: {request_id}",
                    details={"workerId": self.worker_id, "requestId": request_id},
                )
            pending["response"] = response_payload
            self._cv.notify_all()

        result = self._wait_for_turn_terminal()
        return {
            "workerId": self.worker_id,
            "requestId": request_id,
            "decision": decided["decision"],
            "optionId": decided["selected_option_id"],
            "alias": decided["selected_alias"],
            "state": result["state"],
            "eventId": result["eventId"],
        }

    def _handle_cancel_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        reason = params.get("reason")
        if reason is None:
            reason = "canceled by manager"
        if not isinstance(reason, str):
            raise SubagentError(code="INVALID_ARGUMENT", message="`reason` must be a string.")

        with self._cv:
            if self._active_turn_id is None:
                raise SubagentError(
                    code="WORKER_NOT_RUNNING",
                    message="worker has no active turn to cancel",
                    details={"workerId": self.worker_id},
                )
            self._cancel_requested = True
            self._cancel_reason = reason
            pending = self._pending_permission

        if pending is not None:
            request_id = str(pending["request_id"])
            request = self.store.get_approval_request(self.worker_id, request_id)
            if request is not None:
                self.store.decide_approval_request(
                    self.worker_id,
                    request_id,
                    decision="cancelled",
                    selected_option_id="cancelled",
                    selected_alias="cancelled",
                    note=reason,
                )
                turn_id = request.get("turn_id")
                self.store.append_worker_event(
                    self.worker_id,
                    event_type="approval.decided",
                    turn_id=str(turn_id) if isinstance(turn_id, str) else None,
                    data={
                        "requestId": request_id,
                        "decision": "cancelled",
                        "optionId": "cancelled",
                        "alias": "cancelled",
                        "note": reason,
                    },
                    raw={"runtime": "acp-stdio", "phase": "approval.decided"},
                )
            self.store.update_worker_state(self.worker_id, next_state=WORKER_STATE_RUNNING)
            with self._cv:
                if self._pending_permission is not None:
                    self._pending_permission["response"] = {"outcome": {"outcome": "cancelled"}}
                    self._cv.notify_all()
        else:
            assert self.client is not None
            self.client.notify(
                "session/cancel",
                {"sessionId": self.session_id},
            )

        result = self._wait_for_turn_terminal()
        return {
            "workerId": self.worker_id,
            "state": result["state"],
            "eventId": result["eventId"],
            "turnId": result["turnId"],
        }

    def _handle_stop(self, params: dict[str, Any]) -> dict[str, Any]:
        reason = params.get("reason")
        if reason is None:
            reason = "worker stopped"
        if not isinstance(reason, str):
            raise SubagentError(code="INVALID_ARGUMENT", message="`reason` must be a string.")
        with self._cv:
            active_turn_id = self._active_turn_id
        if active_turn_id is not None:
            self._handle_cancel_turn({"reason": reason})
        self._shutdown_requested = True
        server_socket = self.server_socket
        if server_socket is not None:
            try:
                server_socket.close()
            except OSError:
                pass
        return {"workerId": self.worker_id, "stopped": True}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="subagent worker runtime")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--socket-path", required=True)
    parser.add_argument("--launcher-command", required=True)
    parser.add_argument("--launcher-args-json", required=True)
    parser.add_argument("--launcher-env-json", required=True)
    parser.add_argument("--cwd", required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    launcher_args = json.loads(args.launcher_args_json)
    launcher_env = json.loads(args.launcher_env_json)
    if not isinstance(launcher_args, list):
        raise SystemExit(2)
    if not isinstance(launcher_env, dict):
        raise SystemExit(2)
    runtime = WorkerRuntime(
        db_path=Path(args.db_path).expanduser().resolve(),
        worker_id=str(args.worker_id),
        socket_path=Path(args.socket_path),
        launcher_command=str(args.launcher_command),
        launcher_args=[str(item) for item in launcher_args],
        launcher_env={str(key): str(value) for key, value in launcher_env.items()},
        cwd=Path(args.cwd).expanduser().resolve(),
    )
    raise SystemExit(runtime.run())


if __name__ == "__main__":
    main()
