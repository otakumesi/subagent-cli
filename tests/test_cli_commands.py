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
roleDefaults:
  promptLanguage: en
  responseLanguage: same_as_manager
roleHints:
  worker-default:
    preferredLauncher: codex
    delegationHint: "State goal and done conditions."
    recommendedSkills:
      - skill-creator
defaults:
  launcher: codex
  role: worker-default
"""


class CliCommandTests(unittest.TestCase):
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

        role_result = self.invoke(["role", "show", "worker-default", "--json"])
        self.assertEqual(role_result.exit_code, 0)
        role_payload = json.loads(role_result.stdout)
        self.assertTrue(role_payload["ok"])
        self.assertEqual(role_payload["type"], "role.shown")
        self.assertEqual(role_payload["data"]["name"], "worker-default")
        self.assertEqual(role_payload["data"]["delegationHint"], "State goal and done conditions.")
        self.assertEqual(role_payload["data"]["recommendedSkills"], ["skill-creator"])

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
        self.assertIn("roleDefaults:", generated)
        self.assertIn("roleHints:", generated)
        self.assertIn("defaults:", generated)
        self.assertIn("command: npx", generated)
        self.assertIn("@zed-industries/codex-acp", generated)
        self.assertIn("@zed-industries/claude-agent-acp", generated)
        self.assertIn("@google/gemini-cli", generated)
        self.assertIn("--experimental-acp", generated)
        self.assertIn("command: opencode", generated)
        self.assertIn("- acp", generated)
        self.assertIn("- \"cline\"", generated)
        self.assertIn("- \"--acp\"", generated)
        self.assertIn("@github/copilot-language-server", generated)
        self.assertIn("@kirodotdev/cli", generated)

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

    def test_root_help_mentions_manager_bootstrap_flow(self) -> None:
        result = self.invoke(["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("instructed to", result.stdout)
        self.assertIn("use this tool as a manager agent", result.stdout)
        self.assertIn("subagent prompt render", result.stdout)
        self.assertIn("send` now waits by default", result.stdout)
        self.assertIn("--no-wait", result.stdout)

    def test_worker_start_help_mentions_sandbox_permissions(self) -> None:
        result = self.invoke(["worker", "start", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("outside-sandbox execution", result.stdout)

    def test_send_help_lists_wait_options(self) -> None:
        result = self.invoke(["send", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("--wait", result.stdout)
        self.assertIn("--no-wait", result.stdout)
        self.assertIn("--wait-until", result.stdout)
        self.assertIn("wait-timeout", result.stdout)
        self.assertIn("--text-file", result.stdout)
        self.assertIn("--text-stdin", result.stdout)
        self.assertIn("--input", result.stdout)
        self.assertIn("structured JSON", result.stdout)
        self.assertIn("complex", result.stdout)

    def test_wait_help_lists_alias_and_timeout_behavior(self) -> None:
        result = self.invoke(["wait", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("turn_end", result.stdout)
        self.assertIn("--timeout-seconds", result.stdout)
        self.assertIn("no timeout", result.stdout)
        self.assertIn("--after-latest", result.stdout)
        self.assertIn("--include-history", result.stdout)

    def test_watch_help_lists_streaming_options(self) -> None:
        result = self.invoke(["watch", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("--follow", result.stdout)
        self.assertIn("--from-event-id", result.stdout)
        self.assertIn("--ndjson", result.stdout)

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
                    "roleDefaults": {
                        "promptLanguage": "en",
                        "responseLanguage": "same_as_manager",
                    },
                    "roleHints": {
                        "worker-default": {
                            "preferredLauncher": "codex",
                        }
                    },
                    "defaults": {"launcher": "codex", "role": "worker-default"},
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

    def test_send_accepts_cwd_for_state_resolution_without_state_env(self) -> None:
        env = {
            "SUBAGENT_CONFIG": str(self.config_path),
            "SUBAGENT_STATE_DIR": "",
            "SUBAGENT_CTL_ID": "",
            "SUBAGENT_CTL_EPOCH": "",
            "SUBAGENT_CTL_TOKEN": "",
        }
        outsider = self.root / "outsider"
        outsider.mkdir(parents=True, exist_ok=True)
        (outsider / ".git").mkdir(parents=True, exist_ok=True)
        target_workspace = self.root / "target-workspace"
        target_workspace.mkdir(parents=True, exist_ok=True)

        original_cwd = Path.cwd()
        os.chdir(outsider)
        try:
            init = self.runner.invoke(
                app,
                ["controller", "init", "--cwd", str(target_workspace), "--json"],
                env=env,
                catch_exceptions=False,
            )
            self.assertEqual(init.exit_code, 0)

            started = self.runner.invoke(
                app,
                ["worker", "start", "--cwd", str(target_workspace), "--debug-mode", "--json"],
                env=env,
                catch_exceptions=False,
            )
            self.assertEqual(started.exit_code, 0)
            worker_id = str(json.loads(started.stdout)["data"]["workerId"])

            wrong_scope_send = self.runner.invoke(
                app,
                ["send", "--worker-id", worker_id, "--text", "wrong scope", "--debug-mode", "--json"],
                env=env,
                catch_exceptions=False,
            )
            self.assertEqual(wrong_scope_send.exit_code, 1)
            wrong_scope_payload = json.loads(wrong_scope_send.stdout)
            self.assertEqual(wrong_scope_payload["error"]["code"], "WORKER_NOT_FOUND")

            fixed_scope_send = self.runner.invoke(
                app,
                [
                    "send",
                    "--worker-id",
                    worker_id,
                    "--text",
                    "fixed scope",
                    "--cwd",
                    str(target_workspace),
                    "--debug-mode",
                    "--json",
                ],
                env=env,
                catch_exceptions=False,
            )
            self.assertEqual(fixed_scope_send.exit_code, 0)
            fixed_scope_payload = json.loads(fixed_scope_send.stdout)
            self.assertEqual(fixed_scope_payload["type"], "turn.waited")
            self.assertEqual(fixed_scope_payload["data"]["matchedEvent"]["type"], "turn.completed")
        finally:
            os.chdir(original_cwd)

    def test_worker_commands_accept_cwd_for_state_resolution_without_state_env(self) -> None:
        env = {
            "SUBAGENT_CONFIG": str(self.config_path),
            "SUBAGENT_STATE_DIR": "",
            "SUBAGENT_CTL_ID": "",
            "SUBAGENT_CTL_EPOCH": "",
            "SUBAGENT_CTL_TOKEN": "",
        }
        outsider = self.root / "outsider"
        outsider.mkdir(parents=True, exist_ok=True)
        (outsider / ".git").mkdir(parents=True, exist_ok=True)
        target_workspace = self.root / "target-workspace"
        target_workspace.mkdir(parents=True, exist_ok=True)

        original_cwd = Path.cwd()
        os.chdir(outsider)
        try:
            init = self.runner.invoke(
                app,
                ["controller", "init", "--cwd", str(target_workspace), "--json"],
                env=env,
                catch_exceptions=False,
            )
            self.assertEqual(init.exit_code, 0)

            started = self.runner.invoke(
                app,
                ["worker", "start", "--cwd", str(target_workspace), "--debug-mode", "--json"],
                env=env,
                catch_exceptions=False,
            )
            self.assertEqual(started.exit_code, 0)
            worker_id = str(json.loads(started.stdout)["data"]["workerId"])

            listed_wrong = self.runner.invoke(
                app,
                ["worker", "list", "--json"],
                env=env,
                catch_exceptions=False,
            )
            self.assertEqual(listed_wrong.exit_code, 0)
            listed_wrong_payload = json.loads(listed_wrong.stdout)
            self.assertEqual(listed_wrong_payload["data"]["count"], 0)

            listed_fixed = self.runner.invoke(
                app,
                ["worker", "list", "--cwd", str(target_workspace), "--json"],
                env=env,
                catch_exceptions=False,
            )
            self.assertEqual(listed_fixed.exit_code, 0)
            listed_fixed_payload = json.loads(listed_fixed.stdout)
            self.assertEqual(listed_fixed_payload["data"]["count"], 1)
            self.assertEqual(listed_fixed_payload["data"]["items"][0]["workerId"], worker_id)

            show_wrong = self.runner.invoke(
                app,
                ["worker", "show", worker_id, "--json"],
                env=env,
                catch_exceptions=False,
            )
            self.assertEqual(show_wrong.exit_code, 1)
            show_wrong_payload = json.loads(show_wrong.stdout)
            self.assertEqual(show_wrong_payload["error"]["code"], "WORKER_NOT_FOUND")

            show_fixed = self.runner.invoke(
                app,
                ["worker", "show", worker_id, "--cwd", str(target_workspace), "--json"],
                env=env,
                catch_exceptions=False,
            )
            self.assertEqual(show_fixed.exit_code, 0)

            inspect_fixed = self.runner.invoke(
                app,
                ["worker", "inspect", worker_id, "--cwd", str(target_workspace), "--json"],
                env=env,
                catch_exceptions=False,
            )
            self.assertEqual(inspect_fixed.exit_code, 0)

            handoff_fixed = self.runner.invoke(
                app,
                ["worker", "handoff", "--worker-id", worker_id, "--cwd", str(target_workspace), "--json"],
                env=env,
                catch_exceptions=False,
            )
            self.assertEqual(handoff_fixed.exit_code, 0)
            handoff_payload = json.loads(handoff_fixed.stdout)
            handoff_path = Path(handoff_payload["data"]["handoffPath"])
            self.assertTrue(handoff_path.exists())
            expected_root = (target_workspace / ".subagent" / "state" / "handoffs").resolve()
            self.assertTrue(str(handoff_path.resolve()).startswith(str(expected_root)))

            stop_fixed = self.runner.invoke(
                app,
                ["worker", "stop", worker_id, "--cwd", str(target_workspace), "--json"],
                env=env,
                catch_exceptions=False,
            )
            self.assertEqual(stop_fixed.exit_code, 0)
            stop_payload = json.loads(stop_fixed.stdout)
            self.assertEqual(stop_payload["data"]["state"], "stopped")
        finally:
            os.chdir(original_cwd)

    def test_worker_continue_from_handoff_uses_checkpoint_cwd_when_cwd_omitted(self) -> None:
        env = {
            "SUBAGENT_CONFIG": str(self.config_path),
            "SUBAGENT_STATE_DIR": "",
            "SUBAGENT_CTL_ID": "",
            "SUBAGENT_CTL_EPOCH": "",
            "SUBAGENT_CTL_TOKEN": "",
        }
        outsider = self.root / "outsider"
        outsider.mkdir(parents=True, exist_ok=True)
        (outsider / ".git").mkdir(parents=True, exist_ok=True)
        target_workspace = self.root / "target-workspace"
        target_workspace.mkdir(parents=True, exist_ok=True)

        original_cwd = Path.cwd()
        os.chdir(outsider)
        try:
            init = self.runner.invoke(
                app,
                ["controller", "init", "--cwd", str(target_workspace), "--json"],
                env=env,
                catch_exceptions=False,
            )
            self.assertEqual(init.exit_code, 0)

            started = self.runner.invoke(
                app,
                ["worker", "start", "--cwd", str(target_workspace), "--debug-mode", "--json"],
                env=env,
                catch_exceptions=False,
            )
            self.assertEqual(started.exit_code, 0)
            source_worker_id = str(json.loads(started.stdout)["data"]["workerId"])

            sent = self.runner.invoke(
                app,
                [
                    "send",
                    "--worker-id",
                    source_worker_id,
                    "--text",
                    "prepare handoff",
                    "--cwd",
                    str(target_workspace),
                    "--debug-mode",
                    "--json",
                ],
                env=env,
                catch_exceptions=False,
            )
            self.assertEqual(sent.exit_code, 0)

            handoff = self.runner.invoke(
                app,
                [
                    "worker",
                    "handoff",
                    "--worker-id",
                    source_worker_id,
                    "--cwd",
                    str(target_workspace),
                    "--json",
                ],
                env=env,
                catch_exceptions=False,
            )
            self.assertEqual(handoff.exit_code, 0)
            handoff_payload = json.loads(handoff.stdout)
            handoff_path = Path(handoff_payload["data"]["handoffPath"]).resolve()

            continued = self.runner.invoke(
                app,
                [
                    "worker",
                    "continue",
                    "--from-handoff",
                    str(handoff_path),
                    "--debug-mode",
                    "--json",
                ],
                env=env,
                catch_exceptions=False,
            )
            self.assertEqual(continued.exit_code, 0)
            continued_payload = json.loads(continued.stdout)
            self.assertEqual(continued_payload["type"], "worker.continued")
            self.assertEqual(
                Path(continued_payload["data"]["sourceHandoffPath"]).resolve(),
                handoff_path,
            )
            self.assertEqual(
                Path(continued_payload["data"]["worker"]["cwd"]).resolve(),
                target_workspace.resolve(),
            )
        finally:
            os.chdir(original_cwd)

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
