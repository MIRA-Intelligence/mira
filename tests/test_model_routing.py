from typing import Any

from mira_engine.agent.routing import RoutedModel, RoutedProviderManager
from mira_engine.config.schema import Config
from mira_engine.providers.base import LLMProvider, LLMResponse


class _FakeProvider(LLMProvider):
    def __init__(self, stream_deltas: list[str] | None = None):
        super().__init__()
        self.stream_deltas = list(stream_deltas or [])
        self.chat_calls = 0
        self.stream_calls = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        self.chat_calls += 1
        return LLMResponse(content="ok")

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta=None,
    ) -> LLMResponse:
        self.stream_calls += 1
        for delta in self.stream_deltas:
            if on_content_delta is not None:
                await on_content_delta(delta)
        return LLMResponse(content="".join(self.stream_deltas) or "ok")

    def get_default_model(self) -> str:
        return "anthropic/claude-opus-4-5"


def test_resolve_returns_default_model_with_candidates() -> None:
    manager = RoutedProviderManager(
        default_provider=_FakeProvider(),
        default_model="anthropic/claude-opus-4-5",
        default_candidates=("anthropic/claude-opus-4-5", "openai/gpt-4.1"),
    )

    async def _run():
        return await manager.resolve([{"role": "user", "content": "hi"}], iteration=1)

    import asyncio

    provider, route = asyncio.run(_run())
    assert route.tier == "default"
    assert route.model == "anthropic/claude-opus-4-5"
    assert route.candidates == ("anthropic/claude-opus-4-5", "openai/gpt-4.1")


def test_config_accepts_model_candidate_lists() -> None:
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "model": ["anthropic/claude-opus-4-5", "openai/gpt-4.1"],
                }
            }
        }
    )

    assert config.agents.defaults.primary_model == "anthropic/claude-opus-4-5"
    assert config.agents.defaults.default_model_candidates == [
        "anthropic/claude-opus-4-5",
        "openai/gpt-4.1",
    ]


async def test_routed_provider_manager_streams_selected_model() -> None:
    provider = _FakeProvider(stream_deltas=["Hel", "lo"])
    route = RoutedModel(
        tier="default",
        model="anthropic/claude-opus-4-5",
        candidates=("anthropic/claude-opus-4-5",),
        source="default",
    )
    manager = RoutedProviderManager(
        default_provider=provider,
        default_model="anthropic/claude-opus-4-5",
    )
    deltas: list[str] = []

    async def _on_delta(delta: str) -> None:
        deltas.append(delta)

    response, selected = await manager.chat_stream_with_retry(
        route,
        messages=[{"role": "user", "content": "hello"}],
        on_content_delta=_on_delta,
    )

    assert response.content == "Hello"
    assert selected.model == "anthropic/claude-opus-4-5"
    assert deltas == ["Hel", "lo"]
    assert provider.stream_calls == 1
    assert provider.chat_calls == 0


class _RetryableErrorProvider(LLMProvider):
    def __init__(self, text: str):
        super().__init__()
        self.text = text
        self.calls = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content=self.text, finish_reason="error")

    def get_default_model(self) -> str:
        return "anthropic/claude-opus-4-5"


async def test_chat_falls_back_to_secondary_model_on_retryable_error() -> None:
    manager = RoutedProviderManager(
        default_provider=_FakeProvider(),
        default_model="anthropic/claude-opus-4-5",
        provider_factory=lambda model: _RetryableErrorProvider("Model overloaded")
        if model == "openai/gpt-4.1-mini"
        else _FakeProvider(),
    )

    response, resolved_route = await manager.chat(
        route=RoutedModel(
            tier="default",
            model="openai/gpt-4.1-mini",
            candidates=("openai/gpt-4.1-mini", "openai/gpt-4.1-nano"),
            source="test",
        ),
        messages=[{"role": "user", "content": "hello"}],
    )

    assert response.content == "ok"
    assert resolved_route.model == "openai/gpt-4.1-nano"
    assert resolved_route.candidates == ("openai/gpt-4.1-mini", "openai/gpt-4.1-nano")


async def test_chat_does_not_fallback_on_non_retryable_error() -> None:
    manager = RoutedProviderManager(
        default_provider=_FakeProvider(),
        default_model="anthropic/claude-opus-4-5",
        provider_factory=lambda model: _RetryableErrorProvider("400 bad request: unsupported parameter")
        if model == "openai/gpt-4.1-mini"
        else _FakeProvider(),
    )

    response, resolved_route = await manager.chat(
        route=RoutedModel(
            tier="default",
            model="openai/gpt-4.1-mini",
            candidates=("openai/gpt-4.1-mini", "openai/gpt-4.1-nano"),
            source="test",
        ),
        messages=[{"role": "user", "content": "hello"}],
    )

    assert response.finish_reason == "error"
    assert resolved_route.model == "openai/gpt-4.1-mini"
    assert resolved_route.candidates == ("openai/gpt-4.1-mini", "openai/gpt-4.1-nano")


async def test_chat_prefers_recently_successful_model_next_turn() -> None:
    flaky = _RetryableErrorProvider("503 service unavailable")
    healthy = _FakeProvider()
    providers = {
        "openai/gpt-4.1-mini": flaky,
        "openai/gpt-4.1-nano": healthy,
    }
    manager = RoutedProviderManager(
        default_provider=_FakeProvider(),
        default_model="anthropic/claude-opus-4-5",
        provider_factory=lambda model: providers[model],
    )
    route = RoutedModel(
        tier="default",
        model="openai/gpt-4.1-mini",
        candidates=("openai/gpt-4.1-mini", "openai/gpt-4.1-nano"),
        source="test",
    )

    first_response, first_route = await manager.chat(route=route, messages=[{"role": "user", "content": "hello"}])
    second_response, second_route = await manager.chat(route=route, messages=[{"role": "user", "content": "hello again"}])

    assert first_response.content == "ok"
    assert first_route.model == "openai/gpt-4.1-nano"
    assert second_response.content == "ok"
    assert second_route.model == "openai/gpt-4.1-nano"
    assert flaky.calls == 1


async def test_chat_reports_error_when_all_candidate_models_fail() -> None:
    manager = RoutedProviderManager(
        default_provider=_FakeProvider(),
        default_model="anthropic/claude-opus-4-5",
        provider_factory=lambda model: _RetryableErrorProvider("503 service unavailable"),
    )

    response, resolved_route = await manager.chat(
        route=RoutedModel(
            tier="default",
            model="openai/gpt-4.1-mini",
            candidates=("openai/gpt-4.1-mini", "openai/gpt-4.1-nano"),
            source="test",
        ),
        messages=[{"role": "user", "content": "hello"}],
    )

    assert response.finish_reason == "error"
    assert "All candidate models failed for this turn" in (response.content or "")
    assert resolved_route.model == "openai/gpt-4.1-nano"
