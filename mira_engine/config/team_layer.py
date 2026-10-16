"""Team layer: settings Mira Server administrators manage for every user's engine.

Mira Server pushes the layer to each engine it runs (``PUT /api/team/layer``)
when the engine starts and whenever an administrator changes it. The engine
keeps it in memory only and applies it to the user's raw ``config.json`` in
:func:`mira_engine.config.loader.load_config`, before validation:

- **Providers** the team configures replace the user's entry for that provider
  and cannot be changed through the API (:class:`TeamLockedError`). Their
  endpoint is Mira Server's LLM proxy and their key is this engine's gateway
  token, so the team's real keys never reach the engine.
- **Named custom providers** (``custom_<id>``) the team defines are added to
  the user's ``providers.customProviders`` the same way, under the team's name.
- **Disabled providers**: providers administrators turned off are off for the
  user too and cannot be turned on (the user's own key is kept, unused).
- **model_params**, when the team sets them, replace the user's rules (locked).
- **Defaults** (provider and model, reasoning effort, ...) apply only where the
  user has not chosen a value: the key is missing from config.json or still
  holds the built-in default (engines write every default into config.json the
  first time they start). Provider/model pairs, including the team-role pairs,
  are filled together so a team model never ends up with a provider the user
  chose for another model.

The user's own values are remembered on the Config (``_team_user_values``) and
written back by :func:`restore_team_values` when the config is saved, so the
layer never ends up in the user's config.json. A default the user leaves at
the team value is not saved either, so it keeps following the team.

Host policy (``/etc/mira/policy.toml``) is applied after the layer and wins.

Parsing is lenient where a newer Mira Server may send more than this engine
knows (keys, providers): those are skipped, so the rest of the layer applies.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger
from pydantic.alias_generators import to_camel

from mira_engine.security.host_policy import PolicyLockedError

if TYPE_CHECKING:
    from mira_engine.config.schema import Config

LOCKED_ERROR_CODE = "locked_by_team"

# Runtime defaults a team may set, as AgentDefaults field names. Pairs are
# filled together (see the module docstring).
DEFAULT_GROUPS: tuple[tuple[str, ...], ...] = (
    ("provider", "model"),
    ("supervisor_provider", "supervisor_model"),
    ("student_provider", "student_model"),
    ("critic_provider", "critic_model"),
    ("reasoning_effort",),
    ("temperature",),
    ("max_tool_iterations",),
    ("auto_max_rounds",),
)
DEFAULT_FIELDS: tuple[str, ...] = tuple(name for group in DEFAULT_GROUPS for name in group)
PROVIDER_KEYS = ("api_key", "api_base", "models", "enabled")
CUSTOM_CONTAINER = "customProviders"
# What the desktop app writes for an engine nobody set up yet: not a choice.
_SETUP_PLACEHOLDER_MODEL = "custom/mira-ui-bundle-setup"

_ABSENT = object()


class TeamLayerError(ValueError):
    """The pushed layer is malformed."""


class TeamLockedError(PolicyLockedError):
    """A request tried to change a setting the team layer enforces."""

    code = LOCKED_ERROR_CODE


@dataclass(frozen=True)
class TeamLayer:
    revision: str = ""
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    model_params: list[dict[str, Any]] | None = None
    defaults: dict[str, Any] = field(default_factory=dict)
    disabled: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.providers and self.model_params is None and not self.defaults and not self.disabled

    def manages(self, provider_name: str) -> bool:
        return provider_name in self.providers or provider_name in self.disabled

    def summary(self) -> dict[str, Any]:
        """What the layer manages, for the config payload (no values)."""
        return {
            "revision": self.revision,
            "providers": sorted(self.providers),
            "disabled": sorted(self.disabled),
            "model_params": self.model_params is not None,
            "defaults": sorted(self.defaults),
        }


_lock = threading.Lock()
_current = TeamLayer()


def parse_layer(data: Any) -> TeamLayer:
    """Validate a pushed layer (``None`` or ``{}`` clears it)."""
    from mira_engine.config.schema import ProvidersConfig

    if data is None:
        return TeamLayer()
    if not isinstance(data, dict):
        raise TeamLayerError("layer must be an object or null")
    unknown = sorted(set(data) - {"revision", "providers", "disabled", "model_params", "defaults"})
    if unknown:
        logger.warning("Team layer: ignoring unknown key(s) {} (a newer Mira Server)", ", ".join(unknown))
    revision = data.get("revision", "")
    if not isinstance(revision, str):
        raise TeamLayerError("revision must be a string")

    providers: dict[str, dict[str, Any]] = {}
    raw_providers = data.get("providers") or {}
    if not isinstance(raw_providers, dict):
        raise TeamLayerError("providers must be an object")
    from mira_engine.config.schema import CUSTOM_PROVIDER_ID_PATTERN

    known = set(ProvidersConfig.model_fields) - {"proxy", "model_params", "custom_providers"}

    def usable(name: str) -> bool:
        if name in known or CUSTOM_PROVIDER_ID_PATTERN.match(name):
            return True
        logger.warning("Team layer: ignoring provider {} that this engine does not know", name)
        return False

    for name, record in raw_providers.items():
        if not isinstance(record, dict):
            raise TeamLayerError(f"providers.{name} must be an object")
        if not usable(name):
            continue
        display = record.get("name")
        if display is not None and not isinstance(display, str):
            raise TeamLayerError(f"providers.{name}.name must be a string")
        api_key, api_base = record.get("api_key", ""), record.get("api_base")
        models, enabled = record.get("models", []), record.get("enabled")
        if not isinstance(api_key, str) or not (api_base is None or isinstance(api_base, str)):
            raise TeamLayerError(f"providers.{name}: api_key and api_base must be strings")
        if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
            raise TeamLayerError(f"providers.{name}.models must be a list of strings")
        if enabled is not None and not isinstance(enabled, bool):
            raise TeamLayerError(f"providers.{name}.enabled must be a boolean or null")
        providers[name] = {"api_key": api_key, "api_base": api_base, "models": list(models), "enabled": enabled}
        if CUSTOM_PROVIDER_ID_PATTERN.match(name):
            providers[name]["name"] = display or None

    disabled = data.get("disabled") or []
    if not isinstance(disabled, list) or not all(isinstance(name, str) for name in disabled):
        raise TeamLayerError("disabled must be a list of provider names")
    disabled = [name for name in disabled if name in known or usable(name)]
    disabled = [name for name in disabled if not CUSTOM_PROVIDER_ID_PATTERN.match(name)]

    model_params = data.get("model_params")
    if model_params is not None:
        from mira_engine.config.ui_runtime import _normalize_model_param_rules

        try:
            model_params = _normalize_model_param_rules(model_params)
        except ValueError as exc:
            raise TeamLayerError(str(exc)) from None

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise TeamLayerError("defaults must be an object")
    unknown = sorted(set(defaults) - set(DEFAULT_FIELDS))
    if unknown:
        raise TeamLayerError(f"unknown default(s): {', '.join(unknown)}")
    return TeamLayer(revision=revision, providers=providers, model_params=model_params, defaults=dict(defaults),
                     disabled=tuple(sorted(set(disabled) - set(providers))))


def set_team_layer(layer: TeamLayer) -> bool:
    """Replace the current layer; returns whether it changed."""
    global _current
    with _lock:
        changed = layer != _current
        _current = layer
    return changed


def get_team_layer() -> TeamLayer:
    with _lock:
        return _current


def clear_team_layer() -> None:
    set_team_layer(TeamLayer())


# ── applying the layer to raw config data ─────────────────────────────


def _pop_key(record: dict[str, Any], field_name: str) -> tuple[str | None, Any]:
    """Remove a field spelled either way; return (original key, value)."""
    alias = to_camel(field_name)
    for key in (alias, field_name):
        if key in record:
            return key, record.pop(key)
    return None, _ABSENT


def _user_chose(record: dict[str, Any], field_name: str) -> bool:
    """The user's config holds a value other than the built-in default."""
    from mira_engine.config.schema import AgentDefaults

    default = AgentDefaults.model_fields[field_name].get_default(call_default_factory=True)
    for key in (field_name, to_camel(field_name)):
        if key in record and record[key] != default:
            return True
    return False


def _record(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        value = {}
        parent[key] = value
    return value


def _is_custom(name: str) -> bool:
    from mira_engine.config.schema import CUSTOM_PROVIDER_ID_PATTERN

    return bool(CUSTOM_PROVIDER_ID_PATTERN.match(name))


def _provider_path(providers: dict[str, Any], name: str) -> tuple[tuple[str, ...], dict[str, Any], str]:
    """(path under the config, container dict, key) where ``name``'s record lives."""
    if not _is_custom(name):
        return ("providers", to_camel(name)), providers, to_camel(name)
    if "custom_providers" in providers and CUSTOM_CONTAINER not in providers:
        providers[CUSTOM_CONTAINER] = providers.pop("custom_providers")
    container = _record(providers, CUSTOM_CONTAINER)
    return ("providers", CUSTOM_CONTAINER, name), container, name


@dataclass
class TeamValues:
    """The user's own values of what the layer changed (kept on the Config)."""

    layer: TeamLayer = field(default_factory=TeamLayer)
    # path (e.g. ("providers", "openai"), ("providers", "customProviders", "custom_x"),
    # ("providers", "modelParams")) -> (original key, value)
    replaced: dict[tuple[str, ...], tuple[str | None, Any]] = field(default_factory=dict)
    # AgentDefaults alias -> effective value after validation (filled defaults)
    filled: dict[str, Any] = field(default_factory=dict)


def apply_to_raw(data: dict[str, Any], layer: TeamLayer | None = None) -> TeamValues:
    """Apply ``layer`` (default: the current one) to raw config data in place."""
    layer = layer if layer is not None else get_team_layer()
    values = TeamValues(layer=layer)
    if layer.empty:
        return values
    if layer.providers or layer.disabled or layer.model_params is not None:
        providers = _record(data, "providers")
        for name in layer.disabled:
            path, container, key = _provider_path(providers, name)
            original = _pop_key(container, name)
            values.replaced[path] = original
            value = original[1]
            container[key] = {**(value if isinstance(value, dict) else {}), "enabled": False}
        for name, record in layer.providers.items():
            path, container, key = _provider_path(providers, name)
            if _is_custom(name):
                values.replaced[path] = (name, container.pop(name)) if name in container else (None, _ABSENT)
            else:
                values.replaced[path] = _pop_key(container, name)
            container[key] = {
                "apiKey": record["api_key"], "apiBase": record["api_base"],
                "models": list(record["models"]), "enabled": record["enabled"],
            }
            if record.get("name"):
                container[key]["name"] = record["name"]
        if layer.model_params is not None:
            values.replaced[("providers", "modelParams")] = _pop_key(providers, "model_params")
            providers["modelParams"] = [dict(rule) for rule in layer.model_params]
    if layer.defaults:
        defaults = _record(_record(data, "agents"), "defaults")
        for group in DEFAULT_GROUPS:
            present = [name for name in group if name in layer.defaults]
            placeholder = group == ("provider", "model") and defaults.get("model") == _SETUP_PLACEHOLDER_MODEL
            if not present or (not placeholder and any(_user_chose(defaults, name) for name in group)):
                continue
            for name in present:
                defaults.pop(name, None)
                defaults[to_camel(name)] = layer.defaults[name]
                values.filled[to_camel(name)] = _ABSENT  # set after validation
    return values


def attach(config: "Config", values: TeamValues) -> None:
    """Record the applied layer on a validated Config."""
    dumped = config.agents.defaults.model_dump(by_alias=True)
    values.filled = {alias: dumped.get(alias) for alias in values.filled}
    config._team_values = values


def team_values(config: "Config") -> TeamValues:
    values = getattr(config, "_team_values", None)
    return values if isinstance(values, TeamValues) else TeamValues()


def restore_team_values(config: "Config", data: dict[str, Any]) -> dict[str, Any]:
    """Take the layer back out of a ``model_dump(by_alias=True)`` before saving.

    Replaced records get the user's own value back (or disappear). A filled
    default disappears while it still has the team value; a value the user
    changed is kept.
    """
    values = team_values(config)
    for path, (key, value) in values.replaced.items():
        parent = data
        for part in path[:-1]:
            parent = _record(parent, part)
        parent.pop(path[-1], None)
        if key is not None:
            parent[key] = value
    if values.filled:
        defaults = _record(_record(data, "agents"), "defaults")
        for alias, team_value in values.filled.items():
            if alias in defaults and defaults[alias] == team_value:
                del defaults[alias]
            snake = _snake(alias)
            if snake in defaults and defaults[snake] == team_value:
                del defaults[snake]
    return data


def _snake(alias: str) -> str:
    return "".join(f"_{ch.lower()}" if ch.isupper() else ch for ch in alias)


# ── API locks ─────────────────────────────────────────────────────────


def managed_provider(config: "Config", provider_name: str) -> bool:
    return team_values(config).layer.manages(provider_name)


def check_provider_change(config: "Config", provider_name: str, update: dict[str, Any]) -> None:
    """Raise :class:`TeamLockedError` when ``update`` changes a team or team-disabled provider."""
    layer = team_values(config).layer
    record = layer.providers.get(provider_name)
    if record is None and provider_name in layer.disabled:
        current = config.providers.entry(provider_name)
        record = {"api_key": current.api_key, "api_base": current.api_base,
                  "models": list(current.models), "enabled": False}
    if record is None:
        return
    for key in (*PROVIDER_KEYS, "name"):
        if key not in update or (key == "name" and key not in record):
            continue
        requested = update[key]
        current = record[key]
        if key == "name":
            requested = (requested or "").strip() if isinstance(requested, str) else requested
            current = current or ""
        elif key == "api_key":
            # The UI never has the key; an empty or unchanged value is no change.
            if not isinstance(requested, str) or not requested.strip() or requested.strip() == current:
                continue
        elif key == "api_base":
            requested = None if requested in (None, "") else str(requested).strip()
        elif key == "models" and isinstance(requested, list):
            requested = [str(m).strip() for m in requested if str(m).strip()]
        if requested != current:
            what = ("turned off by the Team Server administrators" if provider_name in layer.disabled
                    else "managed by the Team Server administrators")
            raise TeamLockedError(f"providers.{provider_name} is {what} and cannot be changed here")


def check_model_params_change(config: "Config", rules: list[dict[str, Any]]) -> None:
    layer = team_values(config).layer
    if layer.model_params is not None and rules != layer.model_params:
        raise TeamLockedError("model_params are managed by the Team Server administrators "
                              "and cannot be changed here")
