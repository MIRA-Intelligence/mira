from typing import Any

from radiologybot.agent.routing import ModelRouter, RoutedProviderManager
from radiologybot.config.schema import AgentDefaults
from radiologybot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


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
        default_model=defaults.model,
        router=router,
        provider_factory=lambda model: _FakeProvider("large") if model == defaults.small_model else _FakeProvider(),
    )

    _, route = await manager.resolve([{"role": "user", "content": "hello"}], iteration=1)

    assert route.tier == "large"
    assert route.model == defaults.large_model
    assert route.source == "instinct"


async def test_instinct_router_uses_route_model_when_configured() -> None:
    defaults = _defaults(route_model="openai/gpt-4.1-nano")
    router = ModelRouter(defaults)
    manager = RoutedProviderManager(
        default_provider=_FakeProvider(),
        default_model=defaults.model,
        router=router,
        provider_factory=lambda model: _FakeProvider("medium") if model == defaults.route_model else _FakeProvider(),
    )

    _, route = await manager.resolve([{"role": "user", "content": "hello"}], iteration=1)

    assert route.tier == "medium"
    assert route.model == defaults.medium_model
    assert route.source == "instinct"


class _BrokenProvider(LLMProvider):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        raise RuntimeError("router failed")

    def get_default_model(self) -> str:
        return "anthropic/claude-opus-4-5"


async def test_instinct_router_falls_back_to_default_model_on_error() -> None:
    defaults = _defaults()
    router = ModelRouter(defaults)
    manager = RoutedProviderManager(
        default_provider=_FakeProvider(),
        default_model=defaults.model,
        router=router,
        provider_factory=lambda model: _BrokenProvider() if model == defaults.small_model else _FakeProvider(),
    )

    _, route = await manager.resolve([{"role": "user", "content": "hello"}], iteration=1)

    assert route.tier == "default"
    assert route.model == defaults.model
    assert route.source == "fallback"
