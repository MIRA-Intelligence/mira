"""Azure OpenAI provider implementation via OpenAI Responses API SDK."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from openai import AsyncOpenAI

from mira_engine.providers.base import LLMProvider, LLMResponse
from mira_engine.providers.openai_responses import (
    consume_sdk_stream,
    convert_messages,
    convert_tools,
    normalize_openai_reasoning_effort,
    parse_response_output,
)

_AZURE_MSG_KEYS = frozenset({"role", "content", "tool_calls", "tool_call_id", "name"})
_DEFAULT_ACCEPT_ENCODING = "identity"


class AzureOpenAIProvider(LLMProvider):
    """
    Azure OpenAI provider backed by the OpenAI SDK Responses API.
    """

    def __init__(
        self,
        api_key: str = "",
        api_base: str = "",
        default_model: str = "gpt-5.2-chat",
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model

        if not api_key:
            raise ValueError("Azure OpenAI api_key is required")
        if not api_base:
            raise ValueError("Azure OpenAI api_base is required")

        normalized_base = api_base.rstrip("/") + "/"
        self.api_base = normalized_base
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=f"{normalized_base}openai/v1/",
            default_headers={
                "api-key": api_key,
                "x-session-affinity": uuid.uuid4().hex,
                "Accept-Encoding": _DEFAULT_ACCEPT_ENCODING,
            },
            max_retries=0,
        )

    @staticmethod
    def _supports_temperature(
        deployment_name: str,
        reasoning_effort: str | None = None,
    ) -> bool:
        """Return True when temperature is likely supported for this deployment."""
        if reasoning_effort:
            return False
        name = deployment_name.lower()
        return not any(token in name for token in ("gpt-5", "o1", "o3", "o4"))

    def _build_body(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: Any | None = None,
    ) -> dict[str, Any]:
        """Build OpenAI Responses API request body."""
        deployment_name = model or self.default_model
        prepared = self._sanitize_request_messages(
            self._sanitize_empty_content(messages),
            _AZURE_MSG_KEYS,
        )
        system_prompt, input_items = convert_messages(prepared)
        normalized_reasoning_effort = normalize_openai_reasoning_effort(reasoning_effort)

        body: dict[str, Any] = {
            "model": deployment_name,
            "input": input_items,
            "max_output_tokens": max(1, max_tokens),
            "store": False,
        }
        if system_prompt:
            body["instructions"] = system_prompt

        if self._supports_temperature(deployment_name, normalized_reasoning_effort):
            body["temperature"] = temperature
        if normalized_reasoning_effort:
            body["reasoning"] = {"effort": normalized_reasoning_effort}
            body["include"] = ["reasoning.encrypted_content"]
        if tools:
            body["tools"] = convert_tools(tools)
            body["tool_choice"] = tool_choice if tool_choice is not None else "auto"
        return body

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
        """Send a non-streaming chat request to Azure OpenAI."""
        try:
            body = self._build_body(
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                tool_choice=tool_choice,
            )
            return parse_response_output(await self._client.responses.create(**body))
        except Exception as e:
            return LLMResponse(
                content=f"Error calling Azure OpenAI: {e}",
                finish_reason="error",
            )

    @classmethod
    def _handle_error(cls, e: Exception) -> LLMResponse:
        response = getattr(e, "response", None)
        headers = getattr(response, "headers", None)
        body = (
            getattr(e, "body", None)
            or getattr(e, "doc", None)
            or getattr(response, "text", None)
        )
        body_text = body if isinstance(body, str) else str(body) if body is not None else ""
        msg = f"Error: {body_text.strip()[:500]}" if body_text.strip() else f"Error calling Azure OpenAI: {e}"
        retry_after = cls._extract_retry_after_from_headers(headers)
        if retry_after is None:
            retry_after = cls._extract_retry_after(msg)
        return LLMResponse(
            content=msg,
            finish_reason="error",
            retry_after=retry_after,
            error_retry_after_s=retry_after,
        )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: Any | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """Send a streaming chat request to Azure OpenAI."""
        try:
            body = self._build_body(
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                tool_choice=tool_choice,
            )
            body["stream"] = True
            stream = await self._client.responses.create(**body)
            content, tool_calls, finish_reason, usage, reasoning_content = await consume_sdk_stream(
                stream,
                on_content_delta,
            )
            return LLMResponse(
                content=content or None,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=usage,
                reasoning_content=reasoning_content,
            )
        except Exception as e:
            return LLMResponse(
                content=f"Error calling Azure OpenAI: {e}",
                finish_reason="error",
            )

    def get_default_model(self) -> str:
        """Get the default model (also used as default deployment name)."""
        return self.default_model
