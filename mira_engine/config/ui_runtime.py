"""UI-facing runtime config serialization and validation helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic.alias_generators import to_camel

from mira_engine.config.schema import AgentDefaults, Config, ProvidersConfig
from mira_engine.providers.registry import find_by_name

_ALLOWED_REASONING_EFFORTS = {"low", "medium", "high", "adaptive"}
_BUNDLE_SETUP_PROVIDER = "custom"
_BUNDLE_SETUP_MODEL = "custom/mira-ui-bundle-setup"
_BUNDLE_SETUP_API_BASE = "http://127.0.0.1:9/v1"


def _mask_secret(value: str) -> str | None:
    text = value.strip()
    if not text:
        return None
    if len(text) <= 6:
        return "*" * len(text)
    return f"{text[:4]}...{text[-2:]}"


def _provider_field_names() -> tuple[str, ...]:
    # ProvidersConfig also contains global provider settings such as `proxy`.
    # The UI provider map below only serializes concrete ProviderConfig entries.
    return tuple(name for name in ProvidersConfig.model_fields.keys() if name != "proxy")


def _provider_display_name(provider_name: str) -> str:
    if provider_name == "auto":
        return "Auto-detect"
    spec = find_by_name(provider_name)
    if spec is not None:
        return spec.label
    return provider_name.replace("_", " ").replace("-", " ").title()


def _provider_metadata(provider_name: str) -> dict[str, Any]:
    if provider_name == "auto":
        return {
            "display_name": _provider_display_name(provider_name),
            "api_key_required": False,
            "api_base_required": False,
            "default_api_base": None,
            "is_oauth": False,
            "is_local": False,
        }

    spec = find_by_name(provider_name)
    if spec is None:
        return {
            "display_name": _provider_display_name(provider_name),
            "api_key_required": provider_name != "custom",
            "api_base_required": provider_name == "custom",
            "default_api_base": None,
            "is_oauth": False,
            "is_local": False,
        }

    api_key_required = not (spec.is_oauth or spec.is_local or provider_name == "custom")
    api_base_required = provider_name in {"custom", "azure_openai"} or (
        spec.is_local and not spec.default_api_base
    )
    return {
        "display_name": spec.label,
        "api_key_required": api_key_required,
        "api_base_required": api_base_required,
        "default_api_base": spec.default_api_base or None,
        "is_oauth": bool(spec.is_oauth),
        "is_local": bool(spec.is_local),
    }


def _ensure_json_record(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if isinstance(value, dict):
        return value
    record: dict[str, Any] = {}
    parent[key] = record
    return record


def _key_for_alias(record: dict[str, Any], field_name: str, alias: str | None = None) -> str:
    alias = alias or to_camel(field_name)
    if field_name in record and alias not in record:
        return field_name
    if alias in record:
        return alias
    return alias


def _set_alias_value(
    record: dict[str, Any],
    field_name: str,
    value: Any,
    *,
    alias: str | None = None,
) -> None:
    record[_key_for_alias(record, field_name, alias)] = value


def _raw_model_matches_runtime_value(
    raw_value: Any,
    *,
    provider: str,
    runtime_model: str,
) -> bool:
    try:
        defaults = AgentDefaults.model_validate({"provider": provider, "model": raw_value})
    except Exception:
        return raw_value == runtime_model
    return defaults.primary_model == runtime_model


def _set_model_preserving_candidates(
    defaults: dict[str, Any],
    model: str,
    *,
    provider: str,
) -> None:
    current = defaults.get("model")
    if _raw_model_matches_runtime_value(current, provider=provider, runtime_model=model):
        return
    defaults["model"] = model


def _provider_config_record(
    providers: dict[str, Any],
    provider_name: str,
) -> dict[str, Any]:
    key = _key_for_alias(providers, provider_name, to_camel(provider_name))
    value = providers.get(key)
    if isinstance(value, dict):
        return value
    record: dict[str, Any] = {}
    providers[key] = record
    return record


def _build_provider_payload(config: Config) -> dict[str, dict[str, Any]]:
    providers: dict[str, dict[str, Any]] = {
        "auto": {
            "api_key_configured": False,
            "api_key_preview": None,
            "api_base": None,
            **_provider_metadata("auto"),
        }
    }
    for provider_name in _provider_field_names():
        provider_cfg = getattr(config.providers, provider_name)
        providers[provider_name] = {
            "api_key_configured": bool(provider_cfg.api_key),
            "api_key_preview": _mask_secret(provider_cfg.api_key),
            "api_base": provider_cfg.api_base,
            **_provider_metadata(provider_name),
        }
    return providers


def _runtime_setup_status(
    config: Config,
    providers_payload: dict[str, dict[str, Any]],
) -> tuple[bool, str | None, str | None, str | None]:
    defaults = config.agents.defaults
    provider_name = defaults.provider.strip() if isinstance(defaults.provider, str) else ""
    model = defaults.model.strip() if isinstance(defaults.model, str) else ""

    if not provider_name or not model:
        return (
            True,
            "Runtime provider/model is incomplete. Open Settings > Local Runtime Config and finish setup.",
            "missing_runtime",
            None,
        )

    provider_meta = providers_payload.get(provider_name)
    if provider_name != "auto" and provider_meta is None:
        return (
            True,
            f"Runtime provider '{provider_name}' is not recognized by this mira build.",
            "unknown_provider",
            provider_name,
        )

    if provider_name == _BUNDLE_SETUP_PROVIDER:
        custom_base = providers_payload.get("custom", {}).get("api_base")
        normalized_base = custom_base.rstrip("/") if isinstance(custom_base, str) else ""
        if model == _BUNDLE_SETUP_MODEL or normalized_base == _BUNDLE_SETUP_API_BASE.rstrip("/"):
            return (
                True,
                "Local engine is running, but model access is still unconfigured. Open Settings > Local Runtime Config and choose a provider before retrying.",
                "missing_api_base",
                "Custom",
            )
        if not isinstance(custom_base, str) or not custom_base.strip():
            return (
                True,
                "Custom provider API Base is empty. Open Settings > Local Runtime Config and set API Base.",
                "missing_api_base",
                "Custom",
            )

    if provider_meta and provider_meta.get("api_base_required"):
        api_base = provider_meta.get("api_base")
        if not isinstance(api_base, str) or not api_base.strip():
            label = str(provider_meta.get("display_name") or provider_name)
            return (
                True,
                f"{label} requires API Base. Open Settings > Local Runtime Config and update the endpoint.",
                "missing_api_base",
                label,
            )

    if provider_meta and provider_meta.get("api_key_required") and not provider_meta.get("api_key_configured"):
        label = str(provider_meta.get("display_name") or provider_name)
        return (
            True,
            f"{label} is missing its API key. Open Settings > Local Runtime Config and add the credential.",
            "missing_api_key",
            label,
        )

    return False, None, None, None


def build_ui_runtime_payload(
    config: Config,
    *,
    projects_root: Path,
    config_path: Path,
    persisted: bool,
) -> dict[str, Any]:
    defaults = config.agents.defaults
    providers = _build_provider_payload(config)
    setup_required, setup_message, setup_code, setup_subject = _runtime_setup_status(config, providers)
    resolved_projects_root = projects_root.expanduser().resolve(strict=False)
    raw_workspace = _workspace_payload_value(defaults.workspace, resolved_projects_root)

    return {
        "projects_root": str(resolved_projects_root),
        "config_path": str(config_path),
        "persisted": persisted,
        "runtime": {
            "workspace": raw_workspace,
            "workspace_resolved": str(resolved_projects_root),
            "provider": defaults.provider,
            "model": defaults.model,
            "reasoning_effort": defaults.reasoning_effort,
            "max_tool_iterations": defaults.max_tool_iterations,
            "restrict_to_workspace": config.tools.restrict_to_workspace,
            "setup_required": setup_required,
            "setup_message": setup_message,
            "setup_code": setup_code,
            "setup_subject": setup_subject,
        },
        "providers": providers,
        "provider_proxy": config.providers.proxy,
    }


def _workspace_payload_value(raw_workspace: str, projects_root: Path) -> str:
    """Expose the configured workspace when it resolves to the active projects root."""
    if not isinstance(raw_workspace, str) or not raw_workspace.strip():
        return str(projects_root)

    try:
        configured = Path(raw_workspace).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return str(projects_root)

    if configured == projects_root.expanduser().resolve(strict=False):
        return raw_workspace
    return str(projects_root)


def apply_ui_runtime_update_to_raw_data(
    data: dict[str, Any],
    payload: dict[str, Any],
    *,
    current_projects_root: Path,
) -> tuple[Path, bool]:
    """Patch UI-owned config fields without normalizing unrelated user config."""
    changed = False
    projects_root = current_projects_root.expanduser().resolve()
    agents = _ensure_json_record(data, "agents")
    defaults = _ensure_json_record(agents, "defaults")

    raw_projects_root = payload.get("projects_root")
    if raw_projects_root is not None:
        raw_workspace = str(raw_projects_root)
        projects_root = Path(raw_workspace).expanduser().resolve()
        defaults["workspace"] = raw_workspace
        changed = True

    runtime_payload = payload.get("runtime")
    if isinstance(runtime_payload, dict):
        if "workspace" in runtime_payload:
            raw_workspace = str(runtime_payload["workspace"])
            projects_root = Path(raw_workspace).expanduser().resolve()
            defaults["workspace"] = raw_workspace
            changed = True

        if "provider" in runtime_payload:
            provider = str(runtime_payload["provider"]).strip()
            defaults["provider"] = provider
            changed = True

        provider_for_model = str(defaults.get("provider") or "auto").strip() or "auto"

        if "model" in runtime_payload:
            model = str(runtime_payload["model"]).strip()
            _set_model_preserving_candidates(defaults, model, provider=provider_for_model)
            changed = True

        if "reasoning_effort" in runtime_payload:
            reasoning_effort = runtime_payload["reasoning_effort"]
            value = None if reasoning_effort is None or reasoning_effort == "" else str(reasoning_effort)
            _set_alias_value(defaults, "reasoning_effort", value, alias="reasoningEffort")
            changed = True

        if "max_tool_iterations" in runtime_payload:
            _set_alias_value(
                defaults,
                "max_tool_iterations",
                runtime_payload["max_tool_iterations"],
                alias="maxToolIterations",
            )
            changed = True

        if "restrict_to_workspace" in runtime_payload:
            tools = _ensure_json_record(data, "tools")
            _set_alias_value(
                tools,
                "restrict_to_workspace",
                runtime_payload["restrict_to_workspace"],
                alias="restrictToWorkspace",
            )
            changed = True

    providers_payload = payload.get("providers")
    if isinstance(providers_payload, dict):
        providers = _ensure_json_record(data, "providers")
        for provider_name, provider_update in providers_payload.items():
            if provider_name == "proxy":
                providers["proxy"] = None if provider_update in (None, "") else str(provider_update).strip()
                changed = True
                continue

            if not isinstance(provider_update, dict):
                continue

            provider_cfg = _provider_config_record(providers, str(provider_name))
            if "api_key" in provider_update:
                _set_alias_value(provider_cfg, "api_key", str(provider_update["api_key"]).strip(), alias="apiKey")
                changed = True
            if "api_base" in provider_update:
                api_base = provider_update["api_base"]
                value = None if api_base in (None, "") else str(api_base).strip()
                _set_alias_value(provider_cfg, "api_base", value, alias="apiBase")
                changed = True

    return projects_root, changed


def save_ui_runtime_update(
    config: Config,
    payload: dict[str, Any],
    *,
    current_projects_root: Path,
    config_path: Path,
) -> None:
    """Persist a UI settings update while preserving unrelated raw JSON fields."""
    config_path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(by_alias=True)
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                existing = json.load(f)
            if isinstance(existing, dict):
                data = existing
        except (OSError, json.JSONDecodeError, ValueError):
            data = config.model_dump(by_alias=True)

    apply_ui_runtime_update_to_raw_data(
        data,
        payload,
        current_projects_root=current_projects_root,
    )

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def apply_ui_runtime_update(
    config: Config,
    payload: dict[str, Any],
    *,
    current_projects_root: Path,
) -> tuple[Path, bool]:
    changed = False
    projects_root = current_projects_root.expanduser().resolve()

    # Only flag ``changed`` when a value actually differs from the live config.
    # A no-op save (e.g. the UI re-posting the runtime block while the user only
    # toggled a frontend-only preference) must not trigger an expensive agent
    # ``reconfigure_runtime`` or a config rewrite.
    raw_projects_root = payload.get("projects_root")
    if raw_projects_root is not None:
        if not isinstance(raw_projects_root, str):
            raise ValueError("projects_root must be a string")
        projects_root = Path(raw_projects_root).expanduser().resolve()
        if config.agents.defaults.workspace != raw_projects_root:
            config.agents.defaults.workspace = raw_projects_root
            changed = True

    runtime_payload = payload.get("runtime")
    if runtime_payload is not None:
        if not isinstance(runtime_payload, dict):
            raise ValueError("runtime must be an object")

        if "workspace" in runtime_payload:
            raw_workspace = runtime_payload["workspace"]
            if not isinstance(raw_workspace, str):
                raise ValueError("runtime.workspace must be a string")
            projects_root = Path(raw_workspace).expanduser().resolve()
            if config.agents.defaults.workspace != raw_workspace:
                config.agents.defaults.workspace = raw_workspace
                changed = True

        if "provider" in runtime_payload:
            provider = runtime_payload["provider"]
            if not isinstance(provider, str) or not provider.strip():
                raise ValueError("runtime.provider must be a non-empty string")
            provider = provider.strip()
            if config.agents.defaults.provider != provider:
                config.agents.defaults.provider = provider
                changed = True

        if "model" in runtime_payload:
            model = runtime_payload["model"]
            if not isinstance(model, str) or not model.strip():
                raise ValueError("runtime.model must be a non-empty string")
            model = model.strip()
            if config.agents.defaults.model != model:
                config.agents.defaults.model = model
                changed = True

        if "reasoning_effort" in runtime_payload:
            reasoning_effort = runtime_payload["reasoning_effort"]
            if reasoning_effort is None or reasoning_effort == "":
                next_effort = None
            elif isinstance(reasoning_effort, str) and reasoning_effort in _ALLOWED_REASONING_EFFORTS:
                next_effort = reasoning_effort
            else:
                raise ValueError("runtime.reasoning_effort must be one of: low, medium, high, adaptive")
            if config.agents.defaults.reasoning_effort != next_effort:
                config.agents.defaults.reasoning_effort = next_effort
                changed = True

        if "max_tool_iterations" in runtime_payload:
            max_tool_iterations = runtime_payload["max_tool_iterations"]
            if not isinstance(max_tool_iterations, int) or max_tool_iterations < 1:
                raise ValueError("runtime.max_tool_iterations must be a positive integer")
            if config.agents.defaults.max_tool_iterations != max_tool_iterations:
                config.agents.defaults.max_tool_iterations = max_tool_iterations
                changed = True

        if "restrict_to_workspace" in runtime_payload:
            restrict_to_workspace = runtime_payload["restrict_to_workspace"]
            if not isinstance(restrict_to_workspace, bool):
                raise ValueError("runtime.restrict_to_workspace must be a boolean")
            if config.tools.restrict_to_workspace != restrict_to_workspace:
                config.tools.restrict_to_workspace = restrict_to_workspace
                changed = True

    providers_payload = payload.get("providers")
    if providers_payload is not None:
        if not isinstance(providers_payload, dict):
            raise ValueError("providers must be an object")
        for provider_name, provider_update in providers_payload.items():
            if provider_name == "proxy":
                if provider_update is None or provider_update == "":
                    next_proxy = None
                elif isinstance(provider_update, str):
                    next_proxy = provider_update.strip()
                else:
                    raise ValueError("providers.proxy must be a string or null")
                if config.providers.proxy != next_proxy:
                    config.providers.proxy = next_proxy
                    changed = True
                continue

            if provider_name not in _provider_field_names():
                raise ValueError(f"unsupported provider: {provider_name}")
            if not isinstance(provider_update, dict):
                raise ValueError(f"providers.{provider_name} must be an object")

            provider_cfg = getattr(config.providers, provider_name)
            if "api_key" in provider_update:
                api_key = provider_update["api_key"]
                if not isinstance(api_key, str):
                    raise ValueError(f"providers.{provider_name}.api_key must be a string")
                api_key = api_key.strip()
                if provider_cfg.api_key != api_key:
                    provider_cfg.api_key = api_key
                    changed = True

            if "api_base" in provider_update:
                api_base = provider_update["api_base"]
                if api_base is None or api_base == "":
                    next_api_base = None
                elif isinstance(api_base, str):
                    next_api_base = api_base.strip()
                else:
                    raise ValueError(f"providers.{provider_name}.api_base must be a string or null")
                if provider_cfg.api_base != next_api_base:
                    provider_cfg.api_base = next_api_base
                    changed = True

    return projects_root, changed
