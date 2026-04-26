"""UI-facing runtime config serialization and validation helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mira_engine.config.schema import Config

_ALLOWED_PROVIDER_NAMES = ("anthropic", "openai", "openrouter", "custom", "ollama")
_ALLOWED_REASONING_EFFORTS = {"low", "medium", "high", "adaptive"}


def _mask_secret(value: str) -> str | None:
    text = value.strip()
    if not text:
        return None
    if len(text) <= 6:
        return "*" * len(text)
    return f"{text[:4]}...{text[-2:]}"


def build_ui_runtime_payload(
    config: Config,
    *,
    projects_root: Path,
    config_path: Path,
    persisted: bool,
) -> dict[str, Any]:
    defaults = config.agents.defaults
    providers: dict[str, dict[str, Any]] = {}
    for provider_name in _ALLOWED_PROVIDER_NAMES:
        provider_cfg = getattr(config.providers, provider_name)
        providers[provider_name] = {
            "api_key_configured": bool(provider_cfg.api_key),
            "api_key_preview": _mask_secret(provider_cfg.api_key),
            "api_base": provider_cfg.api_base,
        }

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
        },
        "providers": providers,
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
            if provider_name not in _ALLOWED_PROVIDER_NAMES:
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
