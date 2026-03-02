from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from subagent import paths
from subagent.errors import SubagentError


class PathsPhase6Tests(unittest.TestCase):
    def test_resolve_state_dir_uses_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            env_state_dir = Path(tempdir) / "env-state"
            with mock.patch.dict(os.environ, {paths.ENV_STATE_DIR: str(env_state_dir)}, clear=False):
                with mock.patch.object(paths, "resolve_workspace_root_path") as root_resolver:
                    resolved = paths.resolve_state_dir()

        self.assertEqual(resolved, env_state_dir.resolve())
        root_resolver.assert_not_called()

    def test_resolve_workspace_root_path_discovers_parent_git_root(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir) / "workspace"
            nested = workspace / "a" / "b"
            (workspace / ".git").mkdir(parents=True, exist_ok=True)
            nested.mkdir(parents=True, exist_ok=True)
            original_cwd = Path.cwd()
            os.chdir(nested)
            try:
                resolved = paths.resolve_workspace_root_path()
            finally:
                os.chdir(original_cwd)

        self.assertEqual(resolved, workspace.resolve())

    def test_resolve_state_dir_uses_explicit_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir) / "explicit-workspace"
            workspace.mkdir(parents=True, exist_ok=True)
            with mock.patch.dict(os.environ, {paths.ENV_STATE_DIR: ""}, clear=False):
                with mock.patch.object(paths, "resolve_workspace_root_path") as root_resolver:
                    resolved = paths.resolve_state_dir(workspace=workspace)

        self.assertEqual(resolved, (workspace / ".subagent" / "state").resolve())
        root_resolver.assert_not_called()

    def test_resolve_state_dir_defaults_to_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir) / "workspace"
            nested = workspace / "src"
            (workspace / ".git").mkdir(parents=True, exist_ok=True)
            nested.mkdir(parents=True, exist_ok=True)

            with mock.patch.dict(os.environ, {paths.ENV_STATE_DIR: ""}, clear=False):
                original_cwd = Path.cwd()
                os.chdir(nested)
                try:
                    resolved = paths.resolve_state_dir()
                finally:
                    os.chdir(original_cwd)

        self.assertEqual(resolved, (workspace / ".subagent" / "state").resolve())

    def test_resolve_state_dir_errors_when_workspace_root_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            unknown = Path(tempdir) / "unknown"
            unknown.mkdir(parents=True, exist_ok=True)
            with mock.patch.dict(os.environ, {paths.ENV_STATE_DIR: ""}, clear=False):
                original_cwd = Path.cwd()
                os.chdir(unknown)
                try:
                    with self.assertRaises(SubagentError) as raised:
                        paths.resolve_state_dir()
                finally:
                    os.chdir(original_cwd)

        self.assertEqual(raised.exception.code, "WORKSPACE_ROOT_NOT_FOUND")
