from __future__ import annotations

from medpilot.config.schema import Config


def test_match_provider_respects_forced_provider() -> None:
    cfg = Config()
    cfg.agents.defaults.provider = "anthropic"
    cfg.providers.anthropic.api_key = "k-anthropic"
    provider, name = cfg._match_provider("openai/gpt-4o")
    assert name == "anthropic"
    assert provider is cfg.providers.anthropic


def test_match_provider_returns_none_when_forced_missing() -> None:
    cfg = Config()
    cfg.agents.defaults.provider = "nonexistent"
    provider, name = cfg._match_provider("openai/gpt-4o")
    assert provider is None
    assert name is None


def test_match_provider_prefix_wins_for_oauth_provider() -> None:
    cfg = Config()
    cfg.agents.defaults.provider = "auto"
    provider, name = cfg._match_provider("github-copilot/claude-sonnet")
    assert name == "github_copilot"
    assert provider is cfg.providers.github_copilot


def test_match_provider_by_keyword_with_api_key() -> None:
    cfg = Config()
    cfg.agents.defaults.provider = "auto"
    cfg.providers.deepseek.api_key = "k-deepseek"
    provider, name = cfg._match_provider("deepseek-chat")
    assert name == "deepseek"
    assert provider is cfg.providers.deepseek


def test_match_provider_fallback_to_first_available_key() -> None:
    cfg = Config()
    cfg.agents.defaults.provider = "auto"
    cfg.providers.openrouter.api_key = "sk-or-1"
    provider, name = cfg._match_provider("unknown-model")
    assert name == "openrouter"
    assert provider is cfg.providers.openrouter


def test_get_api_base_prefers_explicit_then_gateway_default() -> None:
    cfg = Config()
    cfg.agents.defaults.provider = "auto"
    cfg.providers.openrouter.api_key = "sk-or-1"

    # Default gateway base from provider registry.
    assert cfg.get_api_base("unknown-model") == "https://openrouter.ai/api/v1"

    # Explicit config base overrides default.
    cfg.providers.openrouter.api_base = "https://proxy.example/v1"
    assert cfg.get_api_base("unknown-model") == "https://proxy.example/v1"


def test_provider_helpers_return_expected_fields() -> None:
    cfg = Config()
    cfg.providers.deepseek.api_key = "k-deepseek"
    assert cfg.get_provider_name("deepseek-chat") == "deepseek"
    assert cfg.get_api_key("deepseek-chat") == "k-deepseek"
