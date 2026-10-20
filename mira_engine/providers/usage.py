"""Normalize OpenAI-compatible usage without turning absent cache data into zero."""

from collections.abc import Mapping
from typing import Any


def _field(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)


def _count(value: Any) -> int | None:
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    return value if type(value) is int and value >= 0 else None


def extract_usage(response: Any) -> dict[str, int]:
    """Support raw JSON, SDK objects, Chat Completions and Responses usage.

    Cache reads are a subset of input, never added to the total. Preserve an
    explicit zero; omit missing/null/invalid detail fields so callers can show
    unknown telemetry rather than asserting a cache miss.
    """
    usage = _field(response, "usage")
    if usage is None:
        return {}
    result = {}
    for output, aliases in (
        ("prompt_tokens", ("prompt_tokens", "input_tokens")),
        ("completion_tokens", ("completion_tokens", "output_tokens")),
        ("total_tokens", ("total_tokens",)),
    ):
        value = next((n for key in aliases if (n := _count(_field(usage, key))) is not None), None)
        result[output] = value or 0
    if not result["total_tokens"]:
        result["total_tokens"] = result["prompt_tokens"] + result["completion_tokens"]
    for path in (
        ("prompt_tokens_details", "cached_tokens"),
        ("input_tokens_details", "cached_tokens"),
        ("cached_tokens",),
        ("prompt_cache_hit_tokens",),
    ):
        value = usage
        for key in path:
            value = _field(value, key)
        count = _count(value)
        if count is not None:
            result["cached_tokens"] = count
            break
    return result
