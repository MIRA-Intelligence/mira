"""Model information helpers for the onboard wizard."""

from __future__ import annotations

from typing import Any

from mira_engine.providers.model_fetch import all_cached_models, models_for_provider


def get_all_models() -> list[str]:
    return [model.config_model for model in all_cached_models()]


def find_model_info(model_name: str) -> dict[str, Any] | None:
    normalized = model_name.strip()
    if not normalized:
        return None
    for model in all_cached_models():
        if normalized in {model.config_model, model.id}:
            return {
                "id": model.id,
                "model": model.config_model,
                "display_name": model.display_name,
                "context_window_tokens": model.context_window_tokens,
                "capabilities": model.capabilities,
            }
    return None


def get_model_context_limit(model: str, provider: str = "auto") -> int | None:
    normalized = model.strip()
    if not normalized:
        return None
    candidates = all_cached_models() if provider == "auto" else models_for_provider(provider)
    for item in candidates:
        if normalized in {item.config_model, item.id}:
            return item.context_window_tokens
    return None


def get_model_suggestions(partial: str, provider: str = "auto", limit: int = 20) -> list[str]:
    query = partial.strip().lower()
    candidates = all_cached_models() if provider == "auto" else models_for_provider(provider)
    results: list[str] = []
    seen: set[str] = set()
    for model in candidates:
        value = model.config_model
        if query and query not in value.lower() and query not in model.id.lower():
            continue
        if value in seen:
            continue
        seen.add(value)
        results.append(value)
        if len(results) >= limit:
            break
    return results


def format_token_count(tokens: int) -> str:
    """Format token count for display (e.g., 200000 -> '200,000')."""
    return f"{tokens:,}"
