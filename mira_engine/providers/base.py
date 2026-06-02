"""Base LLM provider interface."""

from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from loguru import logger

from mira_engine.utils.helpers import image_placeholder_text


@dataclass
class ToolCallRequest:
    """A tool call request from the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]
    extra_content: dict[str, Any] | None = None
    provider_specific_fields: dict[str, Any] | None = None
    function_provider_specific_fields: dict[str, Any] | None = None

    def to_openai_tool_call(self) -> dict[str, Any]:
        """Serialize to OpenAI-style tool call payload."""
        tool_call = {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }
        if self.extra_content:
            tool_call["extra_content"] = self.extra_content
        if self.provider_specific_fields:
            tool_call["provider_specific_fields"] = self.provider_specific_fields
        if self.function_provider_specific_fields:
            tool_call["function"]["provider_specific_fields"] = (
                self.function_provider_specific_fields
            )
        return tool_call


@dataclass
class LLMResponse:
    """Response from an LLM provider."""

    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    retry_after: float | None = None
    reasoning_content: str | None = None
    thinking_blocks: list[dict] | None = None
    error_status_code: int | None = None
    error_kind: str | None = None
    error_type: str | None = None
    error_code: str | None = None
    error_retry_after_s: float | None = None
    error_should_retry: bool | None = None

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


@dataclass(frozen=True)
class GenerationSettings:
    """Default generation settings."""

    temperature: float = 0.7
    max_tokens: int = 4096
    reasoning_effort: str | None = None


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    _CHAT_RETRY_DELAYS = (1, 2, 4)
    _PERSISTENT_MAX_DELAY = 60
    _PERSISTENT_IDENTICAL_ERROR_LIMIT = 10
    _RETRY_HEARTBEAT_CHUNK = 30
    _TRANSIENT_ERROR_MARKERS = (
        "429",
        "rate limit",
        "500",
        "502",
        "503",
        "504",
        "overloaded",
        "timeout",
        "timed out",
        "connection",
        "server error",
        "temporarily unavailable",
    )
    _RETRYABLE_STATUS_CODES = frozenset({408, 409, 429})
    _TRANSIENT_ERROR_KINDS = frozenset({"timeout", "connection"})
    _NON_RETRYABLE_429_ERROR_TOKENS = frozenset(
        {
            "insufficient_quota",
            "quota_exceeded",
            "quota_exhausted",
            "billing_hard_limit_reached",
            "insufficient_balance",
            "credit_balance_too_low",
            "billing_not_active",
            "payment_required",
        }
    )
    _RETRYABLE_429_ERROR_TOKENS = frozenset(
        {
            "rate_limit_exceeded",
            "rate_limit_error",
            "too_many_requests",
            "request_limit_exceeded",
            "requests_limit_exceeded",
            "overloaded_error",
        }
    )
    _NON_RETRYABLE_429_TEXT_MARKERS = (
        "insufficient_quota",
        "insufficient quota",
        "quota exceeded",
        "quota exhausted",
        "billing hard limit",
        "billing_hard_limit_reached",
        "billing not active",
        "insufficient balance",
        "insufficient_balance",
        "credit balance too low",
        "payment required",
        "out of credits",
        "out of quota",
        "exceeded your current quota",
    )
    _RETRYABLE_429_TEXT_MARKERS = (
        "rate limit",
        "rate_limit",
        "too many requests",
        "retry after",
        "try again in",
        "temporarily unavailable",
        "overloaded",
        "concurrency limit",
    )
    _SENTINEL = object()

    def __init__(self, api_key: str | None = None, api_base: str | None = None):
        self.api_key = api_key
        self.api_base = api_base
        self.generation: GenerationSettings = GenerationSettings()

    def supports_vision(self, model: str | None = None) -> bool:
        """Return True when the (resolved) model accepts image input.

        Defaults to the provider's own default model when ``model`` is omitted.
        """
        from mira_engine.providers.registry import model_supports_vision

        resolved = model or getattr(self, "default_model", None)
        return model_supports_vision(resolved)

    @staticmethod
    def _sanitize_empty_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Sanitize message content: fix empty blocks, strip internal _meta fields."""
        result: list[dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content")

            if isinstance(content, str) and not content:
                clean = dict(msg)
                clean["content"] = (
                    None
                    if (msg.get("role") == "assistant" and msg.get("tool_calls"))
                    else "(empty)"
                )
                result.append(clean)
                continue

            if isinstance(content, list):
                new_items: list[Any] = []
                changed = False
                for item in content:
                    if (
                        isinstance(item, dict)
                        and item.get("type") in ("text", "input_text", "output_text")
                        and not item.get("text")
                    ):
                        changed = True
                        continue
                    if isinstance(item, dict) and "_meta" in item:
                        new_items.append({k: v for k, v in item.items() if k != "_meta"})
                        changed = True
                    else:
                        new_items.append(item)
                if changed:
                    clean = dict(msg)
                    if new_items:
                        clean["content"] = new_items
                    elif msg.get("role") == "assistant" and msg.get("tool_calls"):
                        clean["content"] = None
                    else:
                        clean["content"] = "(empty)"
                    result.append(clean)
                    continue

            if isinstance(content, dict):
                clean = dict(msg)
                clean["content"] = [content]
                result.append(clean)
                continue

            result.append(msg)
        return result

    @staticmethod
    def _sanitize_request_messages(
        messages: list[dict[str, Any]],
        allowed_keys: frozenset[str],
    ) -> list[dict[str, Any]]:
        sanitized = []
        for msg in messages:
            clean = {k: v for k, v in msg.items() if k in allowed_keys}
            if clean.get("role") == "assistant" and "content" not in clean:
                clean["content"] = None
            sanitized.append(clean)
        return sanitized

    @staticmethod
    def _extract_error_type_code(payload: Any) -> tuple[str | None, str | None]:
        """Extract provider error type/code from dict or JSON string payloads."""
        if payload is None:
            return None, None

        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                return None, None

        if not isinstance(payload, dict):
            return None, None

        error_obj = payload.get("error")
        if isinstance(error_obj, dict):
            error_type = error_obj.get("type")
            error_code = error_obj.get("code")
            return (
                str(error_type) if error_type is not None else None,
                str(error_code) if error_code is not None else None,
            )

        error_type = payload.get("type")
        error_code = payload.get("code")
        return (
            str(error_type) if error_type is not None else None,
            str(error_code) if error_code is not None else None,
        )

    @staticmethod
    def _tool_cache_marker_indices(tools: list[dict[str, Any]]) -> list[int]:
        """Select cache-marker tool indices: builtin→MCP boundary and the tail tool."""
        if not tools:
            return []

        names = [
            (tool.get("function") or {}).get("name") or tool.get("name") or ""
            for tool in tools
        ]
        first_mcp = next((idx for idx, name in enumerate(names) if isinstance(name, str) and name.startswith("mcp_")), None)

        indices: list[int] = []
        if first_mcp is not None and first_mcp > 0:
            indices.append(first_mcp - 1)
        indices.append(len(tools) - 1)
        return sorted(set(i for i in indices if 0 <= i < len(tools)))

    @staticmethod
    def _enforce_role_alternation(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove trailing assistants and merge consecutive user/assistant messages."""
        normalized = [dict(msg) for msg in messages]
        while normalized and normalized[-1].get("role") == "assistant":
            normalized.pop()

        merged: list[dict[str, Any]] = []
        for current in normalized:
            role = current.get("role")
            if not merged:
                merged.append(dict(current))
                continue

            prev = merged[-1]
            prev_role = prev.get("role")
            if role == prev_role and role in {"user", "assistant"}:
                prev_content = prev.get("content")
                cur_content = current.get("content")
                if isinstance(prev_content, str) and isinstance(cur_content, str):
                    prev["content"] = f"{prev_content}\n{cur_content}" if prev_content else cur_content
                else:
                    prev["content"] = cur_content
                continue

            merged.append(dict(current))
        return merged

    def _strip_images_if_text_only(
        self, messages: list[dict[str, Any]], model: str | None
    ) -> list[dict[str, Any]]:
        """Proactively drop image blocks when the target model has no vision.

        Avoids a guaranteed round-trip failure (and the noisy reactive retry)
        for text-only models such as ``moonshot/kimi-k2.6``. Returns the input
        unchanged when the model is vision-capable or has no image content.
        """
        if self.supports_vision(model):
            return messages
        stripped = self._strip_image_content(messages)
        if stripped is None:
            return messages
        logger.debug("Stripping image content for text-only model {}", model or "(default)")
        return stripped

    @staticmethod
    def _strip_image_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """Remove image_url blocks and keep placeholders for fallback retry."""
        changed = False
        stripped_messages: list[dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                new_blocks: list[Any] = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "image_url":
                        changed = True
                        meta = block.get("_meta")
                        path = meta.get("path") if isinstance(meta, dict) else None
                        new_blocks.append(
                            {
                                "type": "text",
                                "text": image_placeholder_text(path, empty="[image omitted]"),
                            }
                        )
                    else:
                        new_blocks.append(block)
                new_msg = dict(msg)
                new_msg["content"] = new_blocks
                stripped_messages.append(new_msg)
            else:
                stripped_messages.append(msg)
        return stripped_messages if changed else None

    @classmethod
    def _to_retry_seconds(cls, value: float, unit: str | None = None) -> float:
        normalized_unit = (unit or "s").lower()
        if normalized_unit in {"ms", "milliseconds"}:
            return max(0.1, value / 1000.0)
        if normalized_unit in {"m", "min", "minutes"}:
            return max(0.1, value * 60.0)
        return max(0.1, value)

    @classmethod
    def _extract_retry_after(cls, content: str | None) -> float | None:
        text = (content or "").lower()
        patterns = (
            r"retry after\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds|s|sec|secs|seconds|m|min|minutes)?",
            r"try again in\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds|s|sec|secs|seconds|m|min|minutes)",
            r"wait\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds|s|sec|secs|seconds|m|min|minutes)\s*before retry",
            r"retry[_-]?after[\"'\s:=]+(\d+(?:\.\d+)?)",
        )
        for idx, pattern in enumerate(patterns):
            match = re.search(pattern, text)
            if not match:
                continue
            value = float(match.group(1))
            unit = match.group(2) if idx < 3 else "s"
            return cls._to_retry_seconds(value, unit)
        return None

    @classmethod
    def _extract_retry_after_from_headers(cls, headers: Any) -> float | None:
        if not headers:
            return None

        def _header_value(name: str) -> Any:
            if hasattr(headers, "get"):
                value = headers.get(name) or headers.get(name.title())
                if value is not None:
                    return value
            if isinstance(headers, dict):
                for key, value in headers.items():
                    if isinstance(key, str) and key.lower() == name.lower():
                        return value
            return None

        try:
            retry_ms = _header_value("retry-after-ms")
            if retry_ms is not None:
                value = float(retry_ms) / 1000.0
                if value > 0:
                    return value
        except (TypeError, ValueError):
            pass

        retry_after = _header_value("retry-after")
        if retry_after is None:
            return None
        retry_after_text = str(retry_after).strip()
        if not retry_after_text:
            return None
        if re.fullmatch(r"\d+(?:\.\d+)?", retry_after_text):
            return cls._to_retry_seconds(float(retry_after_text), "s")
        try:
            retry_at = parsedate_to_datetime(retry_after_text)
        except Exception:
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        remaining = (retry_at - datetime.now(retry_at.tzinfo)).total_seconds()
        return max(0.1, remaining)

    @classmethod
    def _extract_retry_after_from_response(cls, response: LLMResponse) -> float | None:
        if response.error_retry_after_s is not None and response.error_retry_after_s > 0:
            return response.error_retry_after_s
        if response.retry_after is not None and response.retry_after > 0:
            return response.retry_after
        return cls._extract_retry_after(response.content)

    @classmethod
    def _is_transient_response(cls, response: LLMResponse) -> bool:
        if response.error_should_retry is False:
            return False
        if response.error_should_retry is True:
            return True

        status_code = response.error_status_code
        if status_code is not None:
            if status_code == 429:
                tokens = {
                    (response.error_type or "").lower(),
                    (response.error_code or "").lower(),
                    ((response.content or "").lower()),
                }
                if any(
                    marker
                    for marker in cls._NON_RETRYABLE_429_ERROR_TOKENS
                    if any(marker in token for token in tokens)
                ):
                    return False
                if any(
                    marker
                    for marker in cls._NON_RETRYABLE_429_TEXT_MARKERS
                    if any(marker in token for token in tokens)
                ):
                    return False
                if any(
                    marker
                    for marker in cls._RETRYABLE_429_ERROR_TOKENS
                    if any(marker in token for token in tokens)
                ):
                    return True
                if any(
                    marker
                    for marker in cls._RETRYABLE_429_TEXT_MARKERS
                    if any(marker in token for token in tokens)
                ):
                    return True
                return True
            if status_code in cls._RETRYABLE_STATUS_CODES:
                return True
            if status_code >= 500:
                return True

        if response.error_kind and response.error_kind.lower() in cls._TRANSIENT_ERROR_KINDS:
            return True

        lower = (response.content or "").lower()
        return any(marker in lower for marker in cls._TRANSIENT_ERROR_MARKERS)

    async def _sleep_with_heartbeat(
        self,
        delay: float,
        *,
        attempt: int,
        persistent: bool,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        remaining = max(0.0, delay)
        while remaining > 0:
            if on_retry_wait:
                kind = "persistent retry" if persistent else "retry"
                await on_retry_wait(
                    f"Model request failed, {kind} in {max(1, int(round(remaining)))}s "
                    f"(attempt {attempt})."
                )
            chunk = min(remaining, self._RETRY_HEARTBEAT_CHUNK)
            await asyncio.sleep(chunk)
            remaining -= chunk

    async def _safe_chat(self, **kwargs: Any) -> LLMResponse:
        return await self.chat(**kwargs)

    async def _safe_chat_stream(self, **kwargs: Any) -> LLMResponse:
        chat_stream = getattr(self, "chat_stream", None)
        if callable(chat_stream):
            return await chat_stream(**kwargs)
        return await self.chat(**kwargs)

    async def _run_with_retry(
        self,
        call: Callable[..., Awaitable[LLMResponse]],
        kw: dict[str, Any],
        original_messages: list[dict[str, Any]],
        *,
        retry_mode: str,
        on_retry_wait: Callable[[str], Awaitable[None]] | None,
    ) -> LLMResponse:
        attempt = 0
        delays = list(self._CHAT_RETRY_DELAYS)
        persistent = retry_mode == "persistent"
        last_response: LLMResponse | None = None
        last_error_key: str | None = None
        identical_error_count = 0
        while True:
            attempt += 1
            response = await call(**kw)
            if response.finish_reason != "error":
                return response
            last_response = response
            error_key = ((response.content or "").strip().lower() or None)
            if error_key and error_key == last_error_key:
                identical_error_count += 1
            else:
                last_error_key = error_key
                identical_error_count = 1 if error_key else 0

            if not self._is_transient_response(response):
                stripped = self._strip_image_content(original_messages)
                if stripped is not None and stripped != kw["messages"]:
                    logger.warning(
                        "Non-transient LLM error with image content, retrying without images"
                    )
                    retry_kw = dict(kw)
                    retry_kw["messages"] = stripped
                    return await call(**retry_kw)
                return response

            if persistent and identical_error_count >= self._PERSISTENT_IDENTICAL_ERROR_LIMIT:
                logger.warning(
                    "Stopping persistent retry after {} identical transient errors: {}",
                    identical_error_count,
                    (response.content or "")[:120].lower(),
                )
                return response

            if not persistent and attempt > len(delays):
                break

            base_delay = delays[min(attempt - 1, len(delays) - 1)]
            delay = self._extract_retry_after_from_response(response) or base_delay
            if persistent:
                delay = min(delay, self._PERSISTENT_MAX_DELAY)

            logger.warning(
                "LLM transient error (attempt {}{}), retrying in {}s: {}",
                attempt,
                "+" if persistent and attempt > len(delays) else f"/{len(delays)}",
                int(round(delay)),
                (response.content or "")[:120].lower(),
            )
            await self._sleep_with_heartbeat(
                delay,
                attempt=attempt,
                persistent=persistent,
                on_retry_wait=on_retry_wait,
            )

        return last_response if last_response is not None else await call(**kw)

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: object = _SENTINEL,
        temperature: object = _SENTINEL,
        reasoning_effort: object = _SENTINEL,
        tool_choice: str | dict[str, Any] | None = None,
        retry_mode: str = "standard",
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        if max_tokens is self._SENTINEL:
            max_tokens = self.generation.max_tokens
        if temperature is self._SENTINEL:
            temperature = self.generation.temperature
        if reasoning_effort is self._SENTINEL:
            reasoning_effort = self.generation.reasoning_effort

        messages = self._strip_images_if_text_only(messages, model)
        kw: dict[str, Any] = dict(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
        )
        return await self._run_with_retry(
            self._safe_chat,
            kw,
            messages,
            retry_mode=retry_mode,
            on_retry_wait=on_retry_wait,
        )

    async def chat_stream_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: object = _SENTINEL,
        temperature: object = _SENTINEL,
        reasoning_effort: object = _SENTINEL,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        retry_mode: str = "standard",
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        if max_tokens is self._SENTINEL:
            max_tokens = self.generation.max_tokens
        if temperature is self._SENTINEL:
            temperature = self.generation.temperature
        if reasoning_effort is self._SENTINEL:
            reasoning_effort = self.generation.reasoning_effort

        messages = self._strip_images_if_text_only(messages, model)
        kw: dict[str, Any] = dict(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
            on_content_delta=on_content_delta,
        )
        return await self._run_with_retry(
            self._safe_chat_stream,
            kw,
            messages,
            retry_mode=retry_mode,
            on_retry_wait=on_retry_wait,
        )

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        """Send a chat completion request."""

    @abstractmethod
    def get_default_model(self) -> str:
        """Get the default model for this provider."""
