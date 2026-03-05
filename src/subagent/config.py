"""Config model and loader for launchers / role hints / defaults."""

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


def _ensure_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise SubagentError(
            code="CONFIG_PARSE_ERROR",
            message=f"`{field_name}` must be a string",
            details={"field": field_name},
        )
    return value


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
class RoleDefaults:
    prompt_language: str = "en"
    response_language: str = "same_as_manager"

    def to_dict(self) -> dict[str, Any]:
        return {
            "promptLanguage": self.prompt_language,
            "responseLanguage": self.response_language,
        }


@dataclass(slots=True)
class RoleHint:
    name: str
    preferred_launcher: str | None = None
    prompt_language: str | None = None
    response_language: str | None = None
    delegation_hint: str | None = None
    recommended_skills: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
        }
        if self.preferred_launcher is not None:
            payload["preferredLauncher"] = self.preferred_launcher
        if self.prompt_language is not None:
            payload["promptLanguage"] = self.prompt_language
        if self.response_language is not None:
            payload["responseLanguage"] = self.response_language
        if self.delegation_hint is not None:
            payload["delegationHint"] = self.delegation_hint
        payload["recommendedSkills"] = list(self.recommended_skills)
        return payload


@dataclass(slots=True)
class SubagentConfig:
    path: Path
    loaded: bool
    launchers: dict[str, Launcher] = field(default_factory=dict)
    role_hints: dict[str, RoleHint] = field(default_factory=dict)
    role_defaults: RoleDefaults = field(default_factory=RoleDefaults)
    defaults: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "loaded": self.loaded,
            "launchers": {name: launcher.to_dict() for name, launcher in self.launchers.items()},
            "roleHints": {name: role_hint.to_dict() for name, role_hint in self.role_hints.items()},
            "roleDefaults": self.role_defaults.to_dict(),
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


def _parse_role_defaults(raw: Any) -> RoleDefaults:
    mapping = _ensure_mapping(raw or {}, field_name="roleDefaults")
    prompt_language = mapping.get("promptLanguage", "en")
    response_language = mapping.get("responseLanguage", "same_as_manager")
    return RoleDefaults(
        prompt_language=_ensure_string(prompt_language, field_name="roleDefaults.promptLanguage"),
        response_language=_ensure_string(response_language, field_name="roleDefaults.responseLanguage"),
    )


def _parse_role_hints(raw: Any) -> dict[str, RoleHint]:
    mapping = _ensure_mapping(raw or {}, field_name="roleHints")
    role_hints: dict[str, RoleHint] = {}
    for name, payload in mapping.items():
        entry = _ensure_mapping(payload, field_name=f"roleHints.{name}")
        preferred_launcher = entry.get("preferredLauncher")
        prompt_language = entry.get("promptLanguage")
        response_language = entry.get("responseLanguage")
        delegation_hint = entry.get("delegationHint")
        recommended_skills_raw = entry.get("recommendedSkills")
        if preferred_launcher is not None and not isinstance(preferred_launcher, str):
            raise SubagentError(
                code="CONFIG_PARSE_ERROR",
                message=f"`roleHints.{name}.preferredLauncher` must be string or null",
                details={"roleHint": name},
            )
        if prompt_language is not None and not isinstance(prompt_language, str):
            raise SubagentError(
                code="CONFIG_PARSE_ERROR",
                message=f"`roleHints.{name}.promptLanguage` must be string or null",
                details={"roleHint": name},
            )
        if response_language is not None and not isinstance(response_language, str):
            raise SubagentError(
                code="CONFIG_PARSE_ERROR",
                message=f"`roleHints.{name}.responseLanguage` must be string or null",
                details={"roleHint": name},
            )
        if delegation_hint is not None and not isinstance(delegation_hint, str):
            raise SubagentError(
                code="CONFIG_PARSE_ERROR",
                message=f"`roleHints.{name}.delegationHint` must be string or null",
                details={"roleHint": name},
            )
        recommended_skills = _ensure_string_list(
            recommended_skills_raw,
            field_name=f"roleHints.{name}.recommendedSkills",
        )
        normalized_recommended_skills: list[str] = []
        for idx, skill_name in enumerate(recommended_skills):
            normalized_name = skill_name.strip()
            if not normalized_name:
                raise SubagentError(
                    code="CONFIG_PARSE_ERROR",
                    message=f"`roleHints.{name}.recommendedSkills[{idx}]` must be non-empty",
                    details={"roleHint": name, "index": idx},
                )
            normalized_recommended_skills.append(normalized_name)
        normalized_delegation_hint: str | None = None
        if isinstance(delegation_hint, str):
            trimmed_hint = delegation_hint.strip()
            normalized_delegation_hint = trimmed_hint if trimmed_hint else None
        role_hints[name] = RoleHint(
            name=name,
            preferred_launcher=preferred_launcher.strip() if isinstance(preferred_launcher, str) else None,
            prompt_language=prompt_language,
            response_language=response_language,
            delegation_hint=normalized_delegation_hint,
            recommended_skills=normalized_recommended_skills,
        )
    return role_hints


def load_config(config_path: Path | None = None) -> SubagentConfig:
    resolved_path = resolve_config_path(config_path)
    if not resolved_path.exists():
        return SubagentConfig(path=resolved_path, loaded=False)
    raw = _load_raw_config(resolved_path)
    defaults = raw.get("defaults", {})
    if defaults is None:
        defaults = {}
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
        role_hints=_parse_role_hints(raw.get("roleHints")),
        role_defaults=_parse_role_defaults(raw.get("roleDefaults")),
        defaults=defaults,
    )
