"""Minimal ACP stdio JSON-RPC client used by turn execution."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .errors import SubagentError

NotificationHandler = Callable[[str, dict[str, Any]], None]
RequestHandler = Callable[[str, dict[str, Any]], Any]


class AcpStdioClient:
    """Line-oriented JSON-RPC client for ACP stdio adapters."""

    def __init__(
        self,
        *,
        command: str,
        args: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> None:
        merged_env = dict(os.environ)
        if env:
            merged_env.update(env)
        try:
            self._proc = subprocess.Popen(
                [command, *args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(cwd),
                env=merged_env,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise SubagentError(
                code="BACKEND_UNAVAILABLE",
                message=f"Launcher command not found: {command}",
                details={"command": command},
            ) from exc
        except OSError as exc:
            raise SubagentError(
                code="BACKEND_UNAVAILABLE",
                message=f"Failed to start launcher command: {command}",
                details={"command": command, "error": str(exc)},
            ) from exc

        self._next_request_id = 1
        self._messages: queue.Queue[dict[str, Any] | object] = queue.Queue()
        self._pending_responses: dict[str, dict[str, Any]] = {}
        self._stderr_lines: deque[str] = deque(maxlen=80)
        self._eof_token = object()
        self._closed = False
        self._request_id_lock = threading.Lock()
        self._send_lock = threading.Lock()

        self._stdout_thread = threading.Thread(
            target=self._stdout_reader,
            name="acp-stdout-reader",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._stderr_reader,
            name="acp-stderr-reader",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=0.5)

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = 60.0,
        on_notification: NotificationHandler | None = None,
        on_request: RequestHandler | None = None,
    ) -> Any:
        with self._request_id_lock:
            request_id = self._next_request_id
            self._next_request_id += 1

        self._send_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )
        return self._wait_for_response(
            request_id=request_id,
            method=method,
            timeout_seconds=timeout_seconds,
            on_notification=on_notification,
            on_request=on_request,
        )

    def notify(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        self._send_jsonrpc(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
            }
        )

    def _wait_for_response(
        self,
        *,
        request_id: int,
        method: str,
        timeout_seconds: float,
        on_notification: NotificationHandler | None,
        on_request: RequestHandler | None,
    ) -> Any:
        response_key = str(request_id)
        if response_key in self._pending_responses:
            return self._consume_response(response_key, method)

        deadline = time.monotonic() + timeout_seconds
        while True:
            if response_key in self._pending_responses:
                return self._consume_response(response_key, method)
            if time.monotonic() >= deadline:
                raise SubagentError(
                    code="BACKEND_TIMEOUT",
                    message=f"Timed out waiting for backend response: {method}",
                    retryable=True,
                    details={"method": method, "timeoutSeconds": timeout_seconds},
                )
            remaining = max(0.01, deadline - time.monotonic())
            try:
                item = self._messages.get(timeout=remaining)
            except queue.Empty:
                if self._proc.poll() is not None:
                    raise SubagentError(
                        code="BACKEND_UNAVAILABLE",
                        message=f"Backend process exited while waiting for `{method}`.",
                        details={
                            "method": method,
                            "exitCode": self._proc.returncode,
                            "stderrTail": list(self._stderr_lines),
                        },
                    )
                continue

            if item is self._eof_token:
                raise SubagentError(
                    code="BACKEND_UNAVAILABLE",
                    message=f"Backend stream closed while waiting for `{method}`.",
                    details={
                        "method": method,
                        "exitCode": self._proc.poll(),
                        "stderrTail": list(self._stderr_lines),
                    },
                )
            if not isinstance(item, dict):
                continue

            kind = item.get("_kind")
            if kind == "parse_error":
                raise SubagentError(
                    code="BACKEND_PROTOCOL_ERROR",
                    message="Received non-JSON message from backend",
                    details={"line": item.get("line"), "error": item.get("error")},
                )
            if kind != "jsonrpc":
                continue

            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue
            self._dispatch_message(
                payload,
                on_notification=on_notification,
                on_request=on_request,
            )
            if response_key in self._pending_responses:
                return self._consume_response(response_key, method)

    def _dispatch_message(
        self,
        message: dict[str, Any],
        *,
        on_notification: NotificationHandler | None,
        on_request: RequestHandler | None,
    ) -> None:
        if "id" in message and ("result" in message or "error" in message):
            response_id = str(message.get("id"))
            self._pending_responses[response_id] = message
            return

        method = message.get("method")
        if not isinstance(method, str):
            return
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}

        if "id" in message:
            self._handle_server_request(
                request_id=message.get("id"),
                method=method,
                params=params,
                on_request=on_request,
            )
            return
        if on_notification is not None:
            on_notification(method, params)

    def _handle_server_request(
        self,
        *,
        request_id: Any,
        method: str,
        params: dict[str, Any],
        on_request: RequestHandler | None,
    ) -> None:
        if on_request is None:
            self._send_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method}",
                    },
                }
            )
            return
        try:
            result = on_request(method, params)
        except SubagentError as error:
            self._send_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32000,
                        "message": error.message,
                        "data": error.to_dict(),
                    },
                }
            )
            return
        except Exception as error:  # pragma: no cover - safety fallback
            self._send_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32603,
                        "message": f"Internal handler error: {error}",
                    },
                }
            )
            return

        self._send_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result if result is not None else {},
            }
        )

    def _consume_response(self, response_key: str, method: str) -> Any:
        response = self._pending_responses.pop(response_key)
        if "error" in response:
            error = response["error"]
            raise SubagentError(
                code="BACKEND_RPC_ERROR",
                message=f"Backend rejected `{method}` request.",
                details={"method": method, "error": error},
            )
        return response.get("result")

    def _send_jsonrpc(self, payload: dict[str, Any]) -> None:
        with self._send_lock:
            if self._proc.poll() is not None:
                raise SubagentError(
                    code="BACKEND_UNAVAILABLE",
                    message="Backend process is not running.",
                    details={
                        "exitCode": self._proc.returncode,
                        "stderrTail": list(self._stderr_lines),
                    },
                )
            stdin = self._proc.stdin
            if stdin is None:
                raise SubagentError(
                    code="BACKEND_UNAVAILABLE",
                    message="Backend stdin is not available.",
                )
            message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            try:
                stdin.write(message + "\n")
                stdin.flush()
            except BrokenPipeError as exc:
                raise SubagentError(
                    code="BACKEND_UNAVAILABLE",
                    message="Backend process closed stdin.",
                    details={
                        "exitCode": self._proc.poll(),
                        "stderrTail": list(self._stderr_lines),
                    },
                ) from exc

    def _stdout_reader(self) -> None:
        stdout = self._proc.stdout
        if stdout is None:
            self._messages.put(self._eof_token)
            return
        for raw_line in stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as error:
                self._messages.put(
                    {
                        "_kind": "parse_error",
                        "line": line,
                        "error": str(error),
                    }
                )
                continue
            self._messages.put({"_kind": "jsonrpc", "payload": parsed})
        self._messages.put(self._eof_token)

    def _stderr_reader(self) -> None:
        stderr = self._proc.stderr
        if stderr is None:
            return
        for raw_line in stderr:
            self._stderr_lines.append(raw_line.rstrip("\n"))
