from __future__ import annotations

import json
import os
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

    def test_config_init_creates_file_and_requires_force_to_overwrite(self) -> None:
        config_out = self.root / "generated" / "config.yaml"

        first = self.invoke(
            ["config", "init", "--path", str(config_out), "--json"],
        )
        self.assertEqual(first.exit_code, 0)
        first_payload = json.loads(first.stdout)
        self.assertEqual(first_payload["type"], "config.initialized")
        self.assertEqual(Path(first_payload["data"]["path"]).resolve(), config_out.resolve())
        self.assertEqual(first_payload["data"]["scope"], "user")
        self.assertFalse(first_payload["data"]["overwritten"])
        self.assertTrue(config_out.exists())
        generated = config_out.read_text(encoding="utf-8")
        self.assertIn("launchers:", generated)
        self.assertIn("profiles:", generated)
        self.assertIn("defaults:", generated)
        self.assertIn("command: npx", generated)
        self.assertIn("@zed-industries/codex-acp", generated)
        self.assertIn("@zed-industries/claude-agent-acp", generated)

        second = self.invoke(
            ["config", "init", "--path", str(config_out), "--json"],
        )
        self.assertEqual(second.exit_code, 1)
        second_payload = json.loads(second.stdout)
        self.assertEqual(second_payload["error"]["code"], "CONFIG_ALREADY_EXISTS")

        third = self.invoke(
            ["config", "init", "--path", str(config_out), "--force", "--json"],
        )
        self.assertEqual(third.exit_code, 0)
        third_payload = json.loads(third.stdout)
        self.assertTrue(third_payload["data"]["overwritten"])

    def test_config_init_project_scope_writes_under_workspace(self) -> None:
        result = self.invoke(
            ["config", "init", "--scope", "project", "--cwd", str(self.workspace), "--json"],
        )
        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["type"], "config.initialized")
        expected = self.workspace / ".subagent" / "config.yaml"
        self.assertEqual(Path(payload["data"]["path"]).resolve(), expected.resolve())
        self.assertEqual(payload["data"]["scope"], "project")
        self.assertTrue(expected.exists())

    def test_root_help_mentions_manager_prompt_bootstrap(self) -> None:
        result = self.invoke(["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("instructed to", result.stdout)
        self.assertIn("use this tool as a manager agent", result.stdout)
        self.assertIn("subagent prompt render --target", result.stdout)
        self.assertIn("send --wait", result.stdout)
        self.assertIn("outside your sandbox", result.stdout)
        self.assertIn("or with elevated", result.stdout)
        self.assertIn("launcher/runtime policy", result.stdout)

    def test_worker_start_help_mentions_sandbox_permissions(self) -> None:
        result = self.invoke(["worker", "start", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("outside-sandbox execution", result.stdout)

    def test_send_wait_watch_help_mentions_sandbox_permissions(self) -> None:
        for args in (["send", "--help"], ["wait", "--help"], ["watch", "--help"]):
            result = self.invoke(args)
            self.assertEqual(result.exit_code, 0)
            self.assertIn("outside-sandbox execution", result.stdout)

    def test_project_config_is_auto_discovered_from_workspace(self) -> None:
        project_config = self.workspace / ".subagent" / "config.yaml"
        project_config.parent.mkdir(parents=True, exist_ok=True)
        project_config.write_text(
            SAMPLE_CONFIG.replace("codex-acp", "project-codex-acp").strip() + "\n",
            encoding="utf-8",
        )

        original_cwd = Path.cwd()
        os.chdir(self.workspace)
        try:
            result = self.invoke(["launcher", "show", "codex", "--json"], env={"SUBAGENT_CONFIG": ""})
        finally:
            os.chdir(original_cwd)

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["type"], "launcher.shown")
        self.assertEqual(payload["data"]["command"], "project-codex-acp")

    def test_launcher_probe_supports_inline_command_tokens(self) -> None:
        inline_config = self.root / "inline-config.json"
        inline_config.write_text(
            json.dumps(
                {
                    "launchers": {
                        "codex": {
                            "backend": {"kind": "acp-stdio"},
                            "command": "sh -c",
                            "args": ["echo probe-ok"],
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
                    "defaults": {"launcher": "codex", "profile": "worker-default"},
                }
            ),
            encoding="utf-8",
        )
        result = self.invoke(
            ["launcher", "probe", "codex", "--json"],
            env={"SUBAGENT_CONFIG": str(inline_config)},
        )
        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["data"]["available"])
        self.assertEqual(payload["data"]["effectiveCommand"], "sh")
        self.assertEqual(payload["data"]["effectiveArgs"], ["-c", "echo probe-ok"])
        self.assertTrue(payload["data"]["commandWasTokenized"])

    def test_worker_list_errors_when_workspace_root_cannot_be_resolved(self) -> None:
        env = {
            "SUBAGENT_CONFIG": str(self.config_path),
            "SUBAGENT_STATE_DIR": "",
            "SUBAGENT_CTL_ID": "",
            "SUBAGENT_CTL_EPOCH": "",
            "SUBAGENT_CTL_TOKEN": "",
        }
        original_cwd = Path.cwd()
        os.chdir(self.root)
        try:
            result = self.runner.invoke(app, ["worker", "list", "--json"], env=env, catch_exceptions=False)
        finally:
            os.chdir(original_cwd)
        self.assertEqual(result.exit_code, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["code"], "WORKSPACE_ROOT_NOT_FOUND")

    def test_controller_init_uses_cwd_for_state_resolution_without_state_env(self) -> None:
        env = {
            "SUBAGENT_CONFIG": str(self.config_path),
            "SUBAGENT_STATE_DIR": "",
            "SUBAGENT_CTL_ID": "",
            "SUBAGENT_CTL_EPOCH": "",
            "SUBAGENT_CTL_TOKEN": "",
        }
        outsider = self.root / "outsider"
        outsider.mkdir(parents=True, exist_ok=True)
        target_workspace = self.root / "target-workspace"
        target_workspace.mkdir(parents=True, exist_ok=True)

        original_cwd = Path.cwd()
        os.chdir(outsider)
        try:
            result = self.runner.invoke(
                app,
                ["controller", "init", "--cwd", str(target_workspace), "--json"],
                env=env,
                catch_exceptions=False,
            )
        finally:
            os.chdir(original_cwd)

        self.assertEqual(result.exit_code, 0)
        state_db = target_workspace / ".subagent" / "state" / "state.db"
        self.assertTrue(state_db.exists())

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
