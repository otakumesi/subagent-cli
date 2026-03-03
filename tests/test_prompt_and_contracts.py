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
    command: sh
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


class PromptAndContractTests(unittest.TestCase):
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

    def start_worker(self) -> str:
        self.init_controller()
        result = self.invoke(
            ["worker", "start", "--cwd", str(self.workspace), "--debug-mode", "--json"]
        )
        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.stdout)
        return str(payload["data"]["workerId"])

    def test_prompt_render_manager_and_worker(self) -> None:
        manager = self.invoke(["prompt", "render", "--target", "manager", "--json"])
        self.assertEqual(manager.exit_code, 0)
        manager_payload = json.loads(manager.stdout)
        self.assertEqual(manager_payload["type"], "prompt.rendered")
        self.assertEqual(manager_payload["data"]["target"], "manager")
        manager_prompt = str(manager_payload["data"]["prompt"])
        self.assertIn("Read this quick workflow first", manager_prompt)
        self.assertIn("subagent controller init", manager_prompt)
        self.assertIn("subagent send --worker", manager_prompt)
        self.assertIn("--no-wait", manager_prompt)
        self.assertIn("--wait-no-progress-timeout-seconds", manager_prompt)
        self.assertIn("--text-file", manager_prompt)
        self.assertIn("--text-stdin", manager_prompt)

        worker = self.invoke(["prompt", "render", "--target", "worker", "--json"])
        self.assertEqual(worker.exit_code, 0)
        worker_payload = json.loads(worker.stdout)
        self.assertEqual(worker_payload["data"]["target"], "worker")
        self.assertIn("repo-conventions", worker_payload["data"]["packs"])

    def test_launcher_probe_and_worker_inspect(self) -> None:
        probe = self.invoke(["launcher", "probe", "codex", "--json"])
        self.assertEqual(probe.exit_code, 0)
        probe_payload = json.loads(probe.stdout)
        self.assertEqual(probe_payload["type"], "launcher.probed")
        self.assertIn("available", probe_payload["data"])

        worker_id = self.start_worker()
        self.invoke(
            [
                "send",
                "--worker",
                worker_id,
                "--text",
                "needs approval",
                "--request-approval",
                "--json",
            ]
        )
        inspect_result = self.invoke(["worker", "inspect", worker_id, "--json"])
        self.assertEqual(inspect_result.exit_code, 0)
        inspect_payload = json.loads(inspect_result.stdout)
        self.assertEqual(inspect_payload["type"], "worker.inspected")
        self.assertGreaterEqual(len(inspect_payload["data"]["pendingApprovals"]), 1)
        self.assertGreaterEqual(len(inspect_payload["data"]["events"]), 1)

    def test_worker_inspect_supports_filters_and_tail(self) -> None:
        worker_id = self.start_worker()
        first_send = self.invoke(
            [
                "send",
                "--worker",
                worker_id,
                "--text",
                "first filtered turn",
                "--debug-mode",
                "--no-wait",
                "--json",
            ]
        )
        self.assertEqual(first_send.exit_code, 0)
        first_payload = json.loads(first_send.stdout)
        first_turn_id = str(first_payload["data"]["turnId"])

        second_send = self.invoke(
            [
                "send",
                "--worker",
                worker_id,
                "--text",
                "second turn",
                "--request-approval",
                "--json",
            ]
        )
        self.assertEqual(second_send.exit_code, 0)

        filtered = self.invoke(
            [
                "worker",
                "inspect",
                worker_id,
                "--turn-id",
                first_turn_id,
                "--event-type",
                "turn.completed",
                "--tail",
                "5",
                "--json",
            ]
        )
        self.assertEqual(filtered.exit_code, 0)
        filtered_payload = json.loads(filtered.stdout)
        events = filtered_payload["data"]["events"]
        self.assertGreaterEqual(len(events), 1)
        for event in events:
            self.assertEqual(event["turnId"], first_turn_id)
            self.assertEqual(event["type"], "turn.completed")

        since_filtered = self.invoke(
            [
                "worker",
                "inspect",
                worker_id,
                "--since",
                "9999-01-01T00:00:00+00:00",
                "--json",
            ]
        )
        self.assertEqual(since_filtered.exit_code, 0)
        since_payload = json.loads(since_filtered.stdout)
        self.assertEqual(len(since_payload["data"]["events"]), 0)

    def test_controller_recover_and_release(self) -> None:
        init = self.init_controller()
        owner = init["data"]["owner"]
        recover = self.invoke(["controller", "recover", "--cwd", str(self.workspace), "--json"])
        self.assertEqual(recover.exit_code, 0)
        recover_payload = json.loads(recover.stdout)
        self.assertEqual(recover_payload["type"], "controller.recovered")
        self.assertGreaterEqual(recover_payload["data"]["count"], 1)

        release_env = {
            "SUBAGENT_CTL_ID": owner["controllerId"],
            "SUBAGENT_CTL_EPOCH": str(owner["epoch"]),
            "SUBAGENT_CTL_TOKEN": owner["token"],
        }
        release = self.invoke(
            ["controller", "release", "--cwd", str(self.workspace), "--json"],
            env=release_env,
        )
        self.assertEqual(release.exit_code, 0)
        release_payload = json.loads(release.stdout)
        self.assertEqual(release_payload["type"], "controller.released")
        self.assertTrue(release_payload["data"]["released"])

    def test_input_contract_rejects_duplicates_and_supports_input_only(self) -> None:
        worker_id = self.start_worker()
        send_input = self.root / "send.json"
        send_input.write_text(
            json.dumps(
                {
                    "workerId": worker_id,
                    "text": "from input payload",
                }
            ),
            encoding="utf-8",
        )
        duplicate = self.invoke(
            [
                "send",
                "--worker",
                worker_id,
                "--text",
                "duplicate",
                "--input",
                str(send_input),
                "--json",
            ]
        )
        self.assertEqual(duplicate.exit_code, 1)
        duplicate_payload = json.loads(duplicate.stdout)
        self.assertEqual(duplicate_payload["error"]["code"], "INVALID_INPUT")

        worker_input = self.root / "start.json"
        worker_input.write_text(
            json.dumps(
                {
                    "launcher": "codex",
                    "profile": "worker-default",
                    "cwd": str(self.workspace),
                    "label": "from-input",
                    "debugMode": True,
                }
            ),
            encoding="utf-8",
        )
        start_result = self.invoke(["worker", "start", "--input", str(worker_input), "--json"])
        self.assertEqual(start_result.exit_code, 0)
        start_payload = json.loads(start_result.stdout)
        self.assertEqual(start_payload["data"]["label"], "from-input")
