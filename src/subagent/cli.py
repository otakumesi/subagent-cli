"""Top-level `subagent` CLI implementation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
import typer

from .config import SubagentConfig, load_config
from .constants import DEFAULT_WAIT_TIMEOUT_SECONDS, DEFAULT_WAIT_UNTIL, PROJECT_HINT_DIRNAME
from .controller_service import (
    attach_controller,
    init_controller,
    read_env_handle,
    recover_controllers,
    release_controller,
    resolve_controller_id,
    shell_env_exports,
)
from .errors import SubagentError
from .handoff_service import continue_worker, create_handoff
from .input_contract import (
    load_input_payload,
    read_blocks,
    read_bool,
    read_string,
    read_string_list,
    reject_duplicates,
)
from .launcher_service import probe_launcher
from .output import emit_error_and_exit, emit_json, ok_envelope
from .paths import resolve_config_path, resolve_state_db_path, resolve_workspace_path
from .prompt_service import render_prompt
from .state import StateStore
from .turn_service import (
    approve_request,
    cancel_turn,
    collect_assistant_messages,
    send_message,
    wait_for_event,
    watch_events,
)
from .worker_service import inspect_worker, list_workers, show_worker, start_worker, stop_worker

app = typer.Typer(
    help=(
        "subagent: protocol-agnostic worker orchestration CLI\n"
        "If you were instructed to use this tool as a manager agent, "
        "start with: `subagent prompt render --target manager`\n"
        "Tip: `subagent send` now waits by default; "
        "use `--no-wait` for fire-and-return behavior. "
        "Use `subagent wait`/`subagent watch` for advanced monitoring. "
        "These commands may require running outside your sandbox "
        "or with elevated permissions, depending on launcher/runtime policy."
    )
)
launcher_app = typer.Typer(help="Manage launcher registry from config")
profile_app = typer.Typer(help="Manage profile registry from config")
pack_app = typer.Typer(help="Manage pack registry from config")
config_app = typer.Typer(help="Manage config files")
prompt_app = typer.Typer(help="Render manager/worker prompts")
controller_app = typer.Typer(help="Manage controller ownership")
worker_app = typer.Typer(help="Manage worker lifecycle")

app.add_typer(launcher_app, name="launcher")
app.add_typer(profile_app, name="profile")
app.add_typer(pack_app, name="pack")
app.add_typer(config_app, name="config")
app.add_typer(prompt_app, name="prompt")
app.add_typer(controller_app, name="controller")
app.add_typer(worker_app, name="worker")


def _load_config_or_exit(config_path: Path | None, *, json_output: bool) -> SubagentConfig:
    try:
        return load_config(config_path)
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)


_DEFAULT_CONFIG_TEMPLATE = """launchers:
  codex:
    backend:
      kind: acp-stdio
    command: npx
    args:
      - -y
      - "@zed-industries/codex-acp"
    env: {}

  claude-code:
    backend:
      kind: acp-stdio
    command: npx
    args:
      - -y
      - "@zed-industries/claude-agent-acp"
    env: {}

  gemini:
    backend:
      kind: acp-stdio
    command: npx
    args:
      - -y
      - "@google/gemini-cli"
      - "--experimental-acp"
    env: {}

  opencode:
    backend:
      kind: acp-stdio
    command: opencode
    args:
      - acp
    env: {}

  cline:
    backend:
      kind: acp-stdio
    command: npx
    args:
      - -y
      - "cline"
      - "--acp"
    env: {}

  github-copilot:
    backend:
      kind: acp-stdio
    command: npx
    args:
      - -y
      - "@github/copilot-language-server"
      - "--acp"
    env: {}

  kiro:
    backend:
      kind: acp-stdio
    command: npx
    args:
      - -y
      - "@kirodotdev/cli"
      - acp
    env: {}

profiles:
  worker-default:
    promptLanguage: en
    responseLanguage: same_as_manager
    autoHandoff: turn_end
    policyPreset: safe-default
    defaultPacks:
      - repo-conventions
    bootstrap: |
      You are a worker subagent.
      Use STATUS:, ASK:, BLOCKED:, and DONE: prefixes when helpful.
      Keep messages concise and actionable.

packs:
  repo-conventions:
    description: Follow repository coding conventions and keep diffs small.
    prompt: |
      Read existing conventions before editing.
      Prefer minimal, explicit changes.

  python-test-fix:
    description: Fix flaky Python tests with minimal change scope.
    prompt: |
      Reproduce failing tests first.
      Add regression coverage where practical.

policyPresets:
  safe-default:
    filesystem: workspace-write
    network: ask
    dangerousCommands: deny

defaults:
  launcher: codex
  profile: worker-default
  packs:
    - repo-conventions
"""


def _default_project_config_path(cwd: Path) -> Path:
    return resolve_workspace_path(cwd) / PROJECT_HINT_DIRNAME / "config.yaml"


@config_app.command("init")
def config_init(
    scope: str = typer.Option(
        "user",
        "--scope",
        help="Config target scope: user or project.",
    ),
    cwd: Path = typer.Option(Path("."), "--cwd", help="Workspace root for project scope"),
    path: Path | None = typer.Option(None, "--path", help="Explicit output path override"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing file"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
) -> None:
    normalized_scope = scope.strip().lower()
    if normalized_scope not in {"user", "project"}:
        emit_error_and_exit(
            SubagentError(
                code="INVALID_ARGUMENT",
                message="`--scope` must be one of: user, project",
                details={"scope": scope},
            ),
            json_output=json_output,
        )

    if path is not None:
        target_path = path.expanduser().resolve()
    elif normalized_scope == "project":
        target_path = _default_project_config_path(cwd)
    else:
        target_path = resolve_config_path(prefer_project=False)

    existed = target_path.exists()
    if existed and not force:
        emit_error_and_exit(
            SubagentError(
                code="CONFIG_ALREADY_EXISTS",
                message=f"Config already exists: {target_path}",
                details={"path": str(target_path)},
            ),
            json_output=json_output,
        )

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(_DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")

    payload = {
        "path": str(target_path),
        "scope": normalized_scope,
        "overwritten": existed,
    }
    if json_output:
        emit_json(ok_envelope("config.initialized", payload))
    else:
        typer.echo(f"configPath: {target_path}")
        typer.echo(f"scope: {normalized_scope}")


def _store(*, json_output: bool, workspace: Path | None = None) -> StateStore:
    try:
        store = StateStore(resolve_state_db_path(workspace=workspace))
        store.bootstrap()
        return store
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)
    raise AssertionError("unreachable")


def _is_param_default(ctx: typer.Context, name: str) -> bool:
    source = ctx.get_parameter_source(name)
    return source == click.core.ParameterSource.DEFAULT


def _require_value(value: Any, *, name: str, json_output: bool) -> Any:
    if value is None:
        emit_error_and_exit(
            SubagentError(
                code="INVALID_INPUT",
                message=f"`{name}` is required",
            ),
            json_output=json_output,
        )
    return value


def _emit_simple_list(
    *,
    title: str,
    items: list[dict[str, Any]],
    json_output: bool,
    event_type: str,
    config: SubagentConfig,
) -> None:
    if json_output:
        emit_json(
            ok_envelope(
                event_type,
                {
                    "items": items,
                    "count": len(items),
                    "configPath": str(config.path),
                    "configLoaded": config.loaded,
                },
            )
        )
        return
    if not items:
        typer.echo(f"(no {title} configured)")
        return
    for item in items:
        typer.echo(item["name"])


def _emit_simple_show(
    *,
    item: dict[str, Any],
    item_type: str,
    json_output: bool,
) -> None:
    if json_output:
        emit_json(ok_envelope(f"{item_type}.shown", item))
        return
    for key, value in item.items():
        typer.echo(f"{key}: {value}")


def _parse_blocks_json_or_exit(
    blocks_json: str | None,
    *,
    json_output: bool,
) -> list[dict[str, Any]] | None:
    if blocks_json is None:
        return None
    try:
        parsed = json.loads(blocks_json)
    except json.JSONDecodeError as error:
        emit_error_and_exit(
            SubagentError(
                code="INVALID_INPUT",
                message="--blocks must be valid JSON",
                details={"error": str(error)},
            ),
            json_output=json_output,
        )
    if not isinstance(parsed, list):
        emit_error_and_exit(
            SubagentError(
                code="INVALID_INPUT",
                message="--blocks JSON must be a list",
            ),
            json_output=json_output,
        )
    blocks: list[dict[str, Any]] = []
    for idx, item in enumerate(parsed):
        if not isinstance(item, dict):
            emit_error_and_exit(
                SubagentError(
                    code="INVALID_INPUT",
                    message=f"--blocks[{idx}] must be an object",
                ),
                json_output=json_output,
            )
        blocks.append(item)
    return blocks


@launcher_app.command("list")
def launcher_list(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
    config_path: Path | None = typer.Option(
        None,
        "--config",
        help="Override config path (default: ~/.config/subagent/config.yaml).",
    ),
) -> None:
    config = _load_config_or_exit(config_path, json_output=json_output)
    items = [
        {
            "name": launcher.name,
            "backendKind": launcher.backend_kind,
            "command": launcher.command,
        }
        for launcher in sorted(config.launchers.values(), key=lambda x: x.name)
    ]
    _emit_simple_list(
        title="launchers",
        items=items,
        json_output=json_output,
        event_type="launcher.listed",
        config=config,
    )


@launcher_app.command("show")
def launcher_show(
    name: str = typer.Argument(..., help="Launcher name"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    config = _load_config_or_exit(config_path, json_output=json_output)
    launcher = config.launchers.get(name)
    if launcher is None:
        emit_error_and_exit(
            SubagentError(
                code="LAUNCHER_NOT_FOUND",
                message=f"Launcher not found: {name}",
                details={"name": name},
            ),
            json_output=json_output,
        )
    _emit_simple_show(item=launcher.to_dict(), item_type="launcher", json_output=json_output)


@launcher_app.command("probe")
def launcher_probe(
    name: str = typer.Argument(..., help="Launcher name"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    config = _load_config_or_exit(config_path, json_output=json_output)
    try:
        payload = probe_launcher(config, name)
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)
    if json_output:
        emit_json(ok_envelope("launcher.probed", payload))
    else:
        status = "available" if payload["available"] else "missing"
        typer.echo(f"{payload['name']}: {status}")
        typer.echo(f"command: {payload['command']}")
        typer.echo(f"effectiveCommand: {payload['effectiveCommand']}")
        typer.echo(f"effectiveArgs: {json.dumps(payload['effectiveArgs'], ensure_ascii=False)}")
        typer.echo(f"resolvedPath: {payload['resolvedCommandPath']}")


@profile_app.command("list")
def profile_list(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    config = _load_config_or_exit(config_path, json_output=json_output)
    items = [
        {
            "name": profile.name,
            "promptLanguage": profile.prompt_language,
            "responseLanguage": profile.response_language,
        }
        for profile in sorted(config.profiles.values(), key=lambda x: x.name)
    ]
    _emit_simple_list(
        title="profiles",
        items=items,
        json_output=json_output,
        event_type="profile.listed",
        config=config,
    )


@profile_app.command("show")
def profile_show(
    name: str = typer.Argument(..., help="Profile name"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    config = _load_config_or_exit(config_path, json_output=json_output)
    profile = config.profiles.get(name)
    if profile is None:
        emit_error_and_exit(
            SubagentError(
                code="PROFILE_NOT_FOUND",
                message=f"Profile not found: {name}",
                details={"name": name},
            ),
            json_output=json_output,
        )
    _emit_simple_show(item=profile.to_dict(), item_type="profile", json_output=json_output)


@pack_app.command("list")
def pack_list(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    config = _load_config_or_exit(config_path, json_output=json_output)
    items = [
        {
            "name": pack.name,
            "description": pack.description,
        }
        for pack in sorted(config.packs.values(), key=lambda x: x.name)
    ]
    _emit_simple_list(
        title="packs",
        items=items,
        json_output=json_output,
        event_type="pack.listed",
        config=config,
    )


@pack_app.command("show")
def pack_show(
    name: str = typer.Argument(..., help="Pack name"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    config = _load_config_or_exit(config_path, json_output=json_output)
    pack = config.packs.get(name)
    if pack is None:
        emit_error_and_exit(
            SubagentError(
                code="PACK_NOT_FOUND",
                message=f"Pack not found: {name}",
                details={"name": name},
            ),
            json_output=json_output,
        )
    _emit_simple_show(item=pack.to_dict(), item_type="pack", json_output=json_output)


@prompt_app.command("render")
def prompt_render(
    target: str = typer.Option(..., "--target", help="Prompt target: manager|worker"),
    profile: str | None = typer.Option(None, "--profile", help="Profile name for worker target"),
    packs: list[str] = typer.Option([], "--pack", help="Pack names (repeatable)"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    config = _load_config_or_exit(config_path, json_output=json_output)
    try:
        payload = render_prompt(
            config,
            target=target,
            profile_name=profile,
            pack_names=packs,
        )
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)
    if json_output:
        emit_json(ok_envelope("prompt.rendered", payload))
    else:
        typer.echo(payload["prompt"])


def _handle_print_env_flag(print_env: bool, json_output: bool) -> None:
    if print_env and json_output:
        emit_error_and_exit(
            SubagentError(
                code="INVALID_ARGUMENT",
                message="--print-env cannot be combined with --json",
            ),
            json_output=True,
        )


@controller_app.command("init")
def controller_init(
    ctx: typer.Context,
    cwd: Path = typer.Option(Path("."), "--cwd", help="Workspace root"),
    controller_id: str | None = typer.Option(None, "--controller-id", help="Controller ID override"),
    label: str = typer.Option("default-manager", "--label", help="Controller label"),
    print_env: bool = typer.Option(
        False,
        "--print-env",
        help="Print shell exports for SUBAGENT_CTL_* variables.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
) -> None:
    _handle_print_env_flag(print_env, json_output)
    workspace_hint = None if _is_param_default(ctx, "cwd") else resolve_workspace_path(cwd)
    store = _store(json_output=json_output, workspace=workspace_hint)
    try:
        initialized = init_controller(
            store,
            workspace=resolve_workspace_path(cwd),
            controller_id=controller_id,
            label=label,
        )
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)

    if print_env:
        for line in shell_env_exports(initialized.owner):
            typer.echo(line)
        return

    payload = initialized.to_dict()
    if json_output:
        emit_json(ok_envelope("controller.initialized", payload))
    else:
        typer.echo(f"controllerId: {payload['controllerId']}")
        typer.echo(f"workspaceKey: {payload['workspaceKey']}")
        typer.echo(f"epoch: {payload['owner']['epoch']}")
        typer.echo(f"hintPath: {payload['hintPath']}")


@controller_app.command("attach")
def controller_attach(
    ctx: typer.Context,
    cwd: Path = typer.Option(Path("."), "--cwd", help="Workspace root"),
    controller_id: str | None = typer.Option(None, "--controller-id", help="Controller ID override"),
    takeover: bool = typer.Option(False, "--takeover", help="Take ownership even if active owner exists"),
    print_env: bool = typer.Option(
        False,
        "--print-env",
        help="Print shell exports for SUBAGENT_CTL_* variables.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
) -> None:
    _handle_print_env_flag(print_env, json_output)
    workspace_hint = None if _is_param_default(ctx, "cwd") else resolve_workspace_path(cwd)
    store = _store(json_output=json_output, workspace=workspace_hint)
    try:
        attached = attach_controller(
            store,
            workspace=resolve_workspace_path(cwd),
            controller_id=controller_id,
            takeover=takeover,
        )
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)

    if print_env:
        for line in shell_env_exports(attached.owner):
            typer.echo(line)
        return

    payload = attached.to_dict()
    if json_output:
        emit_json(ok_envelope("controller.attached", payload))
    else:
        typer.echo(f"controllerId: {payload['controllerId']}")
        typer.echo(f"workspaceKey: {payload['workspaceKey']}")
        typer.echo(f"epoch: {payload['owner']['epoch']}")
        typer.echo(f"hintPath: {payload['hintPath']}")


@controller_app.command("status")
def controller_status(
    ctx: typer.Context,
    cwd: Path = typer.Option(Path("."), "--cwd", help="Workspace root"),
    controller_id: str | None = typer.Option(None, "--controller-id", help="Controller ID override"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
) -> None:
    workspace_hint = None if _is_param_default(ctx, "cwd") else resolve_workspace_path(cwd)
    store = _store(json_output=json_output, workspace=workspace_hint)
    workspace = resolve_workspace_path(cwd)
    resolved_controller_id = resolve_controller_id(
        store,
        workspace,
        explicit_controller_id=controller_id,
    )
    if resolved_controller_id is None:
        payload = {
            "workspaceKey": str(workspace),
            "state": "dormant",
            "controllerId": None,
            "activeOwner": None,
            "envHandle": read_env_handle(),
        }
        if json_output:
            emit_json(ok_envelope("controller.status", payload))
        else:
            typer.echo("state: dormant")
            typer.echo("controllerId: (none)")
        return

    try:
        payload = store.get_controller_status(resolved_controller_id)
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)

    env_handle = read_env_handle()
    if env_handle is None:
        payload["envHandle"] = {"present": False, "valid": False}
    elif env_handle.get("valid") is False:
        payload["envHandle"] = {
            "present": True,
            "valid": False,
            "reason": env_handle.get("reason"),
        }
    else:
        env_controller_id = str(env_handle["controllerId"])
        env_epoch = int(env_handle["epoch"])
        env_token = str(env_handle["token"])
        valid = (
            env_controller_id == resolved_controller_id
            and store.validate_handle(resolved_controller_id, env_epoch, env_token)
        )
        payload["envHandle"] = {
            "present": True,
            "valid": valid,
            "controllerId": env_controller_id,
            "epoch": env_epoch,
        }

    if json_output:
        emit_json(ok_envelope("controller.status", payload))
    else:
        typer.echo(f"state: {payload['state']}")
        typer.echo(f"controllerId: {payload['controllerId']}")
        owner = payload.get("activeOwner")
        if owner is None:
            typer.echo("owner: (none)")
        else:
            typer.echo(f"owner.epoch: {owner['epoch']}")
            typer.echo(f"owner.pid: {owner['pid']}")
        env_payload = payload["envHandle"]
        typer.echo(f"env.valid: {env_payload.get('valid', False)}")


@controller_app.command("recover")
def controller_recover(
    cwd: Path | None = typer.Option(None, "--cwd", help="Optional workspace filter"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
) -> None:
    store = _store(
        json_output=json_output,
        workspace=resolve_workspace_path(cwd) if cwd is not None else None,
    )
    try:
        payload = recover_controllers(store, workspace=cwd)
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)
    if json_output:
        emit_json(ok_envelope("controller.recovered", payload))
    else:
        if payload["count"] == 0:
            typer.echo("(no controllers)")
            return
        for item in payload["items"]:
            typer.echo(f"{item['controllerId']}\t{item['state']}\t{item['workspaceKey']}")


@controller_app.command("release")
def controller_release(
    ctx: typer.Context,
    cwd: Path = typer.Option(Path("."), "--cwd", help="Workspace root"),
    controller_id: str | None = typer.Option(None, "--controller-id", help="Controller ID override"),
    force: bool = typer.Option(False, "--force", help="Release without validating env handle"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
) -> None:
    workspace_hint = None if _is_param_default(ctx, "cwd") else resolve_workspace_path(cwd)
    store = _store(json_output=json_output, workspace=workspace_hint)
    try:
        payload = release_controller(
            store,
            workspace=resolve_workspace_path(cwd),
            controller_id=controller_id,
            force=force,
        )
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)
    if json_output:
        emit_json(ok_envelope("controller.released", payload))
    else:
        typer.echo(f"controllerId: {payload['controllerId']}")
        typer.echo(f"released: {payload['released']}")


@app.command(
    "send",
    help=(
        "Send a turn to a worker. "
        "In sandboxed manager environments, this may need outside-sandbox execution."
    ),
)
def send(
    ctx: typer.Context,
    worker_id: str | None = typer.Option(None, "--worker", help="Worker ID"),
    text: str | None = typer.Option(None, "--text", help="Instruction text"),
    blocks_json: str | None = typer.Option(
        None,
        "--blocks",
        help="Optional blocks payload as JSON list",
    ),
    request_approval: bool = typer.Option(
        False,
        "--request-approval",
        help="Simulate approval-required turn in local runtime",
    ),
    wait_for_match: bool = typer.Option(
        True,
        "--wait/--no-wait",
        help="Wait for a matching event before returning (default: enabled).",
    ),
    wait_until: str = typer.Option(
        DEFAULT_WAIT_UNTIL,
        "--wait-until",
        help="Event type(s) for --wait (comma-separated, alias: turn_end).",
    ),
    wait_timeout_seconds: float = typer.Option(
        DEFAULT_WAIT_TIMEOUT_SECONDS,
        "--wait-timeout-seconds",
        min=0.0,
        help="Timeout for --wait in seconds. Set 0 for no timeout.",
    ),
    input_path: str | None = typer.Option(None, "--input", help="Read command JSON from file path or '-'"),
    debug_mode: bool = typer.Option(
        False,
        "--debug-mode/--no-debug-mode",
        help="Enable local simulation mode for debug/testing.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    try:
        input_payload = load_input_payload(input_path)
        reject_duplicates(
            input_payload,
            flag_values={
                "worker_id": worker_id,
                "text": text,
                "blocks_json": blocks_json,
                "debug_mode": debug_mode,
                "wait_for_match": wait_for_match,
                "wait_until": wait_until,
                "wait_timeout_seconds": wait_timeout_seconds,
            },
            value_is_default={
                "worker_id": _is_param_default(ctx, "worker_id"),
                "text": _is_param_default(ctx, "text"),
                "blocks_json": _is_param_default(ctx, "blocks_json"),
                "debug_mode": _is_param_default(ctx, "debug_mode"),
                "wait_for_match": _is_param_default(ctx, "wait_for_match"),
                "wait_until": _is_param_default(ctx, "wait_until"),
                "wait_timeout_seconds": _is_param_default(ctx, "wait_timeout_seconds"),
            },
            mapping={
                "workerId": "worker_id",
                "text": "text",
                "blocks": "blocks_json",
                "debugMode": "debug_mode",
                "wait": "wait_for_match",
                "waitUntil": "wait_until",
                "waitTimeoutSeconds": "wait_timeout_seconds",
            },
        )
        if input_payload is not None:
            worker_id = read_string(input_payload, "workerId") or worker_id
            text = read_string(input_payload, "text") or text
            blocks = read_blocks(input_payload, "blocks")
            payload_debug_mode = read_bool(input_payload, "debugMode")
            if payload_debug_mode is not None:
                debug_mode = payload_debug_mode
            payload_wait = read_bool(input_payload, "wait")
            if payload_wait is not None:
                wait_for_match = payload_wait
            wait_until = read_string(input_payload, "waitUntil") or wait_until
            wait_timeout_value = input_payload.get("waitTimeoutSeconds")
            if wait_timeout_value is not None:
                if not isinstance(wait_timeout_value, (int, float)):
                    emit_error_and_exit(
                        SubagentError(code="INVALID_INPUT", message="`waitTimeoutSeconds` must be a number"),
                        json_output=json_output,
                    )
                wait_timeout_seconds = float(wait_timeout_value)
        else:
            blocks = None

        if wait_timeout_seconds < 0:
            emit_error_and_exit(
                SubagentError(code="INVALID_INPUT", message="`waitTimeoutSeconds` must be >= 0"),
                json_output=json_output,
            )

        worker_id = _require_value(worker_id, name="worker", json_output=json_output)
        text = _require_value(text, name="text", json_output=json_output)
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)

    store = _store(json_output=json_output)
    config = _load_config_or_exit(config_path, json_output=json_output)
    if blocks is None:
        blocks = _parse_blocks_json_or_exit(blocks_json, json_output=json_output)
    execution_mode = "simulate" if debug_mode else "strict"
    try:
        payload = send_message(
            store,
            worker_id=worker_id,
            text=text,
            blocks=blocks,
            request_approval=request_approval,
            config=config,
            execution_mode=execution_mode,
        )
        if wait_for_match:
            from_event_id = payload.get("acceptedEventId")
            cursor = from_event_id if isinstance(from_event_id, str) and from_event_id else None
            matched_event = wait_for_event(
                store,
                worker_id=worker_id,
                until=wait_until,
                from_event_id=cursor,
                timeout_seconds=wait_timeout_seconds,
            )
            request_id = payload.get("requestId")
            if not isinstance(request_id, str):
                matched_data = matched_event.get("data")
                if isinstance(matched_data, dict):
                    candidate = matched_data.get("requestId")
                    if isinstance(candidate, str):
                        request_id = candidate
            turn_id = payload.get("turnId")
            waited_payload = dict(payload)
            waited_payload["waitUntil"] = wait_until
            waited_payload["waitTimeoutSeconds"] = wait_timeout_seconds
            waited_payload["matchedEvent"] = matched_event
            waited_payload["requestId"] = request_id if isinstance(request_id, str) else None
            assistant_messages = collect_assistant_messages(
                store,
                worker_id=worker_id,
                turn_id=turn_id if isinstance(turn_id, str) else None,
                from_event_id=cursor,
            )
            waited_payload["lastAssistantMessage"] = assistant_messages.get("fullText")
            waited_payload["lastAssistantChunk"] = assistant_messages.get("lastChunk")
            waited_payload["assistantText"] = assistant_messages.get("fullText")
            current_worker = store.get_worker(worker_id)
            if current_worker is not None:
                waited_payload["state"] = str(current_worker["state"])
            if json_output:
                emit_json(ok_envelope("turn.waited", waited_payload))
            else:
                typer.echo(f"workerId: {waited_payload['workerId']}")
                typer.echo(f"turnId: {waited_payload['turnId']}")
                typer.echo(f"state: {waited_payload['state']}")
                typer.echo(f"matchedEvent: {matched_event['type']}")
                if waited_payload["requestId"]:
                    typer.echo(f"requestId: {waited_payload['requestId']}")
            return
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)
    if json_output:
        emit_json(ok_envelope("turn.accepted", payload))
    else:
        typer.echo(f"workerId: {payload['workerId']}")
        typer.echo(f"turnId: {payload['turnId']}")
        typer.echo(f"state: {payload['state']}")


@app.command(
    "watch",
    help=(
        "Watch worker events. "
        "In sandboxed manager environments, this may need outside-sandbox execution."
    ),
)
def watch(
    worker_id: str = typer.Option(..., "--worker", help="Worker ID"),
    from_event_id: str | None = typer.Option(None, "--from-event-id", help="Cursor event ID"),
    follow: bool = typer.Option(False, "--follow", help="Follow events for a short window"),
    timeout_seconds: float = typer.Option(
        1.0,
        "--timeout-seconds",
        min=0.1,
        help="Polling window when --follow is enabled",
    ),
    ndjson: bool = typer.Option(False, "--ndjson", help="Emit one normalized event per line"),
    raw: bool = typer.Option(False, "--raw", help="Include raw payload when available"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
) -> None:
    if ndjson and json_output:
        emit_error_and_exit(
            SubagentError(
                code="INVALID_ARGUMENT",
                message="--ndjson cannot be combined with --json",
            ),
            json_output=True,
        )
    store = _store(json_output=json_output)
    try:
        events = watch_events(
            store,
            worker_id=worker_id,
            from_event_id=from_event_id,
            follow=follow,
            timeout_seconds=timeout_seconds,
            include_raw=raw,
        )
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)

    ndjson_mode = ndjson or not json_output
    if ndjson_mode:
        for event in events:
            emit_json(event)
        return

    if json_output:
        emit_json(
            ok_envelope(
                "events.watched",
                {
                    "workerId": worker_id,
                    "count": len(events),
                    "items": events,
                },
            )
        )
        return


@app.command(
    "wait",
    help=(
        "Wait for a worker event. "
        "In sandboxed manager environments, this may need outside-sandbox execution."
    ),
)
def wait(
    ctx: typer.Context,
    worker_id: str | None = typer.Option(None, "--worker", help="Worker ID"),
    until: str = typer.Option(
        DEFAULT_WAIT_UNTIL,
        "--until",
        help=(
            "Event type(s) to wait for (comma-separated). "
            "Default waits for terminal outcomes and approval requests. "
            "Alias: turn_end. Wildcards: any,*."
        ),
    ),
    from_event_id: str | None = typer.Option(None, "--from-event-id", help="Cursor event ID"),
    timeout_seconds: float = typer.Option(
        DEFAULT_WAIT_TIMEOUT_SECONDS,
        "--timeout-seconds",
        min=0.0,
        help="Timeout in seconds. Set 0 for no timeout.",
    ),
    input_path: str | None = typer.Option(None, "--input", help="Read command JSON from file path or '-'"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
) -> None:
    try:
        input_payload = load_input_payload(input_path)
        reject_duplicates(
            input_payload,
            flag_values={
                "worker_id": worker_id,
                "until": until,
                "from_event_id": from_event_id,
                "timeout_seconds": timeout_seconds,
            },
            value_is_default={
                "worker_id": _is_param_default(ctx, "worker_id"),
                "until": _is_param_default(ctx, "until"),
                "from_event_id": _is_param_default(ctx, "from_event_id"),
                "timeout_seconds": _is_param_default(ctx, "timeout_seconds"),
            },
            mapping={
                "workerId": "worker_id",
                "until": "until",
                "fromEventId": "from_event_id",
                "timeoutSeconds": "timeout_seconds",
            },
        )
        if input_payload is not None:
            worker_id = read_string(input_payload, "workerId") or worker_id
            until = read_string(input_payload, "until") or until
            from_event_id = read_string(input_payload, "fromEventId") or from_event_id
            timeout_value = input_payload.get("timeoutSeconds")
            if timeout_value is not None:
                if not isinstance(timeout_value, (int, float)):
                    emit_error_and_exit(
                        SubagentError(code="INVALID_INPUT", message="`timeoutSeconds` must be a number"),
                        json_output=json_output,
                    )
                timeout_seconds = float(timeout_value)
            if timeout_seconds < 0:
                emit_error_and_exit(
                    SubagentError(code="INVALID_INPUT", message="`timeoutSeconds` must be >= 0"),
                    json_output=json_output,
                )

        worker_id = _require_value(worker_id, name="worker", json_output=json_output)
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)

    store = _store(json_output=json_output)
    try:
        event = wait_for_event(
            store,
            worker_id=worker_id,
            until=until,
            from_event_id=from_event_id,
            timeout_seconds=timeout_seconds,
        )
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)
    if json_output:
        emit_json(ok_envelope("event.matched", event))
    else:
        typer.echo(f"{event['eventId']}\t{event['type']}")


@app.command("approve")
def approve(
    ctx: typer.Context,
    worker_id: str | None = typer.Option(None, "--worker", help="Worker ID"),
    request_id: str | None = typer.Option(None, "--request", help="Approval request ID"),
    decision: str | None = typer.Option(None, "--decision", help="Decision alias"),
    option_id: str | None = typer.Option(None, "--option-id", help="Approval option id"),
    alias: str | None = typer.Option(None, "--alias", help="Approval option alias"),
    note: str | None = typer.Option(None, "--note", help="Decision note"),
    input_path: str | None = typer.Option(None, "--input", help="Read command JSON from file path or '-'"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    try:
        input_payload = load_input_payload(input_path)
        reject_duplicates(
            input_payload,
            flag_values={
                "worker_id": worker_id,
                "request_id": request_id,
                "decision": decision,
                "option_id": option_id,
                "alias": alias,
                "note": note,
            },
            value_is_default={
                "worker_id": _is_param_default(ctx, "worker_id"),
                "request_id": _is_param_default(ctx, "request_id"),
                "decision": _is_param_default(ctx, "decision"),
                "option_id": _is_param_default(ctx, "option_id"),
                "alias": _is_param_default(ctx, "alias"),
                "note": _is_param_default(ctx, "note"),
            },
            mapping={
                "workerId": "worker_id",
                "requestId": "request_id",
                "decision": "decision",
                "optionId": "option_id",
                "alias": "alias",
                "note": "note",
            },
        )
        if input_payload is not None:
            worker_id = read_string(input_payload, "workerId") or worker_id
            request_id = read_string(input_payload, "requestId") or request_id
            decision = read_string(input_payload, "decision") or decision
            option_id = read_string(input_payload, "optionId") or option_id
            alias = read_string(input_payload, "alias") or alias
            note = read_string(input_payload, "note") or note

        worker_id = _require_value(worker_id, name="worker", json_output=json_output)
        request_id = _require_value(request_id, name="request", json_output=json_output)
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)

    store = _store(json_output=json_output)
    config = _load_config_or_exit(config_path, json_output=json_output)
    try:
        payload = approve_request(
            store,
            worker_id=worker_id,
            request_id=request_id,
            decision=decision,
            option_id=option_id,
            alias=alias,
            note=note,
            config=config,
        )
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)
    if json_output:
        emit_json(ok_envelope("approval.decided", payload))
    else:
        typer.echo(f"requestId: {payload['requestId']}")
        typer.echo(f"decision: {payload['decision']}")
        typer.echo(f"state: {payload['state']}")


@app.command("cancel")
def cancel(
    worker_id: str = typer.Option(..., "--worker", help="Worker ID"),
    reason: str | None = typer.Option(None, "--reason", help="Cancel reason"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    store = _store(json_output=json_output)
    config = _load_config_or_exit(config_path, json_output=json_output)
    try:
        payload = cancel_turn(
            store,
            worker_id=worker_id,
            reason=reason,
            config=config,
        )
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)
    if json_output:
        emit_json(ok_envelope("turn.canceled", payload))
    else:
        typer.echo(f"workerId: {payload['workerId']}")
        typer.echo(f"state: {payload['state']}")


@worker_app.command(
    "start",
    help=(
        "Start a worker runtime. "
        "Tip: run `subagent launcher probe <launcher-name> --json` first and ensure "
        "launcher-required permissions (including network access) are available. "
        "In sandboxed manager environments, this may need outside-sandbox execution."
    ),
)
def worker_start(
    ctx: typer.Context,
    launcher: str | None = typer.Option(None, "--launcher", help="Launcher name"),
    profile: str | None = typer.Option(None, "--profile", help="Profile name"),
    packs: list[str] = typer.Option([], "--pack", help="Pack names (repeatable)"),
    cwd: Path = typer.Option(Path("."), "--cwd", help="Worker working directory"),
    label: str | None = typer.Option(None, "--label", help="Worker label"),
    controller_id: str | None = typer.Option(None, "--controller-id", help="Controller ID override"),
    debug_mode: bool = typer.Option(
        False,
        "--debug-mode/--no-debug-mode",
        help="Start worker without backend runtime (for debug/testing).",
    ),
    input_path: str | None = typer.Option(None, "--input", help="Read command JSON from file path or '-'"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    try:
        input_payload = load_input_payload(input_path)
        reject_duplicates(
            input_payload,
            flag_values={
                "launcher": launcher,
                "profile": profile,
                "packs": packs,
                "cwd": str(cwd),
                "label": label,
                "controller_id": controller_id,
                "debug_mode": debug_mode,
            },
            value_is_default={
                "launcher": _is_param_default(ctx, "launcher"),
                "profile": _is_param_default(ctx, "profile"),
                "packs": _is_param_default(ctx, "packs"),
                "cwd": _is_param_default(ctx, "cwd"),
                "label": _is_param_default(ctx, "label"),
                "controller_id": _is_param_default(ctx, "controller_id"),
                "debug_mode": _is_param_default(ctx, "debug_mode"),
            },
            mapping={
                "launcher": "launcher",
                "profile": "profile",
                "packs": "packs",
                "cwd": "cwd",
                "label": "label",
                "controllerId": "controller_id",
                "debugMode": "debug_mode",
            },
        )
        if input_payload is not None:
            launcher = read_string(input_payload, "launcher") or launcher
            profile = read_string(input_payload, "profile") or profile
            payload_packs = read_string_list(input_payload, "packs")
            if payload_packs is not None:
                packs = payload_packs
            payload_cwd = read_string(input_payload, "cwd")
            if payload_cwd is not None:
                cwd = Path(payload_cwd)
            label = read_string(input_payload, "label") or label
            controller_id = read_string(input_payload, "controllerId") or controller_id
            payload_debug_mode = read_bool(input_payload, "debugMode")
            if payload_debug_mode is not None:
                debug_mode = payload_debug_mode
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)

    workspace_hint = None if _is_param_default(ctx, "cwd") else resolve_workspace_path(cwd)
    store = _store(json_output=json_output, workspace=workspace_hint)
    config = _load_config_or_exit(config_path, json_output=json_output)
    try:
        payload = start_worker(
            store,
            config,
            workspace=resolve_workspace_path(cwd),
            worker_cwd=resolve_workspace_path(cwd),
            controller_id=controller_id,
            launcher=launcher,
            profile=profile,
            packs=packs,
            label=label,
            debug_mode=debug_mode,
        )
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)
    if json_output:
        emit_json(ok_envelope("worker.started", payload))
    else:
        typer.echo(f"workerId: {payload['workerId']}")
        typer.echo(f"controllerId: {payload['controllerId']}")
        typer.echo(f"state: {payload['state']}")


@worker_app.command("list")
def worker_list(
    controller_id: str | None = typer.Option(None, "--controller-id", help="Filter by controller ID"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
) -> None:
    store = _store(json_output=json_output)
    try:
        items = list_workers(store, controller_id=controller_id)
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)
    payload = {
        "items": items,
        "count": len(items),
        "controllerId": controller_id,
    }
    if json_output:
        emit_json(ok_envelope("worker.listed", payload))
    else:
        if not items:
            typer.echo("(no workers)")
            return
        for item in items:
            typer.echo(f"{item['workerId']}\t{item['state']}\t{item['label']}")


@worker_app.command("show")
def worker_show(
    worker_id: str = typer.Argument(..., help="Worker ID"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
) -> None:
    store = _store(json_output=json_output)
    try:
        payload = show_worker(store, worker_id)
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)
    if json_output:
        emit_json(ok_envelope("worker.shown", payload))
    else:
        for key, value in payload.items():
            typer.echo(f"{key}: {value}")


@worker_app.command("inspect")
def worker_inspect(
    worker_id: str = typer.Argument(..., help="Worker ID"),
    events_limit: int = typer.Option(20, "--events-limit", min=1, max=200, help="Number of recent events"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
) -> None:
    store = _store(json_output=json_output)
    try:
        payload = inspect_worker(store, worker_id, events_limit=events_limit)
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)
    if json_output:
        emit_json(ok_envelope("worker.inspected", payload))
    else:
        worker = payload["worker"]
        typer.echo(f"workerId: {worker['workerId']}")
        typer.echo(f"state: {worker['state']}")
        typer.echo(f"recoveryState: {worker['recoveryState']}")
        typer.echo(f"pendingApprovals: {len(payload['pendingApprovals'])}")
        typer.echo(f"events: {len(payload['events'])}")


@worker_app.command("stop")
def worker_stop(
    worker_id: str = typer.Argument(..., help="Worker ID"),
    force: bool = typer.Option(False, "--force", help="Force transition to stopped"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
) -> None:
    store = _store(json_output=json_output)
    try:
        payload = stop_worker(store, worker_id, force=force)
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)
    if json_output:
        emit_json(ok_envelope("worker.stopped", payload))
    else:
        typer.echo(f"workerId: {payload['workerId']}")
        typer.echo(f"state: {payload['state']}")


@worker_app.command("handoff")
def worker_handoff(
    worker_id: str = typer.Option(..., "--worker", help="Worker ID"),
    handoffs_dir: Path | None = typer.Option(
        None,
        "--handoffs-dir",
        help="Override handoff storage directory",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
) -> None:
    store = _store(json_output=json_output)
    try:
        payload = create_handoff(
            store,
            worker_id=worker_id,
            handoffs_dir=handoffs_dir,
        )
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)
    if json_output:
        emit_json(ok_envelope("worker.handoff.ready", payload))
    else:
        typer.echo(f"workerId: {payload['workerId']}")
        typer.echo(f"handoffPath: {payload['handoffPath']}")
        typer.echo(f"checkpointPath: {payload['checkpointPath']}")


@worker_app.command("continue")
def worker_continue(
    from_worker: str | None = typer.Option(None, "--from-worker", help="Source worker ID"),
    from_handoff: Path | None = typer.Option(None, "--from-handoff", help="Path to handoff.md"),
    launcher: str | None = typer.Option(None, "--launcher", help="Launcher override"),
    profile: str | None = typer.Option(None, "--profile", help="Profile override"),
    packs: list[str] = typer.Option([], "--pack", help="Pack override (repeatable)"),
    cwd: Path | None = typer.Option(None, "--cwd", help="Target working directory"),
    label: str | None = typer.Option(None, "--label", help="Target worker label"),
    controller_id: str | None = typer.Option(None, "--controller-id", help="Controller override"),
    handoffs_dir: Path | None = typer.Option(
        None,
        "--handoffs-dir",
        help="Override handoff storage directory",
    ),
    debug_mode: bool = typer.Option(
        False,
        "--debug-mode/--no-debug-mode",
        help="Enable debug mode for worker startup and bootstrap turn.",
    ),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
) -> None:
    store = _store(
        json_output=json_output,
        workspace=resolve_workspace_path(cwd) if cwd is not None else None,
    )
    config = _load_config_or_exit(config_path, json_output=json_output)
    execution_mode = "simulate" if debug_mode else "strict"
    try:
        payload = continue_worker(
            store,
            config,
            from_worker=from_worker,
            from_handoff=from_handoff,
            launcher=launcher,
            profile=profile,
            packs=packs,
            cwd=cwd,
            label=label,
            controller_id=controller_id,
            handoffs_dir=handoffs_dir,
            debug_mode=debug_mode,
            execution_mode=execution_mode,
        )
    except SubagentError as error:
        emit_error_and_exit(error, json_output=json_output)
    if json_output:
        emit_json(ok_envelope("worker.continued", payload))
    else:
        typer.echo(f"sourceHandoffPath: {payload['sourceHandoffPath']}")
        typer.echo(f"workerId: {payload['worker']['workerId']}")
        typer.echo(f"state: {payload['worker']['state']}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
