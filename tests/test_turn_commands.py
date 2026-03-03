from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

from subagent.cli import app
from subagent.errors import SubagentError

SAMPLE_CONFIG = """
launchers:
  codex:
    backend:
      kind: acp-stdio
    command: codex-acp
    args: []
    env: {}
profiles:
  worker-default:
    promptLanguage: en
    responseLanguage: same_as_manager
    defaultPacks:
      - repo-conventions
    bootstrap: |
      You are a worker subagent.
packs:
  repo-conventions:
    description: Follow repo conventions
    prompt: |
      Keep changes small.
defaults:
  launcher: codex
  profile: worker-default
"""


class TurnCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.root / "state"
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(SAMPLE_CONFIG.strip() + "\n", encoding="utf-8")
        self.runner = CliRunner()
        self.base_env = {
            "SUBAGENT_CONFIG": str(self.config_path),
            "SUBAGENT_STATE_DIR": str(self.state_dir),
            "SUBAGENT_CTL_ID": "",
            "SUBAGENT_CTL_EPOCH": "",
            "SUBAGENT_CTL_TOKEN": "",
        }

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def invoke(
        self,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
    ):
        merged_env = dict(self.base_env)
        if env:
            merged_env.update(env)
        return self.runner.invoke(
            app,
            args,
            input=input_text,
            env=merged_env,
            catch_exceptions=False,
        )

    def init_controller(self) -> None:
        result = self.invoke(["controller", "init", "--cwd", str(self.workspace), "--json"])
        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])

    def start_worker(self) -> str:
        self.init_controller()
        result = self.invoke(
            ["worker", "start", "--cwd", str(self.workspace), "--debug-mode", "--json"]
        )
        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        return str(payload["data"]["workerId"])

    def test_send_fails_when_backend_unavailable_without_simulate(self) -> None:
        broken_config_path = self.root / "broken-config.json"
        broken = {
            "launchers": {
                "broken": {
                    "backend": {"kind": "acp-stdio"},
                    "command": "nonexistent-acp-command-for-test",
                    "args": [],
                    "env": {},
                }
            },
            "profiles": {
                "worker-default": {
                    "promptLanguage": "en",
                    "responseLanguage": "same_as_manager",
                    "defaultPacks": ["repo-conventions"],
                    "bootstrap": "You are a worker subagent.",
                }
            },
            "packs": {
                "repo-conventions": {
                    "description": "Follow repo conventions",
                    "prompt": "Keep changes small.",
                }
            },
            "defaults": {"launcher": "broken", "profile": "worker-default"},
        }
        broken_config_path.write_text(json.dumps(broken), encoding="utf-8")
        env = {"SUBAGENT_CONFIG": str(broken_config_path)}

        init_result = self.invoke(
            ["controller", "init", "--cwd", str(self.workspace), "--json"],
            env=env,
        )
        self.assertEqual(init_result.exit_code, 0)
        start_result = self.invoke(
            ["worker", "start", "--cwd", str(self.workspace), "--debug-mode", "--json"],
            env=env,
        )
        self.assertEqual(start_result.exit_code, 0)
        start_payload = json.loads(start_result.stdout)
        worker_id = str(start_payload["data"]["workerId"])
        send_result = self.invoke(
            ["send", "--worker-id", worker_id, "--text", "strict by default", "--json"],
            env=env,
        )
        self.assertEqual(send_result.exit_code, 1)
        payload = json.loads(send_result.stdout)
        self.assertIn(
            payload["error"]["code"],
            {"BACKEND_TIMEOUT", "BACKEND_SOCKET_UNREACHABLE", "BACKEND_PERMISSION_DENIED", "BACKEND_LAUNCHER"},
        )
        details = payload["error"]["details"]
        self.assertIn("recommendedAction", details)

    def test_send_wait_and_watch_ndjson(self) -> None:
        worker_id = self.start_worker()
        send_result = self.invoke(
            [
                "send",
                "--worker-id",
                worker_id,
                "--text",
                "Investigate flaky test",
                "--debug-mode",
                "--no-wait",
                "--json",
            ]
        )
        self.assertEqual(send_result.exit_code, 0)
        send_payload = json.loads(send_result.stdout)
        self.assertEqual(send_payload["type"], "turn.accepted")
        self.assertEqual(send_payload["data"]["state"], "idle")

        wait_result = self.invoke(
            [
                "wait",
                "--worker-id",
                worker_id,
                "--include-history",
                "--until",
                "turn.completed",
                "--timeout-seconds",
                "1",
                "--json",
            ]
        )
        self.assertEqual(wait_result.exit_code, 0)
        wait_payload = json.loads(wait_result.stdout)
        self.assertEqual(wait_payload["type"], "event.matched")
        self.assertEqual(wait_payload["data"]["type"], "turn.completed")

        watch_result = self.invoke(["watch", "--worker-id", worker_id, "--ndjson"])
        self.assertEqual(watch_result.exit_code, 0)
        lines = [line for line in watch_result.stdout.splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 3)
        first_event = json.loads(lines[0])
        self.assertEqual(first_event["schemaVersion"], "v1")
        self.assertEqual(first_event["workerId"], worker_id)
        self.assertIn("eventId", first_event)
        self.assertIn("ts", first_event)
        self.assertIn("type", first_event)
        self.assertIn("data", first_event)

    def test_send_waits_by_default_and_returns_assistant_text(self) -> None:
        worker_id = self.start_worker()
        send_result = self.invoke(
            [
                "send",
                "--worker-id",
                worker_id,
                "--text",
                "default wait path",
                "--debug-mode",
                "--json",
            ]
        )
        self.assertEqual(send_result.exit_code, 0)
        send_payload = json.loads(send_result.stdout)
        self.assertEqual(send_payload["type"], "turn.waited")
        self.assertEqual(send_payload["data"]["matchedEvent"]["type"], "turn.completed")
        self.assertIn("assistantText", send_payload["data"])
        self.assertEqual(
            send_payload["data"]["assistantText"],
            "STATUS: turn accepted and completed in local runtime.",
        )
        self.assertEqual(
            send_payload["data"]["lastAssistantMessage"],
            send_payload["data"]["assistantText"],
        )
        self.assertEqual(
            send_payload["data"]["lastAssistantChunk"],
            send_payload["data"]["assistantText"],
        )

    def test_send_json_warns_when_text_flag_contains_shell_pitfalls(self) -> None:
        worker_id = self.start_worker()
        send_result = self.invoke(
            [
                "send",
                "--worker-id",
                worker_id,
                "--text",
                "Use `echo hello` and $(uname) safely",
                "--debug-mode",
                "--no-wait",
                "--json",
            ]
        )
        self.assertEqual(send_result.exit_code, 0)
        payload = json.loads(send_result.stdout)
        self.assertEqual(payload["type"], "turn.accepted")
        warnings = payload["data"].get("warnings")
        self.assertIsInstance(warnings, list)
        assert isinstance(warnings, list)
        self.assertGreaterEqual(len(warnings), 1)
        first = warnings[0]
        self.assertEqual(first.get("code"), "TEXT_SHELL_PITFALL")
        risk_codes = first.get("riskCodes")
        self.assertIsInstance(risk_codes, list)
        assert isinstance(risk_codes, list)
        self.assertIn("backticks", risk_codes)
        self.assertIn("commandSubstitution", risk_codes)

    def test_send_with_text_file_succeeds(self) -> None:
        worker_id = self.start_worker()
        text_path = self.root / "instruction.txt"
        text_path.write_text("STATUS: from text file", encoding="utf-8")
        send_result = self.invoke(
            [
                "send",
                "--worker-id",
                worker_id,
                "--text-file",
                str(text_path),
                "--debug-mode",
                "--no-wait",
                "--json",
            ]
        )
        self.assertEqual(send_result.exit_code, 0)
        payload = json.loads(send_result.stdout)
        self.assertEqual(payload["type"], "turn.accepted")
        self.assertNotIn("warnings", payload["data"])

    def test_send_with_text_stdin_succeeds(self) -> None:
        worker_id = self.start_worker()
        send_result = self.invoke(
            [
                "send",
                "--worker-id",
                worker_id,
                "--text-stdin",
                "--debug-mode",
                "--no-wait",
                "--json",
            ],
            input_text="STATUS: from stdin",
        )
        self.assertEqual(send_result.exit_code, 0)
        payload = json.loads(send_result.stdout)
        self.assertEqual(payload["type"], "turn.accepted")
        self.assertNotIn("warnings", payload["data"])

    def test_send_rejects_multiple_text_sources(self) -> None:
        worker_id = self.start_worker()
        text_path = self.root / "instruction-dup.txt"
        text_path.write_text("duplicate source", encoding="utf-8")
        send_result = self.invoke(
            [
                "send",
                "--worker-id",
                worker_id,
                "--text",
                "from text flag",
                "--text-file",
                str(text_path),
                "--json",
            ]
        )
        self.assertEqual(send_result.exit_code, 1)
        payload = json.loads(send_result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "INVALID_INPUT")
        self.assertIn("exactly one", payload["error"]["message"])

    def test_send_input_json_avoids_shell_pitfall_warning(self) -> None:
        worker_id = self.start_worker()
        payload_path = self.root / "send-input.json"
        payload_path.write_text(
            json.dumps(
                {
                    "workerId": worker_id,
                    "text": "Use `echo hello` and $(uname) safely",
                    "wait": False,
                    "debugMode": True,
                }
            ),
            encoding="utf-8",
        )
        send_result = self.invoke(["send", "--input", str(payload_path), "--json"])
        self.assertEqual(send_result.exit_code, 0)
        payload = json.loads(send_result.stdout)
        self.assertEqual(payload["type"], "turn.accepted")
        self.assertNotIn("warnings", payload["data"])

    def test_send_input_rejects_worker_alias(self) -> None:
        worker_id = self.start_worker()
        payload_path = self.root / "send-input-worker-alias.json"
        payload_path.write_text(
            json.dumps(
                {
                    "worker": worker_id,
                    "text": "worker alias support",
                    "wait": False,
                    "debugMode": True,
                }
            ),
            encoding="utf-8",
        )
        send_result = self.invoke(["send", "--input", str(payload_path), "--json"])
        self.assertEqual(send_result.exit_code, 1)
        payload = json.loads(send_result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "INVALID_INPUT")
        self.assertIn("not supported", payload["error"]["message"])

    def test_send_input_rejects_conflicting_worker_fields(self) -> None:
        worker_id = self.start_worker()
        payload_path = self.root / "send-input-conflicting-worker-fields.json"
        payload_path.write_text(
            json.dumps(
                {
                    "workerId": worker_id,
                    "worker": "w_conflict",
                    "text": "worker mismatch should fail",
                    "wait": False,
                    "debugMode": True,
                }
            ),
            encoding="utf-8",
        )
        send_result = self.invoke(["send", "--input", str(payload_path), "--json"])
        self.assertEqual(send_result.exit_code, 1)
        payload = json.loads(send_result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "INVALID_INPUT")
        self.assertIn("not supported", payload["error"]["message"])

    def test_send_with_wait_returns_matched_event(self) -> None:
        worker_id = self.start_worker()
        send_result = self.invoke(
            [
                "send",
                "--worker-id",
                worker_id,
                "--text",
                "Needs approval",
                "--request-approval",
                "--wait",
                "--wait-timeout-seconds",
                "1",
                "--json",
            ]
        )
        self.assertEqual(send_result.exit_code, 0)
        send_payload = json.loads(send_result.stdout)
        self.assertEqual(send_payload["type"], "turn.waited")
        self.assertEqual(send_payload["data"]["matchedEvent"]["type"], "approval.requested")
        self.assertIsNotNone(send_payload["data"]["requestId"])
        self.assertEqual(send_payload["data"]["state"], "waiting_approval")

    def test_send_with_wait_timeout_zero_uses_no_deadline_mode(self) -> None:
        worker_id = self.start_worker()
        send_result = self.invoke(
            [
                "send",
                "--worker-id",
                worker_id,
                "--text",
                "Needs approval",
                "--request-approval",
                "--wait",
                "--wait-timeout-seconds",
                "0",
                "--json",
            ]
        )
        self.assertEqual(send_result.exit_code, 0)
        send_payload = json.loads(send_result.stdout)
        self.assertEqual(send_payload["type"], "turn.waited")
        self.assertEqual(send_payload["data"]["matchedEvent"]["type"], "approval.requested")

    def test_wait_uses_default_until_set(self) -> None:
        worker_id = self.start_worker()
        send_result = self.invoke(
            [
                "send",
                "--worker-id",
                worker_id,
                "--text",
                "Needs approval",
                "--request-approval",
                "--json",
            ]
        )
        self.assertEqual(send_result.exit_code, 0)
        send_payload = json.loads(send_result.stdout)
        self.assertEqual(send_payload["data"]["state"], "waiting_approval")

        wait_result = self.invoke(
            [
                "wait",
                "--worker-id",
                worker_id,
                "--include-history",
                "--timeout-seconds",
                "1",
                "--json",
            ]
        )
        self.assertEqual(wait_result.exit_code, 0)
        wait_payload = json.loads(wait_result.stdout)
        self.assertEqual(wait_payload["type"], "event.matched")
        self.assertEqual(wait_payload["data"]["type"], "approval.requested")

    def test_wait_input_rejects_worker_alias(self) -> None:
        worker_id = self.start_worker()
        self.invoke(
            [
                "send",
                "--worker-id",
                worker_id,
                "--text",
                "needs wait alias",
                "--request-approval",
                "--json",
            ]
        )
        payload_path = self.root / "wait-input-worker-alias.json"
        payload_path.write_text(
            json.dumps(
                {
                    "worker": worker_id,
                    "timeoutSeconds": 1,
                }
            ),
            encoding="utf-8",
        )
        wait_result = self.invoke(["wait", "--input", str(payload_path), "--json"])
        self.assertEqual(wait_result.exit_code, 1)
        wait_payload = json.loads(wait_result.stdout)
        self.assertFalse(wait_payload["ok"])
        self.assertEqual(wait_payload["error"]["code"], "INVALID_INPUT")
        self.assertIn("not supported", wait_payload["error"]["message"])

    def test_wait_after_latest_skips_history_events(self) -> None:
        worker_id = self.start_worker()
        self.invoke(
            [
                "send",
                "--worker-id",
                worker_id,
                "--text",
                "history only",
                "--debug-mode",
                "--json",
            ]
        )
        wait_result = self.invoke(
            [
                "wait",
                "--worker-id",
                worker_id,
                "--after-latest",
                "--until",
                "turn.completed",
                "--timeout-seconds",
                "0.1",
                "--json",
            ]
        )
        self.assertEqual(wait_result.exit_code, 1)
        wait_payload = json.loads(wait_result.stdout)
        self.assertFalse(wait_payload["ok"])
        self.assertEqual(wait_payload["error"]["code"], "WAIT_TIMEOUT")

    def test_wait_supports_turn_end_alias(self) -> None:
        worker_id = self.start_worker()
        self.invoke(
            [
                "send",
                "--worker-id",
                worker_id,
                "--text",
                "Needs approval",
                "--request-approval",
                "--json",
            ]
        )
        wait_result = self.invoke(
            [
                "wait",
                "--worker-id",
                worker_id,
                "--include-history",
                "--until",
                "turn_end",
                "--timeout-seconds",
                "1",
                "--json",
            ]
        )
        self.assertEqual(wait_result.exit_code, 0)
        wait_payload = json.loads(wait_result.stdout)
        self.assertEqual(wait_payload["data"]["type"], "approval.requested")

    def test_wait_rejects_unknown_until_value(self) -> None:
        worker_id = self.start_worker()
        wait_result = self.invoke(
            [
                "wait",
                "--worker-id",
                worker_id,
                "--until",
                "turn_end_typo",
                "--timeout-seconds",
                "0",
                "--json",
            ]
        )
        self.assertEqual(wait_result.exit_code, 1)
        payload = json.loads(wait_result.stdout)
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")

    def test_send_rejects_when_worker_busy(self) -> None:
        worker_id = self.start_worker()
        first_send = self.invoke(
            [
                "send",
                "--worker-id",
                worker_id,
                "--text",
                "Needs approval",
                "--request-approval",
                "--json",
            ]
        )
        self.assertEqual(first_send.exit_code, 0)
        first_payload = json.loads(first_send.stdout)
        self.assertEqual(first_payload["data"]["state"], "waiting_approval")

        second_send = self.invoke(
            ["send", "--worker-id", worker_id, "--text", "second instruction", "--json"]
        )
        self.assertEqual(second_send.exit_code, 1)
        error_payload = json.loads(second_send.stdout)
        self.assertFalse(error_payload["ok"])
        self.assertEqual(error_payload["error"]["code"], "WORKER_BUSY")

    def test_approve_supports_alias_and_option_id(self) -> None:
        worker_id = self.start_worker()
        first_send = self.invoke(
            [
                "send",
                "--worker-id",
                worker_id,
                "--text",
                "approval alias",
                "--request-approval",
                "--json",
            ]
        )
        first_payload = json.loads(first_send.stdout)
        request_id = str(first_payload["data"]["requestId"])
        alias_approve = self.invoke(
            [
                "approve",
                "--worker-id",
                worker_id,
                "--request",
                request_id,
                "--alias",
                "allow",
                "--json",
            ]
        )
        self.assertEqual(alias_approve.exit_code, 0)
        alias_payload = json.loads(alias_approve.stdout)
        self.assertEqual(alias_payload["type"], "approval.decided")
        self.assertEqual(alias_payload["data"]["decision"], "allow")

        second_send = self.invoke(
            [
                "send",
                "--worker-id",
                worker_id,
                "--text",
                "approval option id",
                "--request-approval",
                "--json",
            ]
        )
        second_payload = json.loads(second_send.stdout)
        second_request_id = str(second_payload["data"]["requestId"])
        option_approve = self.invoke(
            [
                "approve",
                "--worker-id",
                worker_id,
                "--request",
                second_request_id,
                "--option-id",
                "deny",
                "--json",
            ]
        )
        self.assertEqual(option_approve.exit_code, 0)
        option_payload = json.loads(option_approve.stdout)
        self.assertEqual(option_payload["data"]["optionId"], "deny")

    def test_approve_supports_decision_synonyms(self) -> None:
        worker_id = self.start_worker()

        allow_send = self.invoke(
            [
                "send",
                "--worker-id",
                worker_id,
                "--text",
                "approval decision approve",
                "--request-approval",
                "--json",
            ]
        )
        self.assertEqual(allow_send.exit_code, 0)
        allow_request_id = str(json.loads(allow_send.stdout)["data"]["requestId"])
        allow_approve = self.invoke(
            [
                "approve",
                "--worker-id",
                worker_id,
                "--request",
                allow_request_id,
                "--decision",
                "approve",
                "--json",
            ]
        )
        self.assertEqual(allow_approve.exit_code, 0)
        allow_payload = json.loads(allow_approve.stdout)
        self.assertEqual(allow_payload["type"], "approval.decided")
        self.assertEqual(allow_payload["data"]["optionId"], "allow")

        deny_send = self.invoke(
            [
                "send",
                "--worker-id",
                worker_id,
                "--text",
                "approval decision reject",
                "--request-approval",
                "--json",
            ]
        )
        self.assertEqual(deny_send.exit_code, 0)
        deny_request_id = str(json.loads(deny_send.stdout)["data"]["requestId"])
        deny_approve = self.invoke(
            [
                "approve",
                "--worker-id",
                worker_id,
                "--request",
                deny_request_id,
                "--decision",
                "reject",
                "--json",
            ]
        )
        self.assertEqual(deny_approve.exit_code, 0)
        deny_payload = json.loads(deny_approve.stdout)
        self.assertEqual(deny_payload["type"], "approval.decided")
        self.assertEqual(deny_payload["data"]["optionId"], "deny")

    def test_approve_input_rejects_worker_alias(self) -> None:
        worker_id = self.start_worker()
        send_result = self.invoke(
            [
                "send",
                "--worker-id",
                worker_id,
                "--text",
                "approval via input alias",
                "--request-approval",
                "--json",
            ]
        )
        self.assertEqual(send_result.exit_code, 0)
        send_payload = json.loads(send_result.stdout)
        request_id = str(send_payload["data"]["requestId"])

        payload_path = self.root / "approve-input-worker-alias.json"
        payload_path.write_text(
            json.dumps(
                {
                    "worker": worker_id,
                    "requestId": request_id,
                    "alias": "allow",
                }
            ),
            encoding="utf-8",
        )

        approve_result = self.invoke(["approve", "--input", str(payload_path), "--json"])
        self.assertEqual(approve_result.exit_code, 1)
        approve_payload = json.loads(approve_result.stdout)
        self.assertFalse(approve_payload["ok"])
        self.assertEqual(approve_payload["error"]["code"], "INVALID_INPUT")
        self.assertIn("not supported", approve_payload["error"]["message"])

    def test_cancel_turn_from_waiting_approval(self) -> None:
        worker_id = self.start_worker()
        send_result = self.invoke(
            [
                "send",
                "--worker-id",
                worker_id,
                "--text",
                "cancel this",
                "--request-approval",
                "--json",
            ]
        )
        self.assertEqual(send_result.exit_code, 0)
        cancel_result = self.invoke(
            ["cancel", "--worker-id", worker_id, "--reason", "no longer needed", "--json"]
        )
        self.assertEqual(cancel_result.exit_code, 0)
        cancel_payload = json.loads(cancel_result.stdout)
        self.assertEqual(cancel_payload["type"], "turn.canceled")
        self.assertEqual(cancel_payload["data"]["state"], "idle")

        wait_result = self.invoke(
            [
                "wait",
                "--worker-id",
                worker_id,
                "--include-history",
                "--until",
                "turn.canceled",
                "--timeout-seconds",
                "1",
                "--json",
            ]
        )
        self.assertEqual(wait_result.exit_code, 0)
        wait_payload = json.loads(wait_result.stdout)
        self.assertEqual(wait_payload["data"]["type"], "turn.canceled")

    def test_cancel_returns_not_running_when_worker_is_idle_after_terminal_event(self) -> None:
        worker_id = self.start_worker()
        send_result = self.invoke(
            [
                "send",
                "--worker-id",
                worker_id,
                "--text",
                "complete quickly",
                "--debug-mode",
                "--json",
            ]
        )
        self.assertEqual(send_result.exit_code, 0)
        send_payload = json.loads(send_result.stdout)
        self.assertEqual(send_payload["data"]["state"], "idle")
        self.assertEqual(send_payload["data"]["matchedEvent"]["type"], "turn.completed")

        cancel_result = self.invoke(
            ["cancel", "--worker-id", worker_id, "--reason", "late cancel", "--json"]
        )
        self.assertEqual(cancel_result.exit_code, 1)
        cancel_payload = json.loads(cancel_result.stdout)
        self.assertFalse(cancel_payload["ok"])
        self.assertEqual(cancel_payload["error"]["code"], "WORKER_NOT_RUNNING")

    def test_wait_timeout_returns_error(self) -> None:
        worker_id = self.start_worker()
        wait_result = self.invoke(
            [
                "wait",
                "--worker-id",
                worker_id,
                "--until",
                "approval.requested",
                "--timeout-seconds",
                "0.1",
                "--json",
            ]
        )
        self.assertEqual(wait_result.exit_code, 1)
        payload = json.loads(wait_result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "WAIT_TIMEOUT")
        details = payload["error"]["details"]
        self.assertEqual(details["workerState"], "idle")
        self.assertIsNone(details["latestEvent"])

    def test_wait_no_progress_timeout_returns_diagnostic_error(self) -> None:
        worker_id = self.start_worker()
        wait_result = self.invoke(
            [
                "wait",
                "--worker-id",
                worker_id,
                "--until",
                "turn.completed",
                "--timeout-seconds",
                "1",
                "--no-progress-timeout-seconds",
                "0.1",
                "--json",
            ]
        )
        self.assertEqual(wait_result.exit_code, 1)
        payload = json.loads(wait_result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "WAIT_NO_PROGRESS")
        details = payload["error"]["details"]
        self.assertEqual(details["workerState"], "idle")
        self.assertEqual(details["noProgressTimeoutSeconds"], 0.1)
        self.assertIsNone(details["latestEvent"])
        self.assertIsNone(details["lastMeaningfulEvent"])
        self.assertIn("diagnosis", details)

    def test_send_wait_no_progress_timeout_does_not_rewrite_dispatch_errors(self) -> None:
        worker_id = self.start_worker()
        timeout_error = SubagentError(
            code="BACKEND_TIMEOUT",
            message="Worker runtime request timed out.",
            details={
                "reasonCategory": "timeout",
                "error": "timed out",
                "recommendedAction": "retry",
            },
        )
        with mock.patch("subagent.turn_service._runtime_request_with_restart", side_effect=timeout_error):
            send_result = self.invoke(
                [
                    "send",
                    "--worker-id",
                    worker_id,
                    "--text",
                    "runtime timeout guard",
                    "--wait-no-progress-timeout-seconds",
                    "0.1",
                    "--json",
                ]
            )
        self.assertEqual(send_result.exit_code, 1)
        payload = json.loads(send_result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "BACKEND_TIMEOUT")
        details = payload["error"]["details"]
        self.assertEqual(details["reasonCategory"], "timeout")
