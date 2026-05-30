from typing import Any

from mira_engine.agent.routing import ModelRouter, RoutedModel, RoutedProviderManager
from mira_engine.config.schema import AgentDefaults, Config
from mira_engine.providers.base import LLMProvider, LLMResponse, ToolCallRequest


def _defaults(**overrides) -> AgentDefaults:
    base = {
        "model": "anthropic/claude-opus-4-5",
        "route_model": None,
        "small_model": "openai/gpt-4.1-mini",
        "medium_model": "anthropic/claude-sonnet-4-5",
        "large_model": "anthropic/claude-opus-4-5",
        "route_by_complexity": True,
    }
    base.update(overrides)
    return AgentDefaults(**base)


def test_router_falls_back_to_default_model_when_disabled() -> None:
    router = ModelRouter(_defaults(route_by_complexity=False))

    route = router.default_route()

    assert route.tier == "default"
    assert route.model == "anthropic/claude-opus-4-5"
    assert route.candidates == ("anthropic/claude-opus-4-5",)


def test_config_accepts_model_candidate_lists() -> None:
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "model": ["anthropic/claude-opus-4-5", "openai/gpt-4.1"],
                    "smallModel": ["openai/gpt-4.1-mini", "openai/gpt-4.1-nano"],
                    "mediumModel": ["anthropic/claude-sonnet-4-5", "openai/gpt-4.1"],
                    "largeModel": "anthropic/claude-opus-4-5",
                }
            }
        }
    )

    assert config.agents.defaults.primary_model == "anthropic/claude-opus-4-5"
    assert config.agents.defaults.default_model_candidates == [
        "anthropic/claude-opus-4-5",
        "openai/gpt-4.1",
    ]
    assert config.agents.defaults.tier_model_candidates("small") == [
        "openai/gpt-4.1-mini",
        "openai/gpt-4.1-nano",
    ]


class _FakeProvider(LLMProvider):
    def __init__(self, route_tier: str | None = None):
        super().__init__()
        self.route_tier = route_tier

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        if self.route_tier and tools:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="route-1",
                        name="route_complexity",
                        arguments={"tier": self.route_tier, "reason": "instinct"},
                    )
                ],
            )
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "anthropic/claude-opus-4-5"


async def test_instinct_router_uses_small_model_judgment() -> None:
    defaults = _defaults()
    router = ModelRouter(defaults)
    manager = RoutedProviderManager(
        default_provider=_FakeProvider(),
        default_model=defaults.primary_model,
        router=router,
        provider_factory=lambda model: _FakeProvider("large") if model == defaults.small_model else _FakeProvider(),
    )

    _, route = await manager.resolve([{"role": "user", "content": "hello"}], iteration=1)

    assert route.tier == "large"
    assert route.model == defaults.primary_model_for_tier("large")
    assert route.candidates == tuple(defaults.tier_model_candidates("large"))
    assert route.source == "instinct"


async def test_instinct_router_uses_route_model_when_configured() -> None:
    defaults = _defaults(route_model="openai/gpt-4.1-nano")
    router = ModelRouter(defaults)
    manager = RoutedProviderManager(
        default_provider=_FakeProvider(),
        default_model=defaults.primary_model,
        router=router,
        provider_factory=lambda model: _FakeProvider("medium") if model == defaults.route_model else _FakeProvider(),
    )

    _, route = await manager.resolve([{"role": "user", "content": "hello"}], iteration=1)

    assert route.tier == "medium"
    assert route.model == defaults.primary_model_for_tier("medium")
    assert route.source == "instinct"


class _BrokenProvider(LLMProvider):
    def __init__(self):
        super().__init__()
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
        raise RuntimeError("router failed")

    def get_default_model(self) -> str:
        return "anthropic/claude-opus-4-5"


async def test_instinct_router_falls_back_to_default_model_on_error() -> None:
    defaults = _defaults()
    router = ModelRouter(defaults)
    manager = RoutedProviderManager(
        default_provider=_FakeProvider(),
        default_model=defaults.primary_model,
        router=router,
        provider_factory=lambda model: _BrokenProvider() if model == defaults.small_model else _FakeProvider(),
    )

    _, route = await manager.resolve([{"role": "user", "content": "hello"}], iteration=1)

    assert route.tier == "default"
    assert route.model == defaults.primary_model
    assert route.source == "fallback"


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
        router=None,
        provider_factory=lambda model: _RetryableErrorProvider("Model overloaded")
        if model == "openai/gpt-4.1-mini"
        else _FakeProvider(),
    )

    response, resolved_route = await manager.chat(
        route=RoutedModel(
            tier="small",
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
        router=None,
        provider_factory=lambda model: _RetryableErrorProvider("400 bad request: unsupported parameter")
        if model == "openai/gpt-4.1-mini"
        else _FakeProvider(),
    )

    response, resolved_route = await manager.chat(
        route=RoutedModel(
            tier="small",
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
        router=None,
        provider_factory=lambda model: providers[model],
    )
    route = RoutedModel(
        tier="small",
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


async def test_routing_prefers_recently_successful_routing_model() -> None:
    defaults = _defaults(route_model=["openai/gpt-4.1-mini", "openai/gpt-4.1-nano"])
    broken = _BrokenProvider()
    fallback = _FakeProvider("medium")
    providers = {
        "openai/gpt-4.1-mini": broken,
        "openai/gpt-4.1-nano": fallback,
    }
    manager = RoutedProviderManager(
        default_provider=_FakeProvider(),
        default_model=defaults.primary_model,
        router=ModelRouter(defaults),
        provider_factory=lambda model: providers.get(model, _FakeProvider()),
    )

    _, first_route = await manager.resolve([{"role": "user", "content": "hello"}], iteration=1)
    _, second_route = await manager.resolve([{"role": "user", "content": "hello again"}], iteration=1)

    assert first_route.model == defaults.primary_model_for_tier("medium")
    assert second_route.model == defaults.primary_model_for_tier("medium")
    assert broken.calls == 1


async def test_chat_reports_error_when_all_candidate_models_fail() -> None:
    manager = RoutedProviderManager(
        default_provider=_FakeProvider(),
        default_model="anthropic/claude-opus-4-5",
        router=None,
        provider_factory=lambda model: _RetryableErrorProvider("503 service unavailable"),
    )

    response, resolved_route = await manager.chat(
        route=RoutedModel(
            tier="small",
            model="openai/gpt-4.1-mini",
            candidates=("openai/gpt-4.1-mini", "openai/gpt-4.1-nano"),
            source="test",
        ),
        messages=[{"role": "user", "content": "hello"}],
    )

    assert response.finish_reason == "error"
    assert "All candidate models failed for this turn" in (response.content or "")
    assert resolved_route.model == "openai/gpt-4.1-nano"
