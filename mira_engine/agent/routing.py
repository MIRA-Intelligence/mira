"""Provider/model resolution with candidate fallback.

Historically this module also hosted an instinct-based ``ModelRouter`` that
asked a small model to pick a small/medium/large tier per turn. That tier
router has been removed: Mira's roles (and the ``team`` profile in
particular) map each role to an explicit provider+model, which makes the
runtime complexity-routing redundant. What remains is
:class:`RoutedProviderManager`, the load-bearing layer that every model call
goes through to resolve a provider for a model and fall back across candidate
models on retryable errors (rate limits, 5xx, timeouts).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from loguru import logger

from mira_engine.providers.base import LLMProvider, LLMResponse


@dataclass(frozen=True)
class RoutedModel:
    """Resolved route for a single model call."""

    tier: str
    model: str
    candidates: tuple[str, ...] = ()
    score: int | None = None
    source: str = "default"
    reason: str | None = None


class RoutedProviderManager:
    """Resolve the provider/model pair for each call, with candidate fallback.

    The manager always resolves to the configured default model, and provides
    cross-candidate fallback so a single turn survives a transient failure of
    the primary model. ``default_candidates`` carries the ordered fallback
    list for the default model (previously sourced from the now-removed
    router); without it the default model would have no backups.
    """

    def __init__(
        self,
        default_provider: LLMProvider,
        default_model: str,
        provider_factory: Callable[[str], LLMProvider] | None = None,
        default_candidates: tuple[str, ...] = (),
    ):
        self._default_provider = default_provider
        self._default_model = default_model
        self._provider_factory = provider_factory
        self._default_candidates = tuple(default_candidates) or (default_model,)
        self._providers: dict[str, LLMProvider] = {default_model: default_provider}
        self._successful_models: list[str] = []
        self._failed_models: set[str] = set()

    async def resolve(
        self, messages: list[dict[str, Any]], iteration: int = 1
    ) -> tuple[LLMProvider, RoutedModel]:
        """Return provider and routed model for the current turn."""
        route = RoutedModel(
            tier="default",
            model=self._default_model,
            candidates=self._default_candidates,
            source="default",
        )
        return self._default_provider, route

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

    async def chat_stream_with_retry(
        self,
        route: RoutedModel,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> tuple[LLMResponse, RoutedModel]:
        """Call the routed model with token streaming when the client requests it."""
        candidates = self._ordered_candidate_models(tuple(route.candidates) or (route.model,))
        last_response: LLMResponse | None = None
        last_error: Exception | None = None

        for index, model in enumerate(candidates):
            provider = self._provider_for_model(model)
            try:
                response = await provider.chat_stream_with_retry(
                    messages=messages,
                    tools=tools,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    reasoning_effort=reasoning_effort,
                    on_content_delta=on_content_delta,
                )
            except Exception as exc:
                last_error = exc
                self._mark_model_failed(model)
                if index < len(candidates) - 1:
                    logger.warning(
                        "Model '{}' raised '{}' while streaming; trying fallback model '{}'",
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
                    "Model '{}' failed with retryable streaming error; trying fallback model '{}': {}",
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
