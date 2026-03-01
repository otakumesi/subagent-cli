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


class HandoffPhase4Tests(unittest.TestCase):
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

    def test_worker_handoff_generates_files(self) -> None:
        worker_id = self.start_worker()
        send_result = self.invoke(
            [
                "send",
                "--worker",
                worker_id,
                "--text",
                "Investigate flaky retry test",
                "--debug-mode",
                "--json",
            ]
        )
        self.assertEqual(send_result.exit_code, 0)

        handoff_result = self.invoke(["worker", "handoff", "--worker", worker_id, "--json"])
        self.assertEqual(handoff_result.exit_code, 0)
        handoff_payload = json.loads(handoff_result.stdout)
        self.assertEqual(handoff_payload["type"], "worker.handoff.ready")
        data = handoff_payload["data"]
        handoff_path = Path(data["handoffPath"])
        checkpoint_path = Path(data["checkpointPath"])
        self.assertTrue(handoff_path.exists())
        self.assertTrue(checkpoint_path.exists())

        handoff_text = handoff_path.read_text(encoding="utf-8")
        required_sections = [
            "## Task",
            "## Goal",
            "## Current Status",
            "## Completed",
            "## Pending",
            "## Files of Interest",
            "## Commands Run",
            "## Risks / Notes",
            "## Recommended Next Step",
            "## Artifacts",
        ]
        for section in required_sections:
            self.assertIn(section, handoff_text)

        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["schemaVersion"], "v1")
        self.assertEqual(checkpoint["workerId"], worker_id)
        self.assertEqual(checkpoint["state"], "handoff_ready")

    def test_worker_continue_from_worker_starts_new_worker(self) -> None:
        source_worker = self.start_worker()
        self.invoke(
            [
                "send",
                "--worker",
                source_worker,
                "--text",
                "Prepare handoff context",
                "--debug-mode",
                "--json",
            ]
        )

        continue_result = self.invoke(
            [
                "worker",
                "continue",
                "--from-worker",
                source_worker,
                "--debug-mode",
                "--json",
            ]
        )
        self.assertEqual(continue_result.exit_code, 0)
        payload = json.loads(continue_result.stdout)
        self.assertEqual(payload["type"], "worker.continued")
        new_worker = payload["data"]["worker"]["workerId"]
        self.assertNotEqual(new_worker, source_worker)

        watch_result = self.invoke(["watch", "--worker", new_worker, "--ndjson"])
        self.assertEqual(watch_result.exit_code, 0)
        lines = [line for line in watch_result.stdout.splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 2)
        event_types = [json.loads(line)["type"] for line in lines]
        self.assertIn("message.sent", event_types)
        self.assertIn("turn.completed", event_types)

    def test_worker_continue_from_handoff_path(self) -> None:
        source_worker = self.start_worker()
        self.invoke(
            [
                "send",
                "--worker",
                source_worker,
                "--text",
                "Need follow-up work",
                "--debug-mode",
                "--json",
            ]
        )
        handoff_result = self.invoke(["worker", "handoff", "--worker", source_worker, "--json"])
        handoff_payload = json.loads(handoff_result.stdout)
        handoff_path = handoff_payload["data"]["handoffPath"]

        continue_result = self.invoke(
            [
                "worker",
                "continue",
                "--from-handoff",
                handoff_path,
                "--debug-mode",
                "--json",
            ]
        )
        self.assertEqual(continue_result.exit_code, 0)
        payload = json.loads(continue_result.stdout)
        self.assertEqual(payload["type"], "worker.continued")
        self.assertEqual(payload["data"]["sourceHandoffPath"], handoff_path)

    def test_worker_continue_requires_single_source(self) -> None:
        source_worker = self.start_worker()
        handoff_result = self.invoke(["worker", "handoff", "--worker", source_worker, "--json"])
        handoff_payload = json.loads(handoff_result.stdout)
        handoff_path = handoff_payload["data"]["handoffPath"]

        result = self.invoke(
            [
                "worker",
                "continue",
                "--from-worker",
                source_worker,
                "--from-handoff",
                handoff_path,
                "--json",
            ]
        )
        self.assertEqual(result.exit_code, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")
