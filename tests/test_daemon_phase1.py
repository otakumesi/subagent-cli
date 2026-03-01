from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from subagent.daemon import app


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
