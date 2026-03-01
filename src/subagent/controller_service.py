"""Controller ownership orchestration."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .constants import ENV_CTL_EPOCH, ENV_CTL_ID, ENV_CTL_TOKEN
from .errors import SubagentError
from .hints import read_project_hint, write_project_hint
from .paths import resolve_workspace_path
from .state import ControllerHandle, StateStore


def _generate_controller_id() -> str:
    return f"ctl_{uuid.uuid4().hex[:10]}"


@dataclass(slots=True)
class InitializedController:
    controller_id: str
    label: str
    workspace_key: str
    hint_path: str
    owner: ControllerHandle

    def to_dict(self) -> dict[str, Any]:
        return {
            "controllerId": self.controller_id,
            "label": self.label,
            "workspaceKey": self.workspace_key,
            "hintPath": self.hint_path,
            "owner": self.owner.to_dict(),
        }


def resolve_controller_id(
    store: StateStore,
    workspace: Path,
    *,
    explicit_controller_id: str | None = None,
) -> str | None:
    if explicit_controller_id:
        return explicit_controller_id
    hint = read_project_hint(workspace)
    if hint and isinstance(hint.get("controllerId"), str):
        return str(hint["controllerId"])
    row = store.get_controller_by_workspace(str(workspace))
    if row:
        return str(row["controller_id"])
    return None


def init_controller(
    store: StateStore,
    *,
    workspace: Path,
    controller_id: str | None,
    label: str,
    pid: int | None = None,
) -> InitializedController:
    resolved_workspace = resolve_workspace_path(workspace)
    existing_id = resolve_controller_id(
        store,
        resolved_workspace,
        explicit_controller_id=controller_id,
    )
    target_controller_id = existing_id or _generate_controller_id()
    controller_row = store.register_controller(
        controller_id=target_controller_id,
        label=label,
        workspace_key=str(resolved_workspace),
    )
    owner = store.acquire_owner_handle(
        target_controller_id,
        takeover=False,
        pid=pid if pid is not None else os.getpid(),
    )
    hint_path = write_project_hint(
        resolved_workspace,
        controller_id=target_controller_id,
        label=str(controller_row["label"]),
    )
    return InitializedController(
        controller_id=target_controller_id,
        label=str(controller_row["label"]),
        workspace_key=str(resolved_workspace),
        hint_path=str(hint_path),
        owner=owner,
    )


def attach_controller(
    store: StateStore,
    *,
    workspace: Path,
    controller_id: str | None,
    takeover: bool,
    pid: int | None = None,
) -> InitializedController:
    resolved_workspace = resolve_workspace_path(workspace)
    target_controller_id = resolve_controller_id(
        store,
        resolved_workspace,
        explicit_controller_id=controller_id,
    )
    if target_controller_id is None:
        raise SubagentError(
            code="CONTROLLER_NOT_FOUND",
            message=(
                "Controller could not be resolved. Specify --controller-id "
                "or run `subagent controller init` first."
            ),
            details={"workspaceKey": str(resolved_workspace)},
        )
    controller_row = store.get_controller(target_controller_id)
    if controller_row is None:
        raise SubagentError(
            code="CONTROLLER_NOT_FOUND",
            message=f"Controller not found: {target_controller_id}",
            details={"controllerId": target_controller_id},
        )
    owner = store.acquire_owner_handle(
        target_controller_id,
        takeover=takeover,
        pid=pid if pid is not None else os.getpid(),
    )
    hint_path = write_project_hint(
        resolved_workspace,
        controller_id=target_controller_id,
        label=str(controller_row["label"]),
    )
    return InitializedController(
        controller_id=target_controller_id,
        label=str(controller_row["label"]),
        workspace_key=str(resolved_workspace),
        hint_path=str(hint_path),
        owner=owner,
    )


def read_env_handle() -> dict[str, Any] | None:
    controller_id = os.environ.get(ENV_CTL_ID)
    epoch_text = os.environ.get(ENV_CTL_EPOCH)
    token = os.environ.get(ENV_CTL_TOKEN)
    if not controller_id or not epoch_text or not token:
        return None
    try:
        epoch = int(epoch_text)
    except ValueError:
        return {
            "controllerId": controller_id,
            "epochRaw": epoch_text,
            "token": token,
            "valid": False,
            "reason": "ENV_EPOCH_NOT_INTEGER",
        }
    return {
        "controllerId": controller_id,
        "epoch": epoch,
        "token": token,
    }


def shell_env_exports(handle: ControllerHandle) -> list[str]:
    return [
        f"export {ENV_CTL_ID}={handle.controller_id}",
        f"export {ENV_CTL_EPOCH}={handle.epoch}",
        f"export {ENV_CTL_TOKEN}={handle.token}",
    ]


def release_controller(
    store: StateStore,
    *,
    workspace: Path,
    controller_id: str | None,
    force: bool,
) -> dict[str, Any]:
    resolved_workspace = resolve_workspace_path(workspace)
    target_controller_id = resolve_controller_id(
        store,
        resolved_workspace,
        explicit_controller_id=controller_id,
    )
    if target_controller_id is None:
        raise SubagentError(
            code="CONTROLLER_NOT_FOUND",
            message="Controller could not be resolved for release.",
            details={"workspaceKey": str(resolved_workspace)},
        )
    env_handle = read_env_handle()
    epoch: int | None = None
    token: str | None = None
    if env_handle is not None and "epoch" in env_handle and "token" in env_handle:
        if str(env_handle.get("controllerId")) == target_controller_id:
            epoch = int(env_handle["epoch"])
            token = str(env_handle["token"])
    return store.release_owner_handle(
        controller_id=target_controller_id,
        epoch=epoch,
        token=token,
        force=force,
    )


def recover_controllers(store: StateStore, *, workspace: Path | None = None) -> dict[str, Any]:
    workspace_key = str(resolve_workspace_path(workspace)) if workspace is not None else None
    controllers = store.list_controllers()
    active_instances = store.list_active_instances()
    active_by_controller = {str(item["controller_id"]): item for item in active_instances}

    recovered: list[dict[str, Any]] = []
    for controller in controllers:
        controller_id = str(controller["controller_id"])
        if workspace_key and str(controller["workspace_key"]) != workspace_key:
            continue
        active = active_by_controller.get(controller_id)
        owner_alive: bool | None = None
        if active is not None and active.get("pid") is not None:
            pid = int(active["pid"])
            try:
                os.kill(pid, 0)
            except OSError:
                owner_alive = False
            else:
                owner_alive = True
        if active is None:
            state = "dormant"
        elif owner_alive is True:
            state = "active"
        elif owner_alive is False:
            state = "orphaned"
        else:
            state = "conflicted"
        recovered.append(
            {
                "controllerId": controller_id,
                "label": controller["label"],
                "workspaceKey": controller["workspace_key"],
                "state": state,
                "activeOwner": (
                    {
                        "instanceId": active["instance_id"],
                        "epoch": active["epoch"],
                        "pid": active["pid"],
                        "createdAt": active["created_at"],
                        "alive": owner_alive,
                    }
                    if active is not None
                    else None
                ),
                "suggestedAction": (
                    f"subagent controller attach --cwd {controller['workspace_key']} --takeover"
                    if state in {"orphaned", "conflicted"}
                    else None
                ),
            }
        )
    return {
        "count": len(recovered),
        "items": recovered,
        "workspaceFilter": workspace_key,
    }
