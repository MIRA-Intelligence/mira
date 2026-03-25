"""Instinct-based model routing for balancing speed and quality."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from loguru import logger

from medpilot.config.schema import AgentDefaults
from medpilot.providers.base import LLMProvider

_ROUTE_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "route_complexity",
            "description": "Choose the best model tier for the current task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tier": {
                        "type": "string",
                        "enum": ["small", "medium", "large"],
                    },
                    "reason": {
                        "type": "string",
                        "description": "Short reason for the routing choice.",
                    },
                },
                "required": ["tier"],
            },
        },
    }
]


@dataclass(frozen=True)
class RoutedModel:
    """Resolved route for a single model call."""

    tier: str
    model: str
    score: int | None = None
    source: str = "instinct"
    reason: str | None = None


class ModelRouter:
    """Route requests to small, medium, or large models using a small-model judgment."""

    def __init__(self, defaults: AgentDefaults):
        self.defaults = defaults

    @property
    def enabled(self) -> bool:
        """Return True when routing is configured and enabled."""
        return bool(
            self.defaults.route_by_complexity
            and self.defaults.small_model
            and self.defaults.medium_model
            and self.defaults.large_model
        )

    @property
    def routing_model(self) -> str:
        """Return the model used only for routing judgment."""
        return self.defaults.route_model or self.defaults.small_model or self.defaults.model

    def default_route(self, source: str = "default", reason: str | None = None) -> RoutedModel:
        """Return the default-model route."""
        return RoutedModel(
            tier="default",
            model=self.defaults.model,
            score=None,
            source=source,
            reason=reason,
        )

    async def route(
        self,
        messages: list[dict[str, Any]],
        iteration: int,
        provider: LLMProvider,
    ) -> RoutedModel:
        """Use the small model to make a lightweight routing decision."""
        if not self.enabled:
            return self.default_route()

        response = await provider.chat(
            messages=self._build_instinct_messages(messages, iteration),
            tools=_ROUTE_TOOL,
            model=self.routing_model,
            max_tokens=120,
            temperature=0,
        )
        if response.has_tool_calls:
            args = response.tool_calls[0].arguments
            tier = args.get("tier")
            if tier in {"small", "medium", "large"}:
                return RoutedModel(
                    tier=tier,
                    model=self._model_for_tier(tier),
                    source="instinct",
                    reason=args.get("reason"),
                )

        logger.warning(
            "Model router instinct judgment failed; falling back to default model '{}'",
            self.defaults.model,
        )
        return self.default_route(source="fallback", reason="instinct_failed")

    @staticmethod
    def _latest_user_text(messages: list[dict[str, Any]]) -> str:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                return ModelRouter._coerce_text(msg.get("content"))
        return ""

    @staticmethod
    def _iter_text(messages: list[dict[str, Any]]) -> list[str]:
        return [ModelRouter._coerce_text(msg.get("content")) for msg in messages]

    @staticmethod
    def _coerce_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
        if isinstance(content, dict):
            text = content.get("text")
            return text if isinstance(text, str) else ""
        return ""

    def _model_for_tier(self, tier: str) -> str:
        if tier == "small":
            return self.defaults.small_model or self.defaults.model
        if tier == "medium":
            return self.defaults.medium_model or self.defaults.model
        return self.defaults.large_model or self.defaults.model

    def _build_instinct_messages(
        self,
        messages: list[dict[str, Any]],
        iteration: int,
    ) -> list[dict[str, str]]:
        latest_user = self._latest_user_text(messages)
        conversation_chars = sum(len(text) for text in self._iter_text(messages))
        tool_messages = sum(1 for msg in messages if msg.get("role") == "tool")
        assistant_tool_calls = sum(1 for msg in messages if msg.get("tool_calls"))
        return [
            {
                "role": "system",
                "content": (
                    "You are a routing judge. Choose small, medium, or large for the next model call. "
                    "Use small only for simple chat, direct factual questions, or straightforward single-step requests. "
                    "Use medium for normal implementation, ordinary coding, or standard debugging. "
                    "Any task requiring deep reasoning, complex trade-offs, broad planning, open-ended design, "
                    "novel idea generation, scientific or creative thinking, or non-obvious synthesis must be large. "
                    "When in doubt between medium and large, choose large. "
                    "You must call the route_complexity tool."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Latest user message:\n{latest_user[:2000]}\n\n"
                    f"Iteration: {iteration}\n"
                    f"Conversation chars: {conversation_chars}\n"
                    f"Tool messages: {tool_messages}\n"
                    f"Assistant tool call messages: {assistant_tool_calls}\n"
                    "Decide only the next-call tier. "
                    "If the task needs creativity, deep analysis, architecture, research planning, or difficult synthesis, return large."
                ),
            },
        ]


class RoutedProviderManager:
    """Resolve the provider/model pair for each model call."""

    def __init__(
        self,
        default_provider: LLMProvider,
        default_model: str,
        router: ModelRouter | None = None,
        provider_factory: Callable[[str], LLMProvider] | None = None,
    ):
        self._default_provider = default_provider
        self._default_model = default_model
        self._router = router
        self._provider_factory = provider_factory
        self._providers: dict[str, LLMProvider] = {default_model: default_provider}

    async def resolve(self, messages: list[dict[str, Any]], iteration: int = 1) -> tuple[LLMProvider, RoutedModel]:
        """Return provider and routed model for the current turn."""
        route = await self._select_route(messages, iteration)
        model = route.model or self._default_model
        if self._router and self._router.enabled:
            logger.debug(
                "Model router selected tier='{}' model='{}' score={} iteration={} source='{}' reason='{}'",
                route.tier,
                model,
                route.score,
                iteration,
                route.source,
                route.reason or "",
            )
        if model == self._default_model or not self._provider_factory:
            return self._default_provider, RoutedModel(route.tier, model, route.score, route.source, route.reason)
        provider = self._providers.get(model)
        if provider is None:
            provider = self._provider_factory(model)
            self._providers[model] = provider
        return provider, RoutedModel(route.tier, model, route.score, route.source, route.reason)

    async def _select_route(self, messages: list[dict[str, Any]], iteration: int) -> RoutedModel:
        if not self._router:
            return RoutedModel("default", self._default_model, source="default")
        try:
            instinct_provider = self._provider_for_model(self._router.routing_model)
            return await self._router.route(messages, iteration, instinct_provider)
        except Exception as exc:
            logger.warning("Model router instinct path failed: {}", exc)
            return self._router.default_route(source="fallback", reason="instinct_error")

    def _provider_for_model(self, model: str) -> LLMProvider:
        if model == self._default_model or not self._provider_factory:
            return self._default_provider
        provider = self._providers.get(model)
        if provider is None:
            provider = self._provider_factory(model)
            self._providers[model] = provider
        return provider
