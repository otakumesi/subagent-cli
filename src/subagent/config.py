"""Config model and loader for launchers / profiles / packs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .errors import SubagentError
from .paths import resolve_config_path
from .simple_yaml import ParseError as SimpleYamlParseError
from .simple_yaml import parse_yaml_subset


def _ensure_mapping(value: Any, *, field_name: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    raise SubagentError(
        code="CONFIG_PARSE_ERROR",
        message=f"`{field_name}` must be a mapping",
        details={"field": field_name},
    )


def _ensure_string_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SubagentError(
            code="CONFIG_PARSE_ERROR",
            message=f"`{field_name}` must be a list",
            details={"field": field_name},
        )
    string_values: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str):
            raise SubagentError(
                code="CONFIG_PARSE_ERROR",
                message=f"`{field_name}[{idx}]` must be a string",
                details={"field": field_name, "index": idx},
            )
        string_values.append(item)
    return string_values


def _ensure_string_map(value: Any, *, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    mapping = _ensure_mapping(value, field_name=field_name)
    string_map: dict[str, str] = {}
    for key, item in mapping.items():
        if not isinstance(key, str):
            raise SubagentError(
                code="CONFIG_PARSE_ERROR",
                message=f"`{field_name}` key must be string",
                details={"field": field_name},
            )
        if not isinstance(item, str):
            raise SubagentError(
                code="CONFIG_PARSE_ERROR",
                message=f"`{field_name}.{key}` must be string",
                details={"field": field_name, "key": key},
            )
        string_map[key] = item
    return string_map


@dataclass(slots=True)
class Launcher:
    name: str
    backend_kind: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "backend": {"kind": self.backend_kind},
            "command": self.command,
            "args": self.args,
            "env": self.env,
        }


@dataclass(slots=True)
class Profile:
    name: str
    prompt_language: str = "en"
    response_language: str = "same_as_manager"
    auto_handoff: str | None = None
    policy_preset: str | None = None
    default_packs: list[str] = field(default_factory=list)
    bootstrap: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "promptLanguage": self.prompt_language,
            "responseLanguage": self.response_language,
            "autoHandoff": self.auto_handoff,
            "policyPreset": self.policy_preset,
            "defaultPacks": self.default_packs,
            "bootstrap": self.bootstrap,
        }


@dataclass(slots=True)
class Pack:
    name: str
    description: str = ""
    prompt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "prompt": self.prompt,
        }


@dataclass(slots=True)
class SubagentConfig:
    path: Path
    loaded: bool
    launchers: dict[str, Launcher] = field(default_factory=dict)
    profiles: dict[str, Profile] = field(default_factory=dict)
    packs: dict[str, Pack] = field(default_factory=dict)
    policy_presets: dict[str, Any] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "loaded": self.loaded,
            "launchers": {name: launcher.to_dict() for name, launcher in self.launchers.items()},
            "profiles": {name: profile.to_dict() for name, profile in self.profiles.items()},
            "packs": {name: pack.to_dict() for name, pack in self.packs.items()},
            "policyPresets": self.policy_presets,
            "defaults": self.defaults,
        }


def _load_raw_config(config_path: Path) -> Mapping[str, Any]:
    contents = config_path.read_text(encoding="utf-8")

    # Use YAML if available; fallback to JSON for sandbox/offline bootstrap.
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        try:
            parsed = json.loads(contents)
        except json.JSONDecodeError as exc:
            try:
                parsed = parse_yaml_subset(contents)
            except SimpleYamlParseError as yaml_exc:
                raise SubagentError(
                    code="CONFIG_PARSE_ERROR",
                    message=(
                        "Failed to parse config as JSON or YAML subset. "
                        "Install PyYAML for full YAML support."
                    ),
                    details={
                        "path": str(config_path),
                        "jsonError": str(exc),
                        "yamlError": str(yaml_exc),
                    },
                ) from yaml_exc
    else:
        try:
            parsed = yaml.safe_load(contents)
        except Exception as exc:  # pragma: no cover - parser raises varied exceptions
            raise SubagentError(
                code="CONFIG_PARSE_ERROR",
                message=f"Failed to parse config file: {config_path}",
                details={"path": str(config_path), "error": str(exc)},
            ) from exc

    if parsed is None:
        parsed = {}
    return _ensure_mapping(parsed, field_name="root")


def _parse_launchers(raw: Any) -> dict[str, Launcher]:
    mapping = _ensure_mapping(raw or {}, field_name="launchers")
    launchers: dict[str, Launcher] = {}
    for name, payload in mapping.items():
        entry = _ensure_mapping(payload, field_name=f"launchers.{name}")
        backend = _ensure_mapping(entry.get("backend", {}), field_name=f"launchers.{name}.backend")
        command = entry.get("command")
        if not isinstance(command, str) or not command.strip():
            raise SubagentError(
                code="CONFIG_PARSE_ERROR",
                message=f"`launchers.{name}.command` must be a non-empty string",
                details={"launcher": name},
            )
        launchers[name] = Launcher(
            name=name,
            backend_kind=str(backend.get("kind", "acp-stdio")),
            command=command,
            args=_ensure_string_list(entry.get("args"), field_name=f"launchers.{name}.args"),
            env=_ensure_string_map(entry.get("env"), field_name=f"launchers.{name}.env"),
        )
    return launchers


def _parse_profiles(raw: Any) -> dict[str, Profile]:
    mapping = _ensure_mapping(raw or {}, field_name="profiles")
    profiles: dict[str, Profile] = {}
    for name, payload in mapping.items():
        entry = _ensure_mapping(payload, field_name=f"profiles.{name}")
        prompt_language = entry.get("promptLanguage", "en")
        response_language = entry.get("responseLanguage", "same_as_manager")
        auto_handoff = entry.get("autoHandoff")
        policy_preset = entry.get("policyPreset")
        bootstrap = entry.get("bootstrap", "")
        if not isinstance(prompt_language, str) or not isinstance(response_language, str):
            raise SubagentError(
                code="CONFIG_PARSE_ERROR",
                message=f"`profiles.{name}` language fields must be strings",
                details={"profile": name},
            )
        if auto_handoff is not None and not isinstance(auto_handoff, str):
            raise SubagentError(
                code="CONFIG_PARSE_ERROR",
                message=f"`profiles.{name}.autoHandoff` must be string or null",
                details={"profile": name},
            )
        if policy_preset is not None and not isinstance(policy_preset, str):
            raise SubagentError(
                code="CONFIG_PARSE_ERROR",
                message=f"`profiles.{name}.policyPreset` must be string or null",
                details={"profile": name},
            )
        if not isinstance(bootstrap, str):
            raise SubagentError(
                code="CONFIG_PARSE_ERROR",
                message=f"`profiles.{name}.bootstrap` must be a string",
                details={"profile": name},
            )
        profiles[name] = Profile(
            name=name,
            prompt_language=prompt_language,
            response_language=response_language,
            auto_handoff=auto_handoff,
            policy_preset=policy_preset,
            default_packs=_ensure_string_list(
                entry.get("defaultPacks"),
                field_name=f"profiles.{name}.defaultPacks",
            ),
            bootstrap=bootstrap,
        )
    return profiles


def _parse_packs(raw: Any) -> dict[str, Pack]:
    mapping = _ensure_mapping(raw or {}, field_name="packs")
    packs: dict[str, Pack] = {}
    for name, payload in mapping.items():
        entry = _ensure_mapping(payload, field_name=f"packs.{name}")
        description = entry.get("description", "")
        prompt = entry.get("prompt", "")
        if not isinstance(description, str) or not isinstance(prompt, str):
            raise SubagentError(
                code="CONFIG_PARSE_ERROR",
                message=f"`packs.{name}` fields must be strings",
                details={"pack": name},
            )
        packs[name] = Pack(name=name, description=description, prompt=prompt)
    return packs


def load_config(config_path: Path | None = None) -> SubagentConfig:
    resolved_path = resolve_config_path(config_path)
    if not resolved_path.exists():
        return SubagentConfig(path=resolved_path, loaded=False)
    raw = _load_raw_config(resolved_path)
    policy_presets = raw.get("policyPresets", {})
    defaults = raw.get("defaults", {})
    if policy_presets is None:
        policy_presets = {}
    if defaults is None:
        defaults = {}
    if not isinstance(policy_presets, dict):
        raise SubagentError(
            code="CONFIG_PARSE_ERROR",
            message="`policyPresets` must be a mapping",
            details={"field": "policyPresets"},
        )
    if not isinstance(defaults, dict):
        raise SubagentError(
            code="CONFIG_PARSE_ERROR",
            message="`defaults` must be a mapping",
            details={"field": "defaults"},
        )
    return SubagentConfig(
        path=resolved_path,
        loaded=True,
        launchers=_parse_launchers(raw.get("launchers")),
        profiles=_parse_profiles(raw.get("profiles")),
        packs=_parse_packs(raw.get("packs")),
        policy_presets=policy_presets,
        defaults=defaults,
    )
