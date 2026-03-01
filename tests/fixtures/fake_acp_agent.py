#!/usr/bin/env python3
"""Minimal ACP stdio fake server used by integration tests."""

from __future__ import annotations

import json
import sys


def _write(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _read_message() -> dict[str, object] | None:
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return {}
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _extract_prompt_text(prompt: object) -> str:
    if not isinstance(prompt, list):
        return ""
    texts: list[str] = []
    for block in prompt:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        text = block.get("text")
        if block_type == "text" and isinstance(text, str):
            texts.append(text)
    return "\n".join(texts)


def _wait_for_request_response(request_id: int) -> dict[str, object]:
    while True:
        message = _read_message()
        if message is None:
            return {}
        if message.get("id") != request_id:
            continue
        if "result" in message and isinstance(message["result"], dict):
            return message["result"]
        return {}


def main() -> int:
    session_counter = 0
    known_sessions: set[str] = set()

    while True:
        message = _read_message()
        if message is None:
            return 0
        if not message:
            continue

        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": 1,
                        "serverInfo": {"name": "fake-acp", "version": "test"},
                    },
                }
            )
            continue

        if method == "session/new":
            session_counter += 1
            session_id = f"sess_fake_{session_counter}"
            known_sessions.add(session_id)
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"sessionId": session_id},
                }
            )
            continue

        if method == "session/load":
            params = message.get("params")
            session_id = ""
            if isinstance(params, dict):
                raw_session_id = params.get("sessionId")
                if isinstance(raw_session_id, str):
                    session_id = raw_session_id
            if session_id in known_sessions or session_id.startswith("sess_fake_"):
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"sessionId": session_id},
                    }
                )
            else:
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32001, "message": "session not found"},
                    }
                )
            continue

        if method == "session/prompt":
            params = message.get("params")
            session_id = "sess_fake"
            prompt_text = ""
            if isinstance(params, dict):
                raw_session_id = params.get("sessionId")
                if isinstance(raw_session_id, str) and raw_session_id:
                    session_id = raw_session_id
                prompt_text = _extract_prompt_text(params.get("prompt"))

            _write(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": session_id,
                        "update": {
                            "agent_message_chunk": {
                                "content": [{"type": "text", "text": "STATUS: fake backend started."}]
                            }
                        },
                    },
                }
            )

            if "cancelable turn" in prompt_text:
                while True:
                    cancel_message = _read_message()
                    if cancel_message is None:
                        return 0
                    cancel_method = cancel_message.get("method")
                    cancel_id = cancel_message.get("id")
                    if cancel_method != "session/cancel":
                        if cancel_id is not None:
                            _write(
                                {
                                    "jsonrpc": "2.0",
                                    "id": cancel_id,
                                    "error": {"code": -32601, "message": f"Unknown method: {cancel_method}"},
                                }
                            )
                        continue
                    _write({"jsonrpc": "2.0", "id": cancel_id, "result": {"ok": True}})
                    _write(
                        {
                            "jsonrpc": "2.0",
                            "method": "session/update",
                            "params": {
                                "sessionId": session_id,
                                "update": {
                                    "agent_message_chunk": {
                                        "content": [{"type": "text", "text": "BLOCKED: canceled by manager."}]
                                    }
                                },
                            },
                        }
                    )
                    _write(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": {"stopReason": "cancelled"},
                        }
                    )
                    break
                continue

            if "needs permission" in prompt_text:
                permission_id = 9001
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": permission_id,
                        "method": "session/request_permission",
                        "params": {
                            "sessionId": session_id,
                            "toolCall": {"kind": "shell"},
                            "options": [
                                {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                                {"optionId": "deny", "name": "Deny", "kind": "reject"},
                            ],
                        },
                    }
                )
                permission_result = _wait_for_request_response(permission_id)
                selected = "unknown"
                if isinstance(permission_result, dict):
                    outcome = permission_result.get("outcome")
                    if isinstance(outcome, dict):
                        option_id = outcome.get("optionId")
                        if isinstance(option_id, str) and option_id:
                            selected = option_id
                _write(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": session_id,
                            "update": {
                                "agent_message_chunk": {
                                    "content": [
                                        {"type": "text", "text": f"ASK: permission resolved as `{selected}`."}
                                    ]
                                }
                            },
                        },
                    }
                )

            _write(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": session_id,
                        "update": {
                            "agent_message_chunk": {
                                "content": [{"type": "text", "text": "DONE: fake backend complete."}]
                            }
                        },
                    },
                }
            )
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"stopReason": "end_turn"},
                }
            )
            continue

        _write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
            }
        )


if __name__ == "__main__":
    raise SystemExit(main())
