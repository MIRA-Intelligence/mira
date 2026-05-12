"""UI-facing runtime config serialization and validation helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mira_engine.config.schema import Config, ProvidersConfig
from mira_engine.providers.registry import find_by_name
_ALLOWED_REASONING_EFFORTS = {"low", "medium", "high", "adaptive"}


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

    if provider_name == "custom":
        custom_base = providers_payload.get("custom", {}).get("api_base")
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

    return {
        "projects_root": str(projects_root),
        "config_path": str(config_path),
        "persisted": persisted,
        "runtime": {
            "workspace": str(projects_root),
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


def apply_ui_runtime_update(
    config: Config,
    payload: dict[str, Any],
    *,
    current_projects_root: Path,
) -> tuple[Path, bool]:
    changed = False
    projects_root = current_projects_root.expanduser().resolve()

    raw_projects_root = payload.get("projects_root")
    if raw_projects_root is not None:
        if not isinstance(raw_projects_root, str):
            raise ValueError("projects_root must be a string")
        projects_root = Path(raw_projects_root).expanduser().resolve()
        config.agents.defaults.workspace = str(projects_root)
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
            config.agents.defaults.workspace = str(projects_root)
            changed = True

        if "provider" in runtime_payload:
            provider = runtime_payload["provider"]
            if not isinstance(provider, str) or not provider.strip():
                raise ValueError("runtime.provider must be a non-empty string")
            config.agents.defaults.provider = provider.strip()
            changed = True

        if "model" in runtime_payload:
            model = runtime_payload["model"]
            if not isinstance(model, str) or not model.strip():
                raise ValueError("runtime.model must be a non-empty string")
            config.agents.defaults.model = model.strip()
            changed = True

        if "reasoning_effort" in runtime_payload:
            reasoning_effort = runtime_payload["reasoning_effort"]
            if reasoning_effort is None or reasoning_effort == "":
                config.agents.defaults.reasoning_effort = None
            elif isinstance(reasoning_effort, str) and reasoning_effort in _ALLOWED_REASONING_EFFORTS:
                config.agents.defaults.reasoning_effort = reasoning_effort
            else:
                raise ValueError("runtime.reasoning_effort must be one of: low, medium, high, adaptive")
            changed = True

        if "max_tool_iterations" in runtime_payload:
            max_tool_iterations = runtime_payload["max_tool_iterations"]
            if not isinstance(max_tool_iterations, int) or max_tool_iterations < 1:
                raise ValueError("runtime.max_tool_iterations must be a positive integer")
            config.agents.defaults.max_tool_iterations = max_tool_iterations
            changed = True

        if "restrict_to_workspace" in runtime_payload:
            restrict_to_workspace = runtime_payload["restrict_to_workspace"]
            if not isinstance(restrict_to_workspace, bool):
                raise ValueError("runtime.restrict_to_workspace must be a boolean")
            config.tools.restrict_to_workspace = restrict_to_workspace
            changed = True

    providers_payload = payload.get("providers")
    if providers_payload is not None:
        if not isinstance(providers_payload, dict):
            raise ValueError("providers must be an object")
        for provider_name, provider_update in providers_payload.items():
            if provider_name == "proxy":
                if provider_update is None or provider_update == "":
                    config.providers.proxy = None
                elif isinstance(provider_update, str):
                    config.providers.proxy = provider_update.strip()
                else:
                    raise ValueError("providers.proxy must be a string or null")
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
                provider_cfg.api_key = api_key.strip()
                changed = True

            if "api_base" in provider_update:
                api_base = provider_update["api_base"]
                if api_base is None or api_base == "":
                    provider_cfg.api_base = None
                elif isinstance(api_base, str):
                    provider_cfg.api_base = api_base.strip()
                else:
                    raise ValueError(f"providers.{provider_name}.api_base must be a string or null")
                changed = True

    return projects_root, changed
