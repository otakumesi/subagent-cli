from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from typer.testing import CliRunner

from subagent.cli import app
from subagent.state import StateStore
from subagent.turn_service import send_message


class AcpBackendIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.root / "state"
        self.config_path = self.root / "config.json"
        fake_agent = (Path(__file__).resolve().parent / "fixtures" / "fake_acp_agent.py").resolve()
        config = {
            "launchers": {
                "fake-acp": {
                    "backend": {"kind": "acp-stdio"},
                    "command": sys.executable,
                    "args": [str(fake_agent)],
                    "env": {},
                }
            },
            "roleDefaults": {
                "promptLanguage": "en",
                "responseLanguage": "same_as_manager",
            },
            "roleHints": {"worker-default": {"preferredLauncher": "fake-acp"}},
            "defaults": {"launcher": "fake-acp", "role": "worker-default"},
        }
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
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

    def invoke(self, args: list[str]):
        return self.runner.invoke(
            app,
            args,
            env=self.base_env,
            catch_exceptions=False,
        )

    def start_worker(self) -> str:
        init = self.invoke(["controller", "init", "--cwd", str(self.workspace), "--json"])
        self.assertEqual(init.exit_code, 0)
        started = self.invoke(["worker", "start", "--cwd", str(self.workspace), "--json"])
        self.assertEqual(started.exit_code, 0)
        payload = json.loads(started.stdout)
        return str(payload["data"]["workerId"])

    def _watch_events(self, worker_id: str) -> list[dict[str, object]]:
        watched = self.invoke(["watch", "--worker-id", worker_id, "--raw", "--ndjson"])
        self.assertEqual(watched.exit_code, 0)
        lines = [line for line in watched.stdout.splitlines() if line.strip()]
        return [json.loads(line) for line in lines]

    def test_send_uses_acp_backend_and_updates_session_id(self) -> None:
        worker_id = self.start_worker()
        sent = self.invoke(["send", "--worker-id", worker_id, "--text", "Investigate flaky test", "--json"])
        self.assertEqual(sent.exit_code, 0)
        send_payload = json.loads(sent.stdout)
        self.assertEqual(send_payload["type"], "turn.waited")
        self.assertEqual(send_payload["data"]["state"], "idle")
        self.assertEqual(send_payload["data"]["matchedEvent"]["type"], "turn.completed")
        assistant_text = str(send_payload["data"]["assistantText"])
        self.assertIn("STATUS: fake backend started.", assistant_text)
        self.assertIn("DONE: fake backend complete.", assistant_text)
        self.assertEqual(send_payload["data"]["lastAssistantMessage"], assistant_text)
        self.assertEqual(send_payload["data"]["lastAssistantChunk"], "DONE: fake backend complete.")

        shown = self.invoke(["worker", "show", worker_id, "--json"])
        self.assertEqual(shown.exit_code, 0)
        worker_payload = json.loads(shown.stdout)
        self.assertTrue(str(worker_payload["data"]["sessionId"]).startswith("sess_fake_"))

        events = self._watch_events(worker_id)
        progress_messages = [
            str(event["data"].get("text"))
            for event in events
            if event.get("type") == "progress.message" and isinstance(event.get("data"), dict)
        ]
        self.assertTrue(any("fake backend started" in message for message in progress_messages))

        completed = [event for event in events if event.get("type") == "turn.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["data"].get("stopReason"), "end_turn")
        self.assertEqual(completed[0]["raw"].get("runtime"), "acp-stdio")

    def test_send_reuses_existing_session_via_session_load(self) -> None:
        worker_id = self.start_worker()
        first = self.invoke(["send", "--worker-id", worker_id, "--text", "first turn", "--json"])
        self.assertEqual(first.exit_code, 0)
        shown = self.invoke(["worker", "show", worker_id, "--json"])
        first_session_id = str(json.loads(shown.stdout)["data"]["sessionId"])

        second = self.invoke(["send", "--worker-id", worker_id, "--text", "second turn", "--json"])
        self.assertEqual(second.exit_code, 0)
        shown_after = self.invoke(["worker", "show", worker_id, "--json"])
        second_session_id = str(json.loads(shown_after.stdout)["data"]["sessionId"])
        self.assertEqual(first_session_id, second_session_id)

        events = self._watch_events(worker_id)
        completed = [event for event in events if event.get("type") == "turn.completed"]
        self.assertEqual(len(completed), 2)
        for item in completed:
            raw = item.get("raw")
            self.assertIsInstance(raw, dict)
            assert isinstance(raw, dict)
            self.assertEqual(raw.get("runtime"), "acp-stdio")

    def test_runtime_restart_resumes_previous_session_id(self) -> None:
        worker_id = self.start_worker()
        first = self.invoke(["send", "--worker-id", worker_id, "--text", "first turn", "--json"])
        self.assertEqual(first.exit_code, 0)

        shown = self.invoke(["worker", "show", worker_id, "--json"])
        self.assertEqual(shown.exit_code, 0)
        shown_payload = json.loads(shown.stdout)["data"]
        first_session_id = str(shown_payload["sessionId"])
        first_runtime_pid = int(shown_payload["runtimePid"])

        os.kill(first_runtime_pid, signal.SIGKILL)
        time.sleep(0.3)

        second = self.invoke(["send", "--worker-id", worker_id, "--text", "second turn", "--json"])
        self.assertEqual(second.exit_code, 0)

        shown_after = self.invoke(["worker", "show", worker_id, "--json"])
        self.assertEqual(shown_after.exit_code, 0)
        shown_after_payload = json.loads(shown_after.stdout)["data"]
        second_session_id = str(shown_after_payload["sessionId"])
        second_runtime_pid = int(shown_after_payload["runtimePid"])

        self.assertEqual(first_session_id, second_session_id)
        self.assertNotEqual(first_runtime_pid, second_runtime_pid)

        events = self._watch_events(worker_id)
        load_updates = [
            event
            for event in events
            if event.get("type") == "progress.update"
            and isinstance(event.get("data"), dict)
            and event["data"].get("method") == "session.load"
        ]
        self.assertGreaterEqual(len(load_updates), 1)

    def test_permission_flow_survives_runtime_restart_without_orphan_pending(self) -> None:
        worker_id = self.start_worker()
        first = self.invoke(["send", "--worker-id", worker_id, "--text", "first turn", "--json"])
        self.assertEqual(first.exit_code, 0)

        shown = self.invoke(["worker", "show", worker_id, "--json"])
        self.assertEqual(shown.exit_code, 0)
        runtime_pid = int(json.loads(shown.stdout)["data"]["runtimePid"])
        os.kill(runtime_pid, signal.SIGKILL)
        time.sleep(0.3)

        sent = self.invoke(["send", "--worker-id", worker_id, "--text", "needs permission", "--json"])
        self.assertEqual(sent.exit_code, 0)
        sent_payload = json.loads(sent.stdout)
        self.assertEqual(sent_payload["type"], "turn.waited")
        self.assertEqual(sent_payload["data"]["state"], "waiting_approval")
        self.assertEqual(sent_payload["data"]["matchedEvent"]["type"], "approval.requested")
        request_id = str(sent_payload["data"]["requestId"])

        inspected_pending = self.invoke(["worker", "inspect", worker_id, "--json"])
        self.assertEqual(inspected_pending.exit_code, 0)
        pending_payload = json.loads(inspected_pending.stdout)
        self.assertEqual(len(pending_payload["data"]["pendingApprovals"]), 1)
        self.assertEqual(pending_payload["data"]["pendingApprovals"][0]["requestId"], request_id)

        approved = self.invoke(
            [
                "approve",
                "--worker-id",
                worker_id,
                "--request",
                request_id,
                "--option-id",
                "allow",
                "--json",
            ]
        )
        self.assertEqual(approved.exit_code, 0)
        approve_payload = json.loads(approved.stdout)
        self.assertEqual(approve_payload["type"], "approval.decided")

        inspected = self.invoke(["worker", "inspect", worker_id, "--json"])
        self.assertEqual(inspected.exit_code, 0)
        inspect_payload = json.loads(inspected.stdout)
        self.assertEqual(len(inspect_payload["data"]["pendingApprovals"]), 0)

    def test_permission_request_is_resolved_via_approve_command(self) -> None:
        worker_id = self.start_worker()
        sent = self.invoke(["send", "--worker-id", worker_id, "--text", "needs permission", "--json"])
        self.assertEqual(sent.exit_code, 0)
        sent_payload = json.loads(sent.stdout)
        self.assertEqual(sent_payload["data"]["state"], "waiting_approval")
        request_id = str(sent_payload["data"]["requestId"])

        inspected_pending = self.invoke(["worker", "inspect", worker_id, "--json"])
        self.assertEqual(inspected_pending.exit_code, 0)
        pending_payload = json.loads(inspected_pending.stdout)
        self.assertEqual(len(pending_payload["data"]["pendingApprovals"]), 1)

        approved = self.invoke(
            [
                "approve",
                "--worker-id",
                worker_id,
                "--request",
                request_id,
                "--option-id",
                "deny",
                "--json",
            ]
        )
        self.assertEqual(approved.exit_code, 0)
        approve_payload = json.loads(approved.stdout)
        self.assertEqual(approve_payload["type"], "approval.decided")
        self.assertEqual(approve_payload["data"]["optionId"], "deny")

        events = self._watch_events(worker_id)
        event_types = [str(event.get("type")) for event in events]
        self.assertIn("approval.requested", event_types)
        self.assertIn("approval.decided", event_types)

        decided_events = [event for event in events if event.get("type") == "approval.decided"]
        self.assertEqual(len(decided_events), 1)
        decided_data = decided_events[0]["data"]
        self.assertEqual(decided_data.get("optionId"), "deny")

        inspected = self.invoke(["worker", "inspect", worker_id, "--json"])
        self.assertEqual(inspected.exit_code, 0)
        inspect_payload = json.loads(inspected.stdout)
        self.assertEqual(len(inspect_payload["data"]["pendingApprovals"]), 0)

    def test_cancel_and_stop_are_propagated_to_runtime(self) -> None:
        worker_id = self.start_worker()
        store = StateStore(self.state_dir / "state.db")
        store.bootstrap()
        send_result_holder: dict[str, object] = {}

        def run_send() -> None:
            try:
                send_result_holder["result"] = send_message(
                    store,
                    worker_id=worker_id,
                    text="cancelable turn",
                    execution_mode="strict",
                )
            except Exception as error:  # pragma: no cover - defensive in integration flow
                send_result_holder["error"] = error

        send_thread = threading.Thread(target=run_send, daemon=True)
        send_thread.start()
        time.sleep(0.5)

        cancel = self.invoke(
            ["cancel", "--worker-id", worker_id, "--reason", "no longer needed", "--json"]
        )
        self.assertEqual(cancel.exit_code, 0)
        cancel_payload = json.loads(cancel.stdout)
        self.assertEqual(cancel_payload["type"], "turn.canceled")
        self.assertEqual(cancel_payload["data"]["state"], "idle")

        send_thread.join(timeout=5.0)
        self.assertFalse(send_thread.is_alive())
        send_error = send_result_holder.get("error")
        self.assertIsNone(send_error)
        send_result = send_result_holder.get("result")
        self.assertIsNotNone(send_result)
        assert isinstance(send_result, dict)
        self.assertEqual(send_result.get("state"), "idle")

        events = self._watch_events(worker_id)
        event_types = [str(event.get("type")) for event in events]
        self.assertIn("turn.canceled", event_types)

        stopped = self.invoke(["worker", "stop", worker_id, "--json"])
        self.assertEqual(stopped.exit_code, 0)
        stop_payload = json.loads(stopped.stdout)
        self.assertEqual(stop_payload["data"]["state"], "stopped")

        shown = self.invoke(["worker", "show", worker_id, "--json"])
        self.assertEqual(shown.exit_code, 0)
        worker_payload = json.loads(shown.stdout)
        self.assertIsNone(worker_payload["data"]["runtimeSocket"])

    def test_cancel_recovers_when_runtime_already_finished_but_state_is_stale_running(self) -> None:
        worker_id = self.start_worker()
        sent = self.invoke(["send", "--worker-id", worker_id, "--text", "quick completion", "--json"])
        self.assertEqual(sent.exit_code, 0)
        sent_payload = json.loads(sent.stdout)
        self.assertEqual(sent_payload["type"], "turn.waited")
        self.assertEqual(sent_payload["data"]["matchedEvent"]["type"], "turn.completed")
        completed_turn_id = str(sent_payload["data"]["turnId"])

        store = StateStore(self.state_dir / "state.db")
        store.bootstrap()
        store.set_worker_active_turn(worker_id, completed_turn_id)
        store.update_worker_state(worker_id, next_state="running")

        cancel = self.invoke(["cancel", "--worker-id", worker_id, "--reason", "late cancel", "--json"])
        self.assertEqual(cancel.exit_code, 0)
        cancel_payload = json.loads(cancel.stdout)
        self.assertEqual(cancel_payload["type"], "turn.canceled")
        self.assertEqual(cancel_payload["data"]["state"], "idle")
        self.assertTrue(cancel_payload["data"]["alreadyTerminal"])
        self.assertEqual(cancel_payload["data"]["terminalEventType"], "turn.completed")

        shown = self.invoke(["worker", "show", worker_id, "--json"])
        self.assertEqual(shown.exit_code, 0)
        shown_payload = json.loads(shown.stdout)
        self.assertEqual(shown_payload["data"]["state"], "idle")

    def test_strict_mode_errors_when_backend_is_unavailable(self) -> None:
        broken_config_path = self.root / "broken-config.json"
        broken = {
            "launchers": {
                "fake-acp": {
                    "backend": {"kind": "acp-stdio"},
                    "command": "nonexistent-acp-command-for-test",
                    "args": [],
                    "env": {},
                }
            },
            "roleDefaults": {
                "promptLanguage": "en",
                "responseLanguage": "same_as_manager",
            },
            "roleHints": {"worker-default": {"preferredLauncher": "fake-acp"}},
            "defaults": {"launcher": "fake-acp", "role": "worker-default"},
        }
        broken_config_path.write_text(json.dumps(broken), encoding="utf-8")
        env = dict(self.base_env)
        env["SUBAGENT_CONFIG"] = str(broken_config_path)

        runner = CliRunner()
        init = runner.invoke(
            app,
            ["controller", "init", "--cwd", str(self.workspace), "--json"],
            env=env,
            catch_exceptions=False,
        )
        self.assertEqual(init.exit_code, 0)
        started = runner.invoke(
            app,
            ["worker", "start", "--cwd", str(self.workspace), "--json"],
            env=env,
            catch_exceptions=False,
        )
        self.assertEqual(started.exit_code, 1)
        payload = json.loads(started.stdout)
        self.assertEqual(payload["error"]["code"], "BACKEND_LAUNCHER")
