"""Worker runtime process management and IPC helpers."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import Launcher, SubagentConfig
from .constants import RUNTIME_STARTUP_TIMEOUT_SECONDS
from .errors import SubagentError
from .launcher_service import resolve_launcher_spec
from .state import StateStore


def _backend_error_code_for_category(category: str) -> str:
    mapping = {
        "permission": "BACKEND_PERMISSION_DENIED",
        "socket": "BACKEND_SOCKET_UNREACHABLE",
        "timeout": "BACKEND_TIMEOUT",
        "launcher": "BACKEND_LAUNCHER",
        "worker": "BACKEND_WORKER_CONFIG",
    }
    return mapping.get(category, "BACKEND_UNAVAILABLE")


def _read_runtime_log_tail(log_path: Path, *, max_chars: int = 4000) -> str | None:
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    trimmed = content.strip()
    if not trimmed:
        return None
    if len(trimmed) > max_chars:
        trimmed = trimmed[-max_chars:]
    return trimmed


def _classify_backend_unavailable(error: str | None, *, log_tail: str | None = None) -> tuple[str, str]:
    lowered = "\n".join(part for part in (error, log_tail) if isinstance(part, str)).lower()
    if any(token in lowered for token in ("operation not permitted", "permission denied", "eacces", "eperm")):
        return (
            "permission",
            "Likely blocked by sandbox/socket permissions. Retry the same command with outside-sandbox execution approval.",
        )
    if "address already in use" in lowered:
        return (
            "socket",
            "Runtime socket appears busy. Stop stale workers and retry.",
        )
    if any(token in lowered for token in ("timed out", "timeout")):
        return (
            "timeout",
            "Runtime startup timed out. Check runtime logs and launcher health, then retry.",
        )
    if any(token in lowered for token in ("no such file", "not found", "executable file")):
        return (
            "launcher",
            "Launcher command may be missing. Run `subagent launcher probe <launcher> --json` and verify PATH.",
        )
    return (
        "unknown",
        "Inspect runtime logs and retry. If running in a sandbox, re-run the same command outside sandbox.",
    )


def runtime_socket_path(store: StateStore, worker_id: str) -> Path:
    digest = hashlib.sha1(f"{store.db_path}:{worker_id}".encode("utf-8")).hexdigest()[:16]
    return Path("/tmp") / f"subagent-rt-{digest}.sock"


def runtime_log_path(store: StateStore, worker_id: str) -> Path:
    return store.db_path.parent / "runtimes" / f"{worker_id}.log"


def _send_socket_request(
    socket_path: Path,
    *,
    method: str,
    params: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout_seconds)
            client.connect(str(socket_path))
            payload = {
                "method": method,
                "params": params,
            }
            request_data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            client.sendall(request_data)
            chunks: list[bytes] = []
            while True:
                block = client.recv(4096)
                if not block:
                    break
                chunks.append(block)
                if b"\n" in block:
                    break
    except socket.timeout as exc:
        category = "timeout"
        raise SubagentError(
            code=_backend_error_code_for_category(category),
            message="Worker runtime request timed out.",
            details={
                "socketPath": str(socket_path),
                "method": method,
                "error": str(exc),
                "reasonCategory": category,
                "recommendedAction": (
                    "Worker runtime did not respond before timeout. "
                    "Retry once, then restart the worker runtime if needed."
                ),
            },
        ) from exc
    except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
        category = "socket"
        raise SubagentError(
            code=_backend_error_code_for_category(category),
            message="Worker runtime is not reachable.",
            details={
                "socketPath": str(socket_path),
                "method": method,
                "error": str(exc),
                "reasonCategory": category,
                "recommendedAction": (
                    "Worker runtime endpoint is unreachable. Retry once; "
                    "if it keeps failing, restart the worker runtime."
                ),
            },
        ) from exc
    raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
    if not raw:
        raise SubagentError(
            code="BACKEND_PROTOCOL_ERROR",
            message="Worker runtime returned an empty response.",
            details={"socketPath": str(socket_path), "method": method},
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SubagentError(
            code="BACKEND_PROTOCOL_ERROR",
            message="Worker runtime returned invalid JSON.",
            details={"socketPath": str(socket_path), "method": method, "response": raw},
        ) from exc
    if not isinstance(parsed, dict):
        raise SubagentError(
            code="BACKEND_PROTOCOL_ERROR",
            message="Worker runtime returned non-object response.",
            details={"socketPath": str(socket_path), "method": method, "response": parsed},
        )
    return parsed


def runtime_request(
    store: StateStore,
    *,
    worker_id: str,
    method: str,
    params: dict[str, Any],
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    worker = store.get_worker(worker_id)
    if worker is None:
        raise SubagentError(
            code="WORKER_NOT_FOUND",
            message=f"Worker not found: {worker_id}",
            details={"workerId": worker_id},
        )
    socket_path_raw = worker.get("runtime_socket")
    if not isinstance(socket_path_raw, str) or not socket_path_raw:
        category = "socket"
        raise SubagentError(
            code=_backend_error_code_for_category(category),
            message="Worker runtime is not initialized.",
            details={
                "workerId": worker_id,
                "reasonCategory": category,
                "recommendedAction": "Worker runtime endpoint is missing. Restart worker runtime and retry.",
            },
        )
    socket_path = Path(socket_path_raw)
    response = _send_socket_request(
        socket_path,
        method=method,
        params=params,
        timeout_seconds=timeout_seconds,
    )
    ok = response.get("ok")
    if ok is not True:
        error_payload = response.get("error")
        if isinstance(error_payload, dict):
            raise SubagentError(
                code=str(error_payload.get("code", "BACKEND_RPC_ERROR")),
                message=str(error_payload.get("message", "Worker runtime request failed.")),
                retryable=bool(error_payload.get("retryable", False)),
                details=error_payload.get("details")
                if isinstance(error_payload.get("details"), dict)
                else {},
            )
        raise SubagentError(
            code="BACKEND_RPC_ERROR",
            message="Worker runtime request failed.",
            details={"response": response},
        )
    result = response.get("result")
    if not isinstance(result, dict):
        raise SubagentError(
            code="BACKEND_PROTOCOL_ERROR",
            message="Worker runtime response missing result object.",
            details={"response": response},
        )
    return result


def _runtime_launch_command(
    *,
    store: StateStore,
    worker_id: str,
    socket_path: Path,
    launcher: Launcher,
    cwd: str,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "subagent.worker_runtime",
        "--db-path",
        str(store.db_path),
        "--worker-id",
        worker_id,
        "--socket-path",
        str(socket_path),
        "--launcher-command",
        launcher.command,
        "--launcher-args-json",
        json.dumps(launcher.args, ensure_ascii=False),
        "--launcher-env-json",
        json.dumps(launcher.env, ensure_ascii=False),
        "--cwd",
        cwd,
    ]


def launch_worker_runtime(
    store: StateStore,
    *,
    worker_id: str,
    launcher: Launcher,
    cwd: str,
    timeout_seconds: float = RUNTIME_STARTUP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    socket_path = runtime_socket_path(store, worker_id)
    if socket_path.exists():
        socket_path.unlink()

    log_path = runtime_log_path(store, worker_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = _runtime_launch_command(
        store=store,
        worker_id=worker_id,
        socket_path=socket_path,
        launcher=launcher,
        cwd=cwd,
    )
    with log_path.open("a", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
            env=dict(os.environ),
        )
    store.set_worker_runtime_endpoint(
        worker_id,
        runtime_pid=process.pid,
        runtime_socket=str(socket_path),
    )

    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            last_error = f"runtime exited with code {process.returncode}"
            break
        if socket_path.exists():
            try:
                response = _send_socket_request(
                    socket_path,
                    method="ping",
                    params={},
                    timeout_seconds=1.0,
                )
            except SubagentError as error:
                detail_error = error.details.get("error") if isinstance(error.details, dict) else None
                if isinstance(detail_error, str) and detail_error:
                    last_error = detail_error
                else:
                    last_error = error.message
            else:
                if response.get("ok") is True:
                    return {
                        "pid": process.pid,
                        "socketPath": str(socket_path),
                        "logPath": str(log_path),
                    }
        time.sleep(0.1)
    if last_error is None:
        last_error = "runtime startup timed out before socket became ready"
    try:
        process.terminate()
        process.wait(timeout=1.0)
    except Exception:  # pragma: no cover - cleanup best effort
        try:
            process.kill()
        except Exception:
            pass
    store.clear_worker_runtime_endpoint(worker_id)
    log_tail = _read_runtime_log_tail(log_path)
    reason_category, recommended_action = _classify_backend_unavailable(last_error, log_tail=log_tail)
    details: dict[str, Any] = {
        "workerId": worker_id,
        "command": command,
        "socketPath": str(socket_path),
        "logPath": str(log_path),
        "error": last_error,
        "reasonCategory": reason_category,
        "recommendedAction": recommended_action,
    }
    if log_tail is not None:
        details["logTail"] = log_tail
    if reason_category == "permission":
        details["recommendedCommand"] = "Re-run the same subagent command outside sandbox."
    raise SubagentError(
        code=_backend_error_code_for_category(reason_category),
        message="Failed to start worker runtime.",
        details=details,
    )


def restart_worker_runtime(
    store: StateStore,
    config: SubagentConfig,
    *,
    worker_id: str,
    timeout_seconds: float = RUNTIME_STARTUP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    worker = store.get_worker(worker_id)
    if worker is None:
        raise SubagentError(
            code="WORKER_NOT_FOUND",
            message=f"Worker not found: {worker_id}",
            details={"workerId": worker_id},
        )
    launcher_name = worker.get("launcher")
    if not isinstance(launcher_name, str) or not launcher_name:
        category = "launcher"
        raise SubagentError(
            code=_backend_error_code_for_category(category),
            message="Worker launcher is not set.",
            details={
                "workerId": worker_id,
                "reasonCategory": category,
                "recommendedAction": "Worker metadata is incomplete. Restart worker from manager.",
            },
        )
    launcher = config.launchers.get(launcher_name)
    if launcher is None:
        raise SubagentError(
            code="LAUNCHER_NOT_FOUND",
            message=f"Launcher not found: {launcher_name}",
            details={"workerId": worker_id, "launcher": launcher_name},
        )
    if launcher.backend_kind != "acp-stdio":
        category = "launcher"
        raise SubagentError(
            code=_backend_error_code_for_category(category),
            message=f"Unsupported backend kind for runtime: {launcher.backend_kind}",
            details={
                "workerId": worker_id,
                "launcher": launcher_name,
                "backendKind": launcher.backend_kind,
                "reasonCategory": category,
                "recommendedAction": "Use an `acp-stdio` launcher for runtime-backed workers.",
            },
        )
    resolved = resolve_launcher_spec(launcher)
    if not resolved.available:
        category = "launcher"
        raise SubagentError(
            code=_backend_error_code_for_category(category),
            message=f"Launcher command not available: {launcher.command}",
            details={
                "workerId": worker_id,
                "launcher": launcher_name,
                "command": launcher.command,
                "effectiveCommand": resolved.command,
                "effectiveArgs": resolved.args,
                "reasonCategory": category,
                "recommendedAction": (
                    "Install/fix the launcher command and run "
                    f"`subagent launcher probe {launcher_name} --json` before retrying."
                ),
            },
        )
    runtime_launcher = Launcher(
        name=launcher.name,
        backend_kind=launcher.backend_kind,
        command=resolved.command,
        args=resolved.args,
        env=dict(launcher.env),
    )
    worker_cwd = worker.get("cwd")
    if not isinstance(worker_cwd, str) or not worker_cwd:
        category = "worker"
        raise SubagentError(
            code=_backend_error_code_for_category(category),
            message="Worker cwd is not available.",
            details={
                "workerId": worker_id,
                "reasonCategory": category,
                "recommendedAction": "Worker metadata is incomplete. Restart worker from manager.",
            },
        )
    return launch_worker_runtime(
        store,
        worker_id=worker_id,
        launcher=runtime_launcher,
        cwd=worker_cwd,
        timeout_seconds=timeout_seconds,
    )


def stop_worker_runtime(
    store: StateStore,
    *,
    worker_id: str,
    reason: str = "worker stopped",
) -> None:
    worker = store.get_worker(worker_id)
    if worker is None:
        return
    runtime_socket = worker.get("runtime_socket")
    if isinstance(runtime_socket, str) and runtime_socket:
        try:
            runtime_request(
                store,
                worker_id=worker_id,
                method="stop",
                params={"reason": reason},
                timeout_seconds=5.0,
            )
        except SubagentError:
            # Stopping should be best-effort.
            pass
    store.clear_worker_runtime_endpoint(worker_id)
