"""Provider package with lazy imports to keep optional deps optional."""

from __future__ import annotations

from medpilot.providers.base import LLMProvider, LLMResponse

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "AnthropicProvider",
    "OpenAICompatProvider",
    "OpenAICodexProvider",
    "GitHubCopilotProvider",
    "AzureOpenAIProvider",
]


def __getattr__(name: str):
    if name == "AnthropicProvider":
        from medpilot.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider
    if name == "OpenAICompatProvider":
        from medpilot.providers.openai_compat_provider import OpenAICompatProvider

        return OpenAICompatProvider
    if name == "OpenAICodexProvider":
        from medpilot.providers.openai_codex_provider import OpenAICodexProvider

        return OpenAICodexProvider
    if name == "GitHubCopilotProvider":
        from medpilot.providers.github_copilot_provider import GitHubCopilotProvider

        return GitHubCopilotProvider
    if name == "AzureOpenAIProvider":
        from medpilot.providers.azure_openai_provider import AzureOpenAIProvider

        return AzureOpenAIProvider
    raise AttributeError(f"module 'medpilot.providers' has no attribute {name!r}")
