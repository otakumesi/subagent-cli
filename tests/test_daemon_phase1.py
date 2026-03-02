from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from subagent.daemon import app
from subagent.state import StateStore


class DaemonPhase1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state_dir = self.root / "state"
        self.runner = CliRunner()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def invoke(self, args: list[str]):
        env = {"SUBAGENT_STATE_DIR": str(self.state_dir)}
        return self.runner.invoke(
            app,
            args,
            env=env,
            catch_exceptions=False,
        )

    def test_run_once_and_status(self) -> None:
        run_result = self.invoke(["run", "--once", "--json"])
        self.assertEqual(run_result.exit_code, 0)
        run_payload = json.loads(run_result.stdout)
        self.assertEqual(run_payload["schemaVersion"], "v1")
        self.assertEqual(run_payload["mode"], "once")

        status_result = self.invoke(["status", "--json"])
        self.assertEqual(status_result.exit_code, 0)
        status_payload = json.loads(status_result.stdout)
        self.assertIn("running", status_payload)
        self.assertIn("dbPath", status_payload)
        self.assertIn("workerHealth", status_payload)
        self.assertIn("summary", status_payload["workerHealth"])

    def test_run_once_reports_unhealthy_worker_and_restart_failure(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        config_path = self.root / "config.yaml"
        config_path.write_text(
            (
                "launchers:\n"
                "  codex:\n"
                "    backend:\n"
                "      kind: acp-stdio\n"
                "    command: nonexistent-codex-acp\n"
                "    args: []\n"
                "    env: {}\n"
                "profiles:\n"
                "  worker-default:\n"
                "    promptLanguage: en\n"
                "    responseLanguage: same_as_manager\n"
                "    defaultPacks: []\n"
                "    bootstrap: |\n"
                "      You are a worker subagent.\n"
                "packs: {}\n"
                "defaults:\n"
                "  launcher: codex\n"
                "  profile: worker-default\n"
            ),
            encoding="utf-8",
        )

        store = StateStore(self.state_dir / "state.db")
        store.bootstrap()
        controller_id = "ctl_test"
        store.register_controller(controller_id, "test", str(workspace))
        worker = store.create_worker(
            controller_id=controller_id,
            launcher="codex",
            profile="worker-default",
            packs=[],
            cwd=str(workspace),
            label="w",
        )
        worker_id = str(worker["worker_id"])
        store.set_worker_runtime_endpoint(
            worker_id,
            runtime_pid=999_999,
            runtime_socket=str(self.root / "missing.sock"),
        )

        env = {
            "SUBAGENT_STATE_DIR": str(self.state_dir),
            "SUBAGENT_CONFIG": str(config_path),
        }
        run_result = self.runner.invoke(
            app,
            ["run", "--once", "--json"],
            env=env,
            catch_exceptions=False,
        )
        self.assertEqual(run_result.exit_code, 0)
        payload = json.loads(run_result.stdout)
        summary = payload["workerHealth"]["summary"]
        self.assertEqual(summary["checked"], 1)
        self.assertEqual(summary["unhealthy"], 1)
        self.assertEqual(summary["restartFailed"], 1)
