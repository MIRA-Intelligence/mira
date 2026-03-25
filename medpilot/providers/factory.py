"""Provider factory helpers for creating model-specific providers."""

from __future__ import annotations

from medpilot.config.schema import Config
from medpilot.providers.base import LLMProvider


def make_provider(config: Config, model: str | None = None) -> LLMProvider:
    """Create the appropriate provider for the given model."""
    from medpilot.providers.azure_openai_provider import AzureOpenAIProvider
    from medpilot.providers.custom_provider import CustomProvider
    from medpilot.providers.litellm_provider import LiteLLMProvider
    from medpilot.providers.openai_codex_provider import OpenAICodexProvider
    from medpilot.providers.registry import find_by_name

    resolved_model = model or config.agents.defaults.model
    provider_name = config.get_provider_name(resolved_model)
    provider_config = config.get_provider(resolved_model)

    if provider_name == "openai_codex" or resolved_model.startswith("openai-codex/"):
        return OpenAICodexProvider(default_model=resolved_model)

    if provider_name == "custom":
        return CustomProvider(
            api_key=provider_config.api_key if provider_config else "no-key",
            api_base=config.get_api_base(resolved_model) or "http://localhost:8000/v1",
            default_model=resolved_model,
        )

    if provider_name == "azure_openai":
        if not provider_config or not provider_config.api_key or not provider_config.api_base:
            raise ValueError(
                "Azure OpenAI requires providers.azureOpenai.apiKey and providers.azureOpenai.apiBase."
            )
        return AzureOpenAIProvider(
            api_key=provider_config.api_key,
            api_base=provider_config.api_base,
            default_model=resolved_model,
        )

    spec = find_by_name(provider_name)
    if not resolved_model.startswith("bedrock/") and not (provider_config and provider_config.api_key) and not (spec and spec.is_oauth):
        raise ValueError(
            f"No API key configured for model '{resolved_model}'. Set it under providers in config.json."
        )

    return LiteLLMProvider(
        api_key=provider_config.api_key if provider_config else None,
        api_base=config.get_api_base(resolved_model),
        default_model=resolved_model,
        extra_headers=provider_config.extra_headers if provider_config else None,
        provider_name=provider_name,
    )
