from __future__ import annotations

from mira_engine.cli.models import get_model_context_limit, get_model_suggestions
from mira_engine.providers.model_fetch import ModelCache, ModelInfo, write_model_cache


def test_model_suggestions_use_cached_provider_models(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("mira_engine.providers.model_fetch.get_config_path", lambda: config_path)
    write_model_cache(
        ModelCache(
            provider="github_copilot",
            fetched_at="2026-05-13T12:00:00Z",
            models=[
                ModelInfo(
                    id="gemini-3.1-pro",
                    config_model="github_copilot/gemini-3.1-pro",
                    context_window_tokens=1_000_000,
                )
            ],
        ),
        config_path,
    )

    assert get_model_suggestions("gemini", provider="github_copilot") == [
        "github_copilot/gemini-3.1-pro"
    ]
    assert get_model_context_limit("github_copilot/gemini-3.1-pro", "github_copilot") == 1_000_000
