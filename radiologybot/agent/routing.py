"""Heuristic model routing for balancing speed and quality."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from radiologybot.config.schema import AgentDefaults
from radiologybot.providers.base import LLMProvider


@dataclass(frozen=True)
class RoutedModel:
    """Resolved route for a single model call."""

    tier: str
    model: str


class ModelRouter:
    """Route requests to small, medium, or large models based on task complexity."""

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

    def route(self, messages: list[dict[str, Any]], iteration: int = 1) -> RoutedModel:
        """Choose the best model tier for the current conversation state."""
        if not self.enabled:
            return RoutedModel(tier="default", model=self.defaults.model)

        score = self._score(messages, iteration)
        if score >= 9:
            return RoutedModel(tier="large", model=self.defaults.large_model or self.defaults.model)
        if score >= 4:
            return RoutedModel(tier="medium", model=self.defaults.medium_model or self.defaults.model)
        return RoutedModel(tier="small", model=self.defaults.small_model or self.defaults.model)

    def _score(self, messages: list[dict[str, Any]], iteration: int) -> int:
        last_user = self._latest_user_text(messages)
        conversation_chars = sum(len(text) for text in self._iter_text(messages))
        tool_messages = sum(1 for msg in messages if msg.get("role") == "tool")
        assistant_tool_calls = sum(1 for msg in messages if msg.get("tool_calls"))

        score = 0
        lowered = last_user.lower()

        if len(last_user) > 1200:
            score += 5
        elif len(last_user) > 400:
            score += 3
        elif len(last_user) > 120:
            score += 1

        if conversation_chars > 6000:
            score += 3
        elif conversation_chars > 2500:
            score += 1

        complex_keywords = (
            "architecture",
            "design",
            "research",
            "trade-off",
            "tradeoff",
            "refactor",
            "multi-step",
            "multi step",
            "pipeline",
            "analyze",
            "analysis",
            "debug",
            "investigate",
            "route",
            "complex",
            "difficulty",
            "performance",
            "benchmark",
            "compare",
            "plan",
        )
        if any(keyword in lowered for keyword in complex_keywords):
            score += 3

        code_markers = ("```", "def ", "class ", "traceback", "error:", "pytest", "stack trace")
        if any(marker in lowered for marker in code_markers):
            score += 2

        if any(token in lowered for token in ("and ", " then ", "同时", "另外", "并且", "先", "再")):
            score += 1

        if tool_messages >= 3 or assistant_tool_calls >= 2:
            score += 3
        elif tool_messages >= 1 or assistant_tool_calls >= 1:
            score += 1

        if iteration >= 6:
            score += 3
        elif iteration >= 3:
            score += 1

        return score

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

    def resolve(self, messages: list[dict[str, Any]], iteration: int = 1) -> tuple[LLMProvider, RoutedModel]:
        """Return provider and routed model for the current turn."""
        route = self._router.route(messages, iteration) if self._router else RoutedModel("default", self._default_model)
        model = route.model or self._default_model
        if model == self._default_model or not self._provider_factory:
            return self._default_provider, RoutedModel(route.tier, model)
        provider = self._providers.get(model)
        if provider is None:
            provider = self._provider_factory(model)
            self._providers[model] = provider
        return provider, RoutedModel(route.tier, model)
