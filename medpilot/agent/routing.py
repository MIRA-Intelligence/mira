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
    candidates: tuple[str, ...] = ()
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
        return self.defaults.primary_routing_model

    @property
    def routing_candidates(self) -> tuple[str, ...]:
        """Return the candidate routing models used for routing judgment."""
        return tuple(self.defaults.routing_model_candidates)

    def default_route(self, source: str = "default", reason: str | None = None) -> RoutedModel:
        """Return the default-model route."""
        return RoutedModel(
            tier="default",
            model=self.defaults.primary_model,
            candidates=tuple(self.defaults.default_model_candidates),
            score=None,
            source=source,
            reason=reason,
        )

    async def route(
        self,
        messages: list[dict[str, Any]],
        iteration: int,
        provider: LLMProvider,
        routing_model: str | None = None,
        allow_default_fallback: bool = True,
    ) -> RoutedModel:
        """Use the small model to make a lightweight routing decision."""
        if not self.enabled:
            return self.default_route()

        selected_routing_model = routing_model or self.routing_model

        response = await provider.chat(
            messages=self._build_instinct_messages(messages, iteration),
            tools=_ROUTE_TOOL,
            model=selected_routing_model,
            max_tokens=120,
            temperature=0,
        )
        if response.finish_reason == "error":
            raise RuntimeError(response.content or f"Routing model '{selected_routing_model}' failed")
        if response.has_tool_calls:
            args = response.tool_calls[0].arguments
            tier = args.get("tier")
            if tier in {"small", "medium", "large"}:
                return RoutedModel(
                    tier=tier,
                    model=self._model_for_tier(tier),
                    candidates=tuple(self._candidates_for_tier(tier)),
                    source="instinct",
                    reason=args.get("reason"),
                )

        if not allow_default_fallback:
            raise RuntimeError(f"Routing model '{selected_routing_model}' returned no valid tier")

        logger.warning(
            "Model router instinct judgment failed; falling back to default model '{}'",
            self.defaults.primary_model,
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
        return self.defaults.primary_model_for_tier(tier)

    def _candidates_for_tier(self, tier: str) -> list[str]:
        return self.defaults.tier_model_candidates(tier)

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
        self._successful_models: list[str] = []
        self._failed_models: set[str] = set()

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
            return self._default_provider, RoutedModel(
                route.tier,
                model,
                route.candidates,
                route.score,
                route.source,
                route.reason,
            )
        provider = self._providers.get(model)
        if provider is None:
            provider = self._provider_factory(model)
            self._providers[model] = provider
        return provider, RoutedModel(
            route.tier,
            model,
            route.candidates,
            route.score,
            route.source,
            route.reason,
        )

    async def chat(
        self,
        route: RoutedModel,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
    ) -> tuple[LLMResponse, RoutedModel]:
        """Call the routed model and fall back to configured backups on retryable errors."""
        candidates = self._ordered_candidate_models(tuple(route.candidates) or (route.model,))
        last_response: LLMResponse | None = None
        last_error: Exception | None = None

        for index, model in enumerate(candidates):
            provider = self._provider_for_model(model)
            try:
                response = await provider.chat(
                    messages=messages,
                    tools=tools,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    reasoning_effort=reasoning_effort,
                )
            except Exception as exc:
                last_error = exc
                self._mark_model_failed(model)
                if index < len(candidates) - 1:
                    logger.warning(
                        "Model '{}' raised '{}'; trying fallback model '{}'",
                        model,
                        exc,
                        candidates[index + 1],
                    )
                    continue
                raise

            if response.finish_reason != "error" or not self._should_retry_with_fallback(response.content):
                if response.finish_reason == "error":
                    self._mark_model_failed(model)
                else:
                    self._mark_model_success(model)
                return response, RoutedModel(
                    route.tier,
                    model,
                    candidates,
                    route.score,
                    route.source,
                    route.reason,
                )

            last_response = response
            self._mark_model_failed(model)
            if index < len(candidates) - 1:
                logger.warning(
                    "Model '{}' failed with retryable error; trying fallback model '{}': {}",
                    model,
                    candidates[index + 1],
                    (response.content or "")[:200],
                )

        if last_response is not None:
            return LLMResponse(
                content=(
                    f"All candidate models failed for this turn. "
                    f"Last error from '{candidates[-1]}': {last_response.content or 'unknown error'}"
                ),
                finish_reason="error",
                usage=last_response.usage,
                reasoning_content=last_response.reasoning_content,
                thinking_blocks=last_response.thinking_blocks,
            ), RoutedModel(
                route.tier,
                candidates[-1],
                candidates,
                route.score,
                route.source,
                route.reason,
            )
        if last_error is not None:
            raise last_error
        raise RuntimeError("No candidate models available for chat completion")

    async def _select_route(self, messages: list[dict[str, Any]], iteration: int) -> RoutedModel:
        if not self._router:
            return RoutedModel("default", self._default_model, (self._default_model,), source="default")

        last_error: Exception | None = None
        routing_candidates = self._ordered_candidate_models(self._router.routing_candidates)
        for index, routing_model in enumerate(routing_candidates):
            try:
                instinct_provider = self._provider_for_model(routing_model)
                route = await self._router.route(
                    messages,
                    iteration,
                    instinct_provider,
                    routing_model=routing_model,
                    allow_default_fallback=False,
                )
                self._mark_model_success(routing_model)
                return route
            except Exception as exc:
                last_error = exc
                self._mark_model_failed(routing_model)
                if index < len(routing_candidates) - 1:
                    logger.warning(
                        "Routing model '{}' failed; trying fallback routing model '{}': {}",
                        routing_model,
                        routing_candidates[index + 1],
                        exc,
                    )
                    continue
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

    def _ordered_candidate_models(self, candidates: tuple[str, ...]) -> tuple[str, ...]:
        """Return session-local candidates ordered by recent success, with failures moved last."""
        successful = [model for model in self._successful_models if model in candidates]
        neutral = [
            model for model in candidates if model not in successful and model not in self._failed_models
        ]
        failed = [
            model for model in candidates if model not in successful and model in self._failed_models
        ]
        ordered = tuple(successful + neutral + failed)
        if ordered != candidates:
            logger.debug(
                "Reordered candidate models for session: {} -> {}",
                list(candidates),
                list(ordered),
            )
        return ordered

    def _mark_model_failed(self, model: str) -> None:
        """Move a failing model to the back of session preference ordering."""
        self._failed_models.add(model)
        self._successful_models = [item for item in self._successful_models if item != model]

    def _mark_model_success(self, model: str) -> None:
        """Promote a successful model to the front of session preference ordering."""
        self._failed_models.discard(model)
        self._successful_models = [item for item in self._successful_models if item != model]
        self._successful_models.insert(0, model)

    @staticmethod
    def _should_retry_with_fallback(error_text: str | None) -> bool:
        """Return True when an error is likely transient or model-specific."""
        if not error_text:
            return True

        error = error_text.lower()
        non_retryable_markers = (
            "authentication",
            "unauthorized",
            "invalid api key",
            "incorrect api key",
            "permission",
            "forbidden",
            "context length",
            "maximum context length",
            "unsupported parameter",
            "invalid_request_error",
            "bad request",
            "tool schema",
            "does not support tools",
        )
        if any(marker in error for marker in non_retryable_markers):
            return False

        retryable_markers = (
            "rate limit",
            "429",
            "500",
            "502",
            "503",
            "504",
            "timeout",
            "timed out",
            "overloaded",
            "overload",
            "unavailable",
            "temporar",
            "capacity",
            "busy",
            "connection",
            "network",
            "try again",
            "service unavailable",
        )
        return any(marker in error for marker in retryable_markers)
