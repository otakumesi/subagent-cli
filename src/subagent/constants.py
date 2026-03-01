"""Shared constants used across the subagent CLI."""

from pathlib import Path

APP_NAME = "subagent"
SCHEMA_VERSION = "v1"

ENV_CONFIG_PATH = "SUBAGENT_CONFIG"
ENV_STATE_DIR = "SUBAGENT_STATE_DIR"
ENV_CTL_ID = "SUBAGENT_CTL_ID"
ENV_CTL_EPOCH = "SUBAGENT_CTL_EPOCH"
ENV_CTL_TOKEN = "SUBAGENT_CTL_TOKEN"

DEFAULT_CONFIG_PATH = Path.home() / ".config" / APP_NAME / "config.yaml"
DEFAULT_STATE_DIR = Path.home() / ".local" / "share" / APP_NAME
DEFAULT_STATE_DB_PATH = DEFAULT_STATE_DIR / "state.db"
DAEMON_STATUS_PATH = DEFAULT_STATE_DIR / "subagentd-status.json"
DEFAULT_HANDOFFS_DIR = DEFAULT_STATE_DIR / "handoffs"

PROJECT_HINT_DIRNAME = ".subagent"
PROJECT_HINT_FILENAME = "controller.json"
