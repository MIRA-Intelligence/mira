"""Provider factory helpers for creating model-specific providers."""

from __future__ import annotations

from typing import Any

from mira_engine.config.schema import Config, primary_model_candidate
from mira_engine.providers.base import LLMProvider, LLMResponse

_BUNDLE_SETUP_MODEL = "custom/mira-ui-bundle-setup"
_BUNDLE_SETUP_API_BASE = "http://127.0.0.1:9/v1"
_BUNDLE_SETUP_MESSAGE = (
    "Bundle runtime provider is not configured. Open Settings > Local Runtime Config "
    "and choose a provider before retrying."
)


class BundleSetupRequiredProvider(LLMProvider):
    """Placeholder provider that keeps the bundle gateway alive until UI setup."""

    def __init__(self, default_model: str = _BUNDLE_SETUP_MODEL):
        self.default_model = default_model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        return LLMResponse(content=_BUNDLE_SETUP_MESSAGE, finish_reason="error")

    def get_default_model(self) -> str:
        return self.default_model


def resolve_provider_proxy(config: Config) -> str | None:
    """Resolve the global LLM provider proxy, with web proxy as legacy fallback."""
    return config.providers.proxy or config.tools.web.proxy or None


def make_provider(config: Config, model: str | None = None) -> LLMProvider:
    """Create the appropriate provider for the given model."""
    from mira_engine.providers.azure_openai_provider import AzureOpenAIProvider
    from mira_engine.providers.github_copilot_provider import GitHubCopilotProvider
    from mira_engine.providers.litellm_provider import LiteLLMProvider
    from mira_engine.providers.nvidia_provider import NvidiaProvider
    from mira_engine.providers.openai_codex_provider import OpenAICodexProvider
    from mira_engine.providers.openai_compat_provider import OpenAICompatProvider
    from mira_engine.providers.registry import find_by_name

    resolved_model = primary_model_candidate(model, config.agents.defaults.primary_model)
    if not resolved_model:
        raise ValueError("No model configured. Set agents.defaults.model in config.json.")
    provider_name = config.get_provider_name(resolved_model)
    provider_config = config.get_provider(resolved_model)
    if not provider_name:
        raise ValueError(
            f"Unable to match provider for model '{resolved_model}'. "
            "Set agents.defaults.provider explicitly in config.json."
        )

    if provider_name == "openai_codex" or resolved_model.startswith("openai-codex/"):
        return OpenAICodexProvider(
            default_model=resolved_model,
            proxy=resolve_provider_proxy(config),
        )
    if provider_name == "github_copilot" or resolved_model.startswith("github-copilot/"):
        return GitHubCopilotProvider(default_model=resolved_model)

    if provider_name == "custom":
        api_base = config.get_api_base(resolved_model)
        normalized_base = api_base.rstrip("/") if isinstance(api_base, str) else ""
        if resolved_model == _BUNDLE_SETUP_MODEL or normalized_base == _BUNDLE_SETUP_API_BASE.rstrip("/"):
            return BundleSetupRequiredProvider(default_model=resolved_model)
        # Require explicit apiBase configuration for custom provider
        if not api_base:
            raise ValueError(
                "Custom provider requires 'providers.custom.apiBase' to be configured. "
                "Please set the API base URL (e.g., 'http://localhost:8000/v1' or 'https://api.example.com/v1') "
                "in your config.json, or run 'mira onboard --wizard' to configure it interactively."
            )
        return OpenAICompatProvider(
            api_key=provider_config.api_key if provider_config else "no-key",
            api_base=api_base,
            default_model=resolved_model,
            extra_headers=provider_config.extra_headers if provider_config else None,
            spec=find_by_name("custom"),
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

    if provider_name == "nvidia":
        nvidia_spec = spec or find_by_name("nvidia")
        api_base = config.get_api_base(resolved_model)
        if not api_base and nvidia_spec:
            api_base = nvidia_spec.default_api_base or None
        return NvidiaProvider(
            api_key=provider_config.api_key if provider_config else None,
            api_base=api_base,
            default_model=resolved_model,
            extra_headers=provider_config.extra_headers if provider_config else None,
        )

    # Native DeepSeek path — bypass LiteLLM to avoid the thinking-mode
    # reasoning_content round-trip bug (litellm#26395). OpenAICompatProvider
    # already preserves reasoning_content across turns; the spec carries the
    # default api_base and model-name stripping so the OpenAI SDK can hit
    # DeepSeek's OpenAI-compatible endpoint directly.
    if provider_name == "deepseek" or resolved_model.startswith("deepseek/"):
        deepseek_spec = spec or find_by_name("deepseek")
        api_base = config.get_api_base(resolved_model)
        if not api_base and deepseek_spec:
            api_base = deepseek_spec.default_api_base or None
        return OpenAICompatProvider(
            api_key=provider_config.api_key if provider_config else None,
            api_base=api_base,
            default_model=resolved_model,
            extra_headers=provider_config.extra_headers if provider_config else None,
            spec=deepseek_spec,
        )

    return LiteLLMProvider(
        api_key=provider_config.api_key if provider_config else None,
        api_base=config.get_api_base(resolved_model),
        default_model=resolved_model,
        extra_headers=provider_config.extra_headers if provider_config else None,
        provider_name=provider_name,
    )
