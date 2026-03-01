from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from subagent.cli import app

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


class TurnPhase3Tests(unittest.TestCase):
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

    def invoke(self, args: list[str], *, env: dict[str, str] | None = None):
        merged_env = dict(self.base_env)
        if env:
            merged_env.update(env)
        return self.runner.invoke(
            app,
            args,
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
        worker_id = self.start_worker()
        send_result = self.invoke(
            ["send", "--worker", worker_id, "--text", "strict by default", "--json"]
        )
        self.assertEqual(send_result.exit_code, 1)
        payload = json.loads(send_result.stdout)
        self.assertEqual(payload["error"]["code"], "BACKEND_UNAVAILABLE")

    def test_send_wait_and_watch_ndjson(self) -> None:
        worker_id = self.start_worker()
        send_result = self.invoke(
            [
                "send",
                "--worker",
                worker_id,
                "--text",
                "Investigate flaky test",
                "--debug-mode",
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
                "--worker",
                worker_id,
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

        watch_result = self.invoke(["watch", "--worker", worker_id, "--ndjson"])
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

    def test_send_rejects_when_worker_busy(self) -> None:
        worker_id = self.start_worker()
        first_send = self.invoke(
            [
                "send",
                "--worker",
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
            ["send", "--worker", worker_id, "--text", "second instruction", "--json"]
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
                "--worker",
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
                "--worker",
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
                "--worker",
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
                "--worker",
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

    def test_cancel_turn_from_waiting_approval(self) -> None:
        worker_id = self.start_worker()
        send_result = self.invoke(
            [
                "send",
                "--worker",
                worker_id,
                "--text",
                "cancel this",
                "--request-approval",
                "--json",
            ]
        )
        self.assertEqual(send_result.exit_code, 0)
        cancel_result = self.invoke(
            ["cancel", "--worker", worker_id, "--reason", "no longer needed", "--json"]
        )
        self.assertEqual(cancel_result.exit_code, 0)
        cancel_payload = json.loads(cancel_result.stdout)
        self.assertEqual(cancel_payload["type"], "turn.canceled")
        self.assertEqual(cancel_payload["data"]["state"], "idle")

        wait_result = self.invoke(
            [
                "wait",
                "--worker",
                worker_id,
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

    def test_wait_timeout_returns_error(self) -> None:
        worker_id = self.start_worker()
        wait_result = self.invoke(
            [
                "wait",
                "--worker",
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
