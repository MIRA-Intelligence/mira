from __future__ import annotations

import pytest

from mira_engine.config.schema import Config
from mira_engine.providers.factory import make_provider


def test_make_provider_raises_clear_error_when_provider_cannot_be_matched() -> None:
    config = Config()
    config.agents.defaults.model = "unknown-model-name"
    config.agents.defaults.provider = "auto"

    with pytest.raises(ValueError, match="Unable to match provider for model 'unknown-model-name'"):
        make_provider(config)


def test_make_provider_raises_error_for_custom_without_api_base() -> None:
    """Custom provider requires explicit apiBase configuration."""
    config = Config()
    config.agents.defaults.model = "custom/my-model"
    config.agents.defaults.provider = "custom"
    config.providers.custom.api_key = "test-key"
    # Intentionally not setting api_base

    with pytest.raises(ValueError, match="Custom provider requires.*apiBase"):
        make_provider(config)


async def test_make_provider_uses_bundle_setup_placeholder_without_network_call() -> None:
    """The bundle setup sentinel keeps the gateway alive but fails model calls clearly."""
    config = Config()
    config.agents.defaults.model = "custom/mira-ui-bundle-setup"
    config.agents.defaults.provider = "custom"
    config.providers.custom.api_base = "http://127.0.0.1:9/v1"

    provider = make_provider(config)
    response = await provider.chat(messages=[{"role": "user", "content": "hello"}])

    assert provider.get_default_model() == "custom/mira-ui-bundle-setup"
    assert response.finish_reason == "error"
    assert "Bundle runtime provider is not configured" in (response.content or "")


def test_make_provider_succeeds_for_custom_with_api_base() -> None:
    """Custom provider works when apiBase is configured."""
    config = Config()
    config.agents.defaults.model = "custom/my-model"
    config.agents.defaults.provider = "custom"
    config.providers.custom.api_key = "test-key"
    config.providers.custom.api_base = "http://localhost:8000/v1"

    # Should not raise
    provider = make_provider(config)
    assert provider is not None


def test_make_provider_passes_provider_proxy_to_openai_codex() -> None:
    """OpenAI Codex provider uses providers.proxy for LLM HTTP calls."""
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "openai_codex",
                    "model": "openai-codex/gpt-5.3-codex",
                }
            },
            "providers": {"proxy": "http://127.0.0.1:7890"},
            "tools": {"web": {"proxy": "http://127.0.0.1:9999"}},
        }
    )

    provider = make_provider(config)

    assert provider.__class__.__name__ == "OpenAICodexProvider"
    assert provider.proxy == "http://127.0.0.1:7890"


def test_make_provider_falls_back_to_web_proxy_for_openai_codex() -> None:
    """Existing tools.web.proxy configs continue to work until migrated."""
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "openai_codex",
                    "model": "openai-codex/gpt-5.3-codex",
                }
            },
            "tools": {"web": {"proxy": "http://127.0.0.1:7890"}},
        }
    )

    provider = make_provider(config)

    assert provider.__class__.__name__ == "OpenAICodexProvider"
    assert provider.proxy == "http://127.0.0.1:7890"


def test_make_provider_passes_provider_proxy_to_github_copilot() -> None:
    """GitHub Copilot provider uses providers.proxy for token exchange calls."""
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "github_copilot",
                    "model": "github_copilot/gemini-3.1-pro-preview",
                }
            },
            "providers": {"proxy": "http://127.0.0.1:7890"},
            "tools": {"web": {"proxy": "http://127.0.0.1:9999"}},
        }
    )

    provider = make_provider(config)

    assert provider.__class__.__name__ == "GitHubCopilotProvider"
    assert provider.proxy == "http://127.0.0.1:7890"


def test_make_provider_routes_deepseek_through_openai_compat() -> None:
    """DeepSeek bypasses LiteLLM to dodge the thinking-mode reasoning_content bug."""
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "deepseek",
                    "model": "deepseek/deepseek-v4-pro",
                }
            },
            "providers": {"deepseek": {"apiKey": "sk-deepseek-test"}},
        }
    )

    provider = make_provider(config)

    assert provider.__class__.__name__ == "OpenAICompatProvider"
    assert provider.get_default_model() == "deepseek/deepseek-v4-pro"
    assert provider._effective_base == "https://api.deepseek.com/v1"


def test_make_provider_routes_deepseek_with_custom_api_base() -> None:
    """User-provided api_base wins over the registry default."""
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "deepseek",
                    "model": "deepseek/deepseek-chat",
                }
            },
            "providers": {
                "deepseek": {
                    "apiKey": "sk-deepseek-test",
                    "apiBase": "https://deepseek.proxy.example/v1",
                }
            },
        }
    )

    provider = make_provider(config)

    assert provider.__class__.__name__ == "OpenAICompatProvider"
    assert provider._effective_base == "https://deepseek.proxy.example/v1"


def test_make_provider_routes_nvidia_through_direct_provider() -> None:
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "nvidia",
                    "model": "nvidia/nvidia/llama-3.3-nemotron-super-49b-v1.5",
                }
            },
            "providers": {"nvidia": {"apiKey": "nvapi-test-key"}},
        }
    )

    provider = make_provider(config)

    assert provider.__class__.__name__ == "NvidiaProvider"
    assert provider.get_default_model() == "nvidia/nvidia/llama-3.3-nemotron-super-49b-v1.5"
    assert provider.api_base == "https://inference-api.nvidia.com/v1"


def test_make_provider_routes_nvidia_with_custom_api_base() -> None:
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "nvidia",
                    "model": "nvidia/deepseek-ai/deepseek-v4-pro",
                }
            },
            "providers": {
                "nvidia": {
                    "apiKey": "nvapi-test-key",
                    "apiBase": "https://proxy.example/v1/chat/completions",
                }
            },
        }
    )

    provider = make_provider(config)

    assert provider.__class__.__name__ == "NvidiaProvider"
    assert provider.api_base == "https://proxy.example/v1"
