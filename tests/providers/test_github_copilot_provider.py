from __future__ import annotations

from unittest.mock import patch

import pytest
from oauth_cli_kit.models import OAuthToken

import mira_engine.providers.github_copilot_provider as github_provider


def test_github_copilot_storage_prepares_oauth_state(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        github_provider,
        "ensure_oauth_state_dirs_for_runtime",
        lambda: calls.append("prepare"),
    )

    storage = github_provider._storage()

    assert storage is not None
    assert calls == ["prepare"]


def test_extract_copilot_api_base_prefers_token_api_endpoint() -> None:
    payload = {
        "endpoints": {
            "telemetry": "https://telemetry.githubcopilot.com",
            "api": "https://api.individual.githubcopilot.com",
        }
    }

    assert github_provider._extract_copilot_api_base(payload) == "https://api.individual.githubcopilot.com"


def test_extract_copilot_api_base_ignores_non_copilot_hosts() -> None:
    payload = {"endpoints": {"api": "https://api.github.com"}}

    assert github_provider._extract_copilot_api_base(payload) is None


def test_extract_copilot_api_base_trims_openai_compatible_endpoint_paths() -> None:
    payload = {
        "endpoints": {
            "chat": "https://api.individual.githubcopilot.com/v1/chat/completions"
        }
    }

    assert github_provider._extract_copilot_api_base(payload) == "https://api.individual.githubcopilot.com/v1"


def test_extract_copilot_api_base_ignores_telemetry_endpoint() -> None:
    payload = {"endpoints": {"telemetry": "https://telemetry.githubcopilot.com"}}

    assert github_provider._extract_copilot_api_base(payload) is None


def test_github_copilot_provider_normalizes_preview_model_alias() -> None:
    with patch("mira_engine.providers.openai_compat_provider.AsyncOpenAI"):
        provider = github_provider.GitHubCopilotProvider(
            default_model="github_copilot/gemini-3.1-pro-preview"
        )

    kwargs = provider._build_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model="github_copilot/gemini-3.1-pro-preview",
        max_tokens=16,
        temperature=0.1,
        reasoning_effort=None,
        tool_choice=None,
    )

    assert kwargs["model"] == "gemini-3.1-pro"


def test_github_copilot_provider_accepts_configured_api_base_and_proxy() -> None:
    with patch("mira_engine.providers.openai_compat_provider.AsyncOpenAI") as openai_client:
        provider = github_provider.GitHubCopilotProvider(
            default_model="github-copilot/gpt-4.1",
            api_base="api.business.githubcopilot.com",
            proxy="http://127.0.0.1:7890",
        )

    assert provider.api_base == "https://api.business.githubcopilot.com"
    assert provider.proxy == "http://127.0.0.1:7890"
    assert openai_client.call_args.kwargs["http_client"] is provider._openai_http_client


@pytest.mark.asyncio
async def test_github_copilot_token_exchange_updates_api_base_from_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        github_provider,
        "_load_github_token",
        lambda: OAuthToken(access="github-token", refresh="", expires=9999999999999),
    )

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "token": "copilot-token",
                "refresh_in": 1000,
                "endpoints": {"api": "https://api.individual.githubcopilot.com"},
            }

    class _Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers):
            return _Response()

    monkeypatch.setattr(github_provider.httpx, "AsyncClient", _Client)

    with patch("mira_engine.providers.openai_compat_provider.AsyncOpenAI"):
        provider = github_provider.GitHubCopilotProvider(default_model="github-copilot/gpt-4.1")

    token = await provider._get_copilot_access_token()

    assert token == "copilot-token"
    assert provider.api_base == "https://api.individual.githubcopilot.com"
