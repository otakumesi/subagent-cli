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


class WorkerPhase2Tests(unittest.TestCase):
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

    def init_controller(self) -> dict[str, object]:
        result = self.invoke(["controller", "init", "--cwd", str(self.workspace), "--json"])
        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        return payload

    def test_worker_start_uses_config_defaults(self) -> None:
        self.init_controller()
        start_result = self.invoke(
            ["worker", "start", "--cwd", str(self.workspace), "--debug-mode", "--json"]
        )
        self.assertEqual(start_result.exit_code, 0)
        payload = json.loads(start_result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["type"], "worker.started")
        data = payload["data"]
        self.assertEqual(data["launcher"], "codex")
        self.assertEqual(data["profile"], "worker-default")
        self.assertEqual(data["packs"], ["repo-conventions"])
        self.assertEqual(data["state"], "idle")

    def test_worker_list_show_and_stop_flow(self) -> None:
        self.init_controller()
        start_result = self.invoke(
            [
                "worker",
                "start",
                "--cwd",
                str(self.workspace),
                "--label",
                "payments-fix",
                "--debug-mode",
                "--json",
            ]
        )
        self.assertEqual(start_result.exit_code, 0)
        start_payload = json.loads(start_result.stdout)
        worker_id = start_payload["data"]["workerId"]

        list_result = self.invoke(["worker", "list", "--json"])
        self.assertEqual(list_result.exit_code, 0)
        list_payload = json.loads(list_result.stdout)
        self.assertEqual(list_payload["type"], "worker.listed")
        self.assertEqual(list_payload["data"]["count"], 1)
        self.assertEqual(list_payload["data"]["items"][0]["workerId"], worker_id)

        show_result = self.invoke(["worker", "show", worker_id, "--json"])
        self.assertEqual(show_result.exit_code, 0)
        show_payload = json.loads(show_result.stdout)
        self.assertEqual(show_payload["type"], "worker.shown")
        self.assertEqual(show_payload["data"]["state"], "idle")

        stop_result = self.invoke(["worker", "stop", worker_id, "--json"])
        self.assertEqual(stop_result.exit_code, 0)
        stop_payload = json.loads(stop_result.stdout)
        self.assertEqual(stop_payload["type"], "worker.stopped")
        self.assertEqual(stop_payload["data"]["state"], "stopped")

        show_after_stop = self.invoke(["worker", "show", worker_id, "--json"])
        self.assertEqual(show_after_stop.exit_code, 0)
        show_after_stop_payload = json.loads(show_after_stop.stdout)
        self.assertEqual(show_after_stop_payload["data"]["state"], "stopped")

    def test_worker_start_fails_on_unknown_launcher(self) -> None:
        self.init_controller()
        result = self.invoke(
            [
                "worker",
                "start",
                "--cwd",
                str(self.workspace),
                "--launcher",
                "unknown",
                "--json",
            ]
        )
        self.assertEqual(result.exit_code, 1)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "LAUNCHER_NOT_FOUND")

    def test_worker_stop_fails_for_unknown_worker(self) -> None:
        self.init_controller()
        result = self.invoke(["worker", "stop", "w_missing", "--json"])
        self.assertEqual(result.exit_code, 1)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "WORKER_NOT_FOUND")
