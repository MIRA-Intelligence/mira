"""The team layer: provider keys and defaults administrators set for every user.

Administrators (members of ``auth.admins_group``) edit it from the desktop app:
for their sessions Mira Server answers the config endpoints itself
(``/api/config``, ``/api/providers/{name}/models|test``) in the engine's own
payload format, against ``<data_dir>/team-config.json`` (mode 0600, the
engine's ``config.json`` format, holding only what administrators set).

Engines never see the team's keys. Mira Server pushes each engine a layer
(``mira_engine/config/team_layer.py``) in which every team provider points at
Mira Server's LLM proxy (``/llm/<provider>``) with the engine's own token as
its key; the proxy swaps that token for the real key.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic.alias_generators import to_camel

from mira_engine.config import team_layer
from mira_engine.config.schema import Config, ProvidersConfig
from mira_engine.config.ui_runtime import (
    apply_ui_runtime_update,
    apply_ui_runtime_update_to_raw_data,
    build_ui_runtime_payload,
)
from mira_engine.providers.registry import find_by_name

TEAM_FILENAME = "team-config.json"
# Upstream endpoints for providers whose engine client has a built-in default
# instead of a registry ``default_api_base``.
_FALLBACK_BASES = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "qianfan": "https://qianfan.baidubce.com/v2",
}
# Runtime settings that belong to each user or to the host policy, not the team.
_NOT_TEAM_RUNTIME = ("workspace", "restrict_to_workspace")


class TeamConfigError(ValueError):
    """An administrator's change is invalid (answered with 400)."""


@dataclass(frozen=True)
class Upstream:
    base: str
    api_key: str


def _provider_names() -> tuple[str, ...]:
    return tuple(name for name in ProvidersConfig.model_fields
                 if name not in {"proxy", "model_params", "custom_providers"})


def _custom_records(providers: dict[str, Any]) -> dict[str, Any]:
    """Named custom providers (``custom_<id>``) in raw team settings."""
    records = _get(providers, "custom_providers")
    return records if isinstance(records, dict) else {}


def _get(record: dict[str, Any], field_name: str, default: Any = None) -> Any:
    alias = to_camel(field_name)
    if alias in record:
        return record[alias]
    return record.get(field_name, default)


def upstream_base(name: str, api_base: str | None) -> str | None:
    spec = find_by_name(name)
    return (api_base or "").strip() or (spec.default_api_base if spec else None) or _FALLBACK_BASES.get(name)


class TeamConfig:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._raw = self._read()

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise TeamConfigError(f"cannot read the team configuration {self.path}: {exc}") from None
        if not isinstance(data, dict):
            raise TeamConfigError(f"{self.path} must hold a JSON object")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    @property
    def projects_root(self) -> Path:
        # build_ui_runtime_payload needs one; projects are per user, not a team setting.
        return self.path.parent

    def engine_config(self) -> Config:
        return Config.model_validate(copy.deepcopy(self._raw))

    # ── what administrators see and change ────────────────────────────

    def payload(self, *, persisted: bool = False) -> dict[str, Any]:
        payload = build_ui_runtime_payload(self.engine_config(), projects_root=self.projects_root,
                                           config_path=self.path, persisted=persisted)
        payload["scope"] = "team"
        payload["project_location"] = {
            "mode": "managed", "custom_dir_allowed": False,
            "default_parent_dir": str(self.projects_root), "workspace_file": "",
        }
        return payload

    def _strip_user_settings(self, body: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        """Drop per-user and host-policy settings the app re-sends unchanged; refuse changes."""
        body = dict(body)
        if "projects_root" in body:
            if body.pop("projects_root") != current["projects_root"]:
                raise TeamConfigError("the projects folder is set per user, not for the team")
        runtime = body.get("runtime")
        if isinstance(runtime, dict):
            runtime = dict(runtime)
            for key in _NOT_TEAM_RUNTIME:
                if key in runtime and runtime.pop(key) != current["runtime"][key]:
                    where = "per user" if key == "workspace" else "by the host policy (/etc/mira/policy.toml)"
                    raise TeamConfigError(f"runtime.{key} is set {where}, not for the team")
            body["runtime"] = runtime
        return body

    def _check_providers(self, config: Config, body: dict[str, Any]) -> None:
        providers = body.get("providers")
        if not isinstance(providers, dict):
            return
        for name, update in providers.items():
            if name == "proxy" or not isinstance(update, dict):
                continue
            spec = find_by_name(name)
            # Turning an OAuth provider off (or on) is fine; giving it a team key is not.
            sets_credential = any(isinstance(update.get(key), str) and update[key].strip()
                                  for key in ("api_key", "api_base"))
            if spec is not None and spec.is_oauth and sets_credential:
                raise TeamConfigError(f"{spec.label} signs in per user and cannot be a team provider")
            record = config.providers.entry(name)
            if record is None or not (record.api_key or record.api_base):
                continue
            if not upstream_base(name, record.api_base):
                label = spec.label if spec else name
                raise TeamConfigError(f"set the API Base of {label} to use it as a team provider")

    def update(self, body: dict[str, Any]) -> tuple[bool, list[str]]:
        """Apply an ``/api/config`` body; returns (changed, changed setting paths)."""
        body = self._strip_user_settings(body, self.payload())
        config = self.engine_config()
        try:
            _, changed = apply_ui_runtime_update(config, body, current_projects_root=self.projects_root)
        except ValueError as exc:
            raise TeamConfigError(str(exc)) from None
        self._check_providers(config, body)
        if not changed:
            return False, []
        raw = copy.deepcopy(self._raw)
        apply_ui_runtime_update_to_raw_data(raw, body, current_projects_root=self.projects_root)
        paths = changed_paths(self._raw, raw)
        if not paths:
            return False, []
        self._write(raw)
        self._raw = raw
        return True, paths

    # ── what engines get ──────────────────────────────────────────────

    def team_providers(self) -> dict[str, dict[str, Any]]:
        """Providers administrators configured (a key or an endpoint), by name."""
        providers = self._raw.get("providers")
        if not isinstance(providers, dict):
            return {}
        found: dict[str, dict[str, Any]] = {}
        named = [(name, _get(providers, name)) for name in _provider_names()]
        named += list(_custom_records(providers).items())
        for name, record in named:
            if not isinstance(record, dict):
                continue
            spec = find_by_name(name)
            if spec is not None and spec.is_oauth:
                continue
            api_key = (_get(record, "api_key", "") or "").strip()
            api_base = (_get(record, "api_base") or "") or None
            if not api_key and not api_base:
                continue
            found[name] = {"api_key": api_key, "api_base": api_base,
                           "models": list(_get(record, "models", []) or []), "enabled": _get(record, "enabled")}
            if name.startswith("custom_"):
                found[name]["name"] = _get(record, "name")
        return found

    def disabled_providers(self) -> list[str]:
        """Providers administrators turned off (other than team providers): off for every user."""
        providers = self._raw.get("providers")
        if not isinstance(providers, dict):
            return []
        team = self.team_providers()
        return [name for name in _provider_names()
                if name not in team and isinstance(_get(providers, name), dict)
                and _get(_get(providers, name), "enabled") is False]

    def upstream(self, name: str) -> Upstream | None:
        record = self.team_providers().get(name)
        if record is None:
            return None
        base = upstream_base(name, record["api_base"])
        return Upstream(base=base.rstrip("/"), api_key=record["api_key"]) if base else None

    def proxy(self) -> str | None:
        providers = self._raw.get("providers")
        value = providers.get("proxy") if isinstance(providers, dict) else None
        return value or None

    def revision(self) -> str:
        text = json.dumps(self._raw, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    def layer_for(self, engine_token: str, proxy_base: str) -> dict[str, Any]:
        """The layer for one engine (``mira_engine.config.team_layer.parse_layer`` format)."""
        providers = {}
        for name, record in self.team_providers().items():
            providers[name] = {"api_key": engine_token, "api_base": f"{proxy_base}/{name}",
                               "models": record["models"], "enabled": record["enabled"]}
            if "name" in record:
                providers[name]["name"] = record["name"]
        layer: dict[str, Any] = {"revision": self.revision(), "providers": providers,
                                 "disabled": self.disabled_providers()}
        raw_providers = self._raw.get("providers")
        if isinstance(raw_providers, dict):
            rules = _get(raw_providers, "model_params")
            if isinstance(rules, list):
                layer["model_params"] = rules
        raw_defaults = _get(self._raw.get("agents") or {}, "defaults") or {}
        if isinstance(raw_defaults, dict):
            layer["defaults"] = {name: _get(raw_defaults, name) for name in team_layer.DEFAULT_FIELDS
                                 if to_camel(name) in raw_defaults or name in raw_defaults}
        return layer


def _flatten(value: Any, prefix: str, out: dict[str, str]) -> None:
    if isinstance(value, dict):  # an empty section is the same as none
        for key, item in value.items():
            _flatten(item, f"{prefix}.{key}" if prefix else str(key), out)
    else:
        out[prefix] = json.dumps(value, sort_keys=True)


def changed_paths(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Setting paths that differ, for the audit log (never values)."""
    old: dict[str, str] = {}
    new: dict[str, str] = {}
    _flatten(before, "", old)
    _flatten(after, "", new)
    return sorted(path for path in old.keys() | new.keys() if old.get(path) != new.get(path))
