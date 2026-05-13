from __future__ import annotations

import pytest

from mira_engine.config.schema import Config
from mira_engine.providers import model_fetch
from mira_engine.providers.model_fetch import (
    ModelCache,
    ModelInfo,
    config_model_for_provider,
    fetch_provider_models,
    read_model_cache,
    write_model_cache,
)


def test_model_cache_roundtrip(tmp_path) -> None:
    cache = ModelCache(
        provider="github_copilot",
        api_base="https://api.githubcopilot.com",
        account_id="octocat",
        fetched_at="2026-05-13T12:00:00Z",
        models=[
            ModelInfo(
                id="gemini-3.1-pro",
                display_name="Gemini 3.1 Pro",
                config_model="github_copilot/gemini-3.1-pro",
                context_window_tokens=1_000_000,
                capabilities=["chat"],
            )
        ],
    )

    path = write_model_cache(cache, tmp_path / "config.json")
    loaded = read_model_cache("github_copilot", tmp_path / "config.json")

    assert path.name == "github_copilot.json"
    assert loaded is not None
    assert loaded.provider == "github_copilot"
    assert loaded.models[0].config_model == "github_copilot/gemini-3.1-pro"
    assert loaded.models[0].context_window_tokens == 1_000_000


def test_config_model_for_provider_prefixes_oauth_provider() -> None:
    assert (
        config_model_for_provider("gemini-3.1-pro", "github_copilot")
        == "github_copilot/gemini-3.1-pro"
    )


def test_config_model_for_provider_keeps_openrouter_model_id() -> None:
    assert config_model_for_provider("openai/gpt-4o", "openrouter") == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_fetch_openai_compatible_custom_models_without_api_key(monkeypatch) -> None:
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "custom", "model": "test-model"}},
            "providers": {"custom": {"apiBase": "http://localhost:8000/v1"}},
        }
    )

    async def fake_get_json(url: str, headers: dict[str, str], proxy: str | None = None):
        assert url == "http://localhost:8000/v1/models"
        assert "Authorization" not in headers
        return {"data": [{"id": "custom-model", "context_length": 32768}]}

    monkeypatch.setattr(model_fetch, "_get_json", fake_get_json)

    cache = await fetch_provider_models(config, "custom")

    assert cache.provider == "custom"
    assert cache.models[0].config_model == "custom-model"
    assert cache.models[0].context_window_tokens == 32768


@pytest.mark.asyncio
async def test_fetch_copilot_models_keeps_only_picker_chat_completions(monkeypatch) -> None:
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "github_copilot",
                    "model": "github_copilot/gpt-4.1",
                }
            }
        }
    )

    class _Provider:
        api_base = "https://api.individual.githubcopilot.com"
        proxy = None
        extra_headers = {}

        def __init__(self, *args, **kwargs):
            pass

        async def _get_copilot_access_token(self):
            return "copilot-token"

    async def fake_get_json(url: str, headers: dict[str, str], proxy: str | None = None):
        assert url == "https://api.individual.githubcopilot.com/models"
        return {
            "data": [
                {
                    "id": "gemini-3.1-pro-preview",
                    "name": "Gemini 3.1 Pro",
                    "model_picker_enabled": True,
                    "supported_endpoints": ["/chat/completions"],
                    "capabilities": {
                        "type": "chat",
                        "limits": {"max_context_window_tokens": 128000},
                    },
                },
                {
                    "id": "claude-opus-4.7",
                    "name": "Claude Opus 4.7",
                    "model_picker_enabled": True,
                    "policy": {"state": "disabled"},
                    "supported_endpoints": ["/chat/completions"],
                    "capabilities": {"type": "chat"},
                },
                {
                    "id": "text-embedding-3-small",
                    "model_picker_enabled": False,
                    "capabilities": {"type": "embeddings"},
                },
                {
                    "id": "gpt-5.5",
                    "model_picker_enabled": True,
                    "supported_endpoints": ["/responses"],
                    "capabilities": {"type": "chat"},
                },
            ]
        }

    monkeypatch.setattr(
        "mira_engine.providers.github_copilot_provider.GitHubCopilotProvider",
        _Provider,
    )
    monkeypatch.setattr(
        "mira_engine.providers.github_copilot_provider.get_github_copilot_login_status",
        lambda: None,
    )
    monkeypatch.setattr(model_fetch, "_get_json", fake_get_json)

    cache = await fetch_provider_models(config, "github_copilot")

    assert [model.config_model for model in cache.models] == [
        "github_copilot/gemini-3.1-pro-preview"
    ]
    assert cache.models[0].context_window_tokens == 128000


@pytest.mark.asyncio
async def test_fetch_ollama_models_uses_tags_endpoint(monkeypatch) -> None:
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "ollama", "model": "ollama/llama3.2"}},
            "providers": {"ollama": {"apiBase": "http://localhost:11434/v1"}},
        }
    )

    async def fake_get_json(url: str, headers: dict[str, str], proxy: str | None = None):
        assert url == "http://localhost:11434/api/tags"
        return {"models": [{"model": "llama3.2"}]}

    monkeypatch.setattr(model_fetch, "_get_json", fake_get_json)

    cache = await fetch_provider_models(config, "ollama")

    assert cache.provider == "ollama"
    assert cache.models[0].config_model == "ollama/llama3.2"
