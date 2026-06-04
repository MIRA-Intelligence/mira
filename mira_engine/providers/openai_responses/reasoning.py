"""Reasoning effort normalization for OpenAI-family providers."""

from __future__ import annotations

_OPENAI_REASONING_EFFORTS = {"low", "medium", "high", "max", "xhigh"}


def normalize_openai_reasoning_effort(effort: str | None) -> str | None:
    """Return an OpenAI-compatible reasoning effort value.

    MIRA exposes ``adaptive`` because Anthropic supports adaptive thinking, but
    OpenAI-family Responses/chat endpoints reject that enum. Treat adaptive as
    the strongest common non-experimental setting so a shared UI config keeps
    working across providers.
    """
    if effort is None:
        return None
    value = effort.strip().lower()
    if not value or value == "none":
        return None
    if value == "adaptive":
        return "high"
    if value in _OPENAI_REASONING_EFFORTS:
        return value
    return value
