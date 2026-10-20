"""Runtime-specific helper functions and constants."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from mira_engine.utils.helpers import stringify_text_blocks

_MAX_REPEAT_EXTERNAL_LOOKUPS = 2

# Leading apology used when an unexpected exception escapes message dispatch.
# Kept distinct from the provider-level "...calling the AI model." string so
# CLI error detection (`_is_llm_error`) stays unambiguous.
DISPATCH_ERROR_PREFIX = "Sorry, I ran into a problem while handling your message."

_MAX_ERROR_DETAIL_CHARS = 300

# Stable error category codes shared with the UI i18n layer. Keep these values
# in sync with the `error<Code>` keys in mira-ui (`src/i18n/index.ts`).
ERROR_CODE_TIMEOUT = "timeout"
ERROR_CODE_NETWORK = "network"
ERROR_CODE_AUTH = "auth"
ERROR_CODE_RATE_LIMIT = "rate_limit"
ERROR_CODE_CONTEXT_WINDOW = "context_window"
ERROR_CODE_UNKNOWN = "unknown"

# English hint text per category, used for channels without their own i18n
# (CLI, Telegram, QQ, ...). The UI localizes via `error_code` instead.
_ERROR_HINTS = {
    ERROR_CODE_TIMEOUT: (
        "the request timed out — the model or a tool took too long to respond. "
        "Please try again."
    ),
    ERROR_CODE_NETWORK: (
        "a network error occurred while reaching the model or a tool. "
        "Check your connection/proxy and try again."
    ),
    ERROR_CODE_AUTH: (
        "authentication failed. Check the provider API key/credentials in your "
        "config (`mira config`)."
    ),
    ERROR_CODE_RATE_LIMIT: (
        "the provider rate-limited the request or the quota is exhausted. "
        "Please wait a moment and retry."
    ),
    ERROR_CODE_CONTEXT_WINDOW: (
        "the conversation exceeded the model's context window. "
        "Start a new session or shorten the input."
    ),
}


def classify_exception(exc: BaseException) -> str:
    """Map *exc* onto a stable :data:`ERROR_CODE_*` category.

    The returned code is wire-stable and shared with the UI i18n layer so the
    frontend can localize without parsing English text.
    """
    lowered = " ".join(str(exc).split()).lower()

    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timed out" in lowered or "timeout" in lowered:
        return ERROR_CODE_TIMEOUT
    if isinstance(exc, ConnectionError) or any(
        kw in lowered for kw in ("connection refused", "connection reset", "connection error",
                                 "failed to connect", "cannot connect", "network", "dns",
                                 "ssl", "unreachable", "name resolution")
    ):
        return ERROR_CODE_NETWORK
    if any(kw in lowered for kw in ("401", "unauthorized", "api key", "api_key",
                                    "invalid key", "authentication", "forbidden", "403")):
        return ERROR_CODE_AUTH
    if any(kw in lowered for kw in ("429", "rate limit", "too many requests", "quota", "insufficient_quota")):
        return ERROR_CODE_RATE_LIMIT
    if any(kw in lowered for kw in ("context length", "maximum context", "context window",
                                    "too many tokens", "max_tokens")):
        return ERROR_CODE_CONTEXT_WINDOW
    return ERROR_CODE_UNKNOWN


def error_detail_text(exc: BaseException) -> str:
    """Return a trimmed, single-line ``ExceptionType: message`` for *exc*."""
    raw = " ".join(str(exc).split())  # collapse newlines/whitespace
    detail = raw or "no additional detail was reported"
    if len(detail) > _MAX_ERROR_DETAIL_CHARS:
        detail = detail[:_MAX_ERROR_DETAIL_CHARS].rstrip() + "…"
    return f"{type(exc).__name__}: {detail}"


def describe_exception(exc: BaseException) -> str:
    """Return a concise, user-safe English reason for *exc*.

    Maps common failure modes onto actionable hints, and otherwise falls back
    to a trimmed ``ExceptionType: message`` form so the user always gets
    something more useful than a bare apology.
    """
    hint = _ERROR_HINTS.get(classify_exception(exc))
    if hint is not None:
        return hint
    return error_detail_text(exc)


def build_dispatch_error_text(exc: BaseException, *, channel: str | None = None) -> str:
    """Compose the user-facing message for an unhandled dispatch exception."""
    text = f"{DISPATCH_ERROR_PREFIX} {describe_exception(exc)}"
    if channel == "cli":
        text += "\n\nRun `mira agent --logs` to view the full traceback."
    return text

EMPTY_FINAL_RESPONSE_MESSAGE = (
    "I completed the tool steps but couldn't produce a final answer. "
    "Please try again or narrow the task."
)

FINALIZATION_RETRY_PROMPT = (
    "Please provide your response to the user based on the conversation above."
)

LENGTH_RECOVERY_PROMPT = (
    "Output limit reached. Continue exactly where you left off "
    "— no recap, no apology. Break remaining work into smaller steps if needed."
)


def empty_tool_result_message(tool_name: str) -> str:
    """Short prompt-safe marker for tools that completed without visible output."""
    return f"({tool_name} completed with no output)"


def ensure_nonempty_tool_result(tool_name: str, content: Any) -> Any:
    """Replace semantically empty tool results with a short marker string."""
    if content is None:
        return empty_tool_result_message(tool_name)
    if isinstance(content, str) and not content.strip():
        return empty_tool_result_message(tool_name)
    if isinstance(content, list):
        if not content:
            return empty_tool_result_message(tool_name)
        text_payload = stringify_text_blocks(content)
        if text_payload is not None and not text_payload.strip():
            return empty_tool_result_message(tool_name)
    return content


def is_blank_text(content: str | None) -> bool:
    """True when *content* is missing or only whitespace."""
    return content is None or not content.strip()


def build_finalization_retry_message() -> dict[str, str]:
    """A short no-tools-allowed prompt for final answer recovery."""
    return {"role": "user", "content": FINALIZATION_RETRY_PROMPT}


def build_length_recovery_message() -> dict[str, str]:
    """Prompt the model to continue after hitting output token limit."""
    return {"role": "user", "content": LENGTH_RECOVERY_PROMPT}


def external_lookup_signature(tool_name: str, arguments: dict[str, Any]) -> str | None:
    """Stable signature for repeated external lookups we want to throttle."""
    if tool_name == "web_fetch":
        url = str(arguments.get("url") or "").strip()
        if url:
            return f"web_fetch:{url.lower()}"
    if tool_name == "web_search":
        query = str(arguments.get("query") or arguments.get("search_term") or "").strip()
        if query:
            return f"web_search:{query.lower()}"
    return None


def repeated_external_lookup_error(
    tool_name: str,
    arguments: dict[str, Any],
    seen_counts: dict[str, int],
) -> str | None:
    """Block repeated external lookups after a small retry budget."""
    signature = external_lookup_signature(tool_name, arguments)
    if signature is None:
        return None
    count = seen_counts.get(signature, 0) + 1
    seen_counts[signature] = count
    if count <= _MAX_REPEAT_EXTERNAL_LOOKUPS:
        return None
    logger.warning(
        "Blocking repeated external lookup {} on attempt {}",
        signature[:160],
        count,
    )
    return (
        "Error: repeated external lookup blocked. "
        "Use the results you already have to answer, or try a meaningfully different source."
    )
