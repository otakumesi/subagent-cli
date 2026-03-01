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


class CLIPhase1Tests(unittest.TestCase):
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

    def test_registry_commands_with_json_output(self) -> None:
        launcher_result = self.invoke(["launcher", "list", "--json"])
        self.assertEqual(launcher_result.exit_code, 0)
        launcher_payload = json.loads(launcher_result.stdout)
        self.assertTrue(launcher_payload["ok"])
        self.assertEqual(launcher_payload["type"], "launcher.listed")
        self.assertEqual(launcher_payload["data"]["count"], 1)

        profile_result = self.invoke(["profile", "show", "worker-default", "--json"])
        self.assertEqual(profile_result.exit_code, 0)
        profile_payload = json.loads(profile_result.stdout)
        self.assertTrue(profile_payload["ok"])
        self.assertEqual(profile_payload["type"], "profile.shown")
        self.assertEqual(profile_payload["data"]["name"], "worker-default")

        pack_result = self.invoke(["pack", "list", "--json"])
        self.assertEqual(pack_result.exit_code, 0)
        pack_payload = json.loads(pack_result.stdout)
        self.assertEqual(pack_payload["type"], "pack.listed")
        self.assertEqual(pack_payload["data"]["count"], 1)

    def test_root_help_mentions_manager_prompt_bootstrap(self) -> None:
        result = self.invoke(["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("prompt render --target manager", result.stdout)

    def test_controller_init_and_status_with_valid_env_handle(self) -> None:
        init_result = self.invoke(
            ["controller", "init", "--cwd", str(self.workspace), "--json"]
        )
        self.assertEqual(init_result.exit_code, 0)
        init_payload = json.loads(init_result.stdout)
        self.assertTrue(init_payload["ok"])
        self.assertEqual(init_payload["type"], "controller.initialized")
        owner = init_payload["data"]["owner"]

        status_env = {
            "SUBAGENT_CTL_ID": owner["controllerId"],
            "SUBAGENT_CTL_EPOCH": str(owner["epoch"]),
            "SUBAGENT_CTL_TOKEN": owner["token"],
        }
        status_result = self.invoke(
            ["controller", "status", "--cwd", str(self.workspace), "--json"],
            env=status_env,
        )
        self.assertEqual(status_result.exit_code, 0)
        status_payload = json.loads(status_result.stdout)
        self.assertEqual(status_payload["type"], "controller.status")
        self.assertEqual(status_payload["data"]["state"], "active")
        self.assertTrue(status_payload["data"]["envHandle"]["valid"])
