"""OpenAI Codex Responses Provider."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, AsyncGenerator

import httpx
from loguru import logger
from oauth_cli_kit import get_token as get_codex_token

from mira_engine.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from mira_engine.providers.oauth_state import ensure_oauth_state_dirs_for_runtime
from mira_engine.providers.openai_responses import normalize_openai_reasoning_effort

DEFAULT_CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_ORIGINATOR = "mira"
CODEX_TIMEOUT = httpx.Timeout(300.0, connect=30.0)


class CodexAPIError(RuntimeError):
    """HTTP-level Codex API error with retry metadata."""

    def __init__(
        self,
        status_code: int,
        message: str,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class OpenAICodexProvider(LLMProvider):
    """Use Codex OAuth to call the Responses API."""

    def __init__(self, default_model: str = "openai-codex/gpt-5.1-codex", proxy: str | None = None):
        super().__init__(api_key=None, api_base=None)
        self.default_model = default_model
        self.proxy = proxy or None

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
        model = model or self.default_model
        system_prompt, input_items = _convert_messages(messages)

        try:
            ensure_oauth_state_dirs_for_runtime()
            token = await asyncio.to_thread(get_codex_token)
            if not getattr(token, "access", None):
                raise RuntimeError(
                    "Codex OAuth token is missing. Run `mira onboard --wizard` and log in to OpenAI Codex."
                )
            if not getattr(token, "account_id", None):
                raise RuntimeError(
                    "Codex OAuth account id is missing. Run `mira onboard --wizard` and log in again."
                )

            headers = _build_headers(token.account_id, token.access)
            body: dict[str, Any] = {
                "model": _strip_model_prefix(model),
                "store": False,
                "stream": True,
                "instructions": system_prompt,
                "input": input_items,
                "text": {"verbosity": "medium"},
                "include": ["reasoning.encrypted_content"],
                "prompt_cache_key": _prompt_cache_key(messages),
                "parallel_tool_calls": True,
            }

            normalized_reasoning_effort = normalize_openai_reasoning_effort(reasoning_effort)
            if normalized_reasoning_effort:
                body["reasoning"] = {"effort": normalized_reasoning_effort}

            if tools:
                body["tools"] = _convert_tools(tools)
                body["tool_choice"] = tool_choice if tool_choice is not None else "auto"

            url = DEFAULT_CODEX_URL
            try:
                content, tool_calls, finish_reason = await _request_codex(
                    url, headers, body, verify=True, proxy=self.proxy
                )
            except Exception as e:
                if "CERTIFICATE_VERIFY_FAILED" not in str(e):
                    raise
                logger.warning("SSL certificate verification failed for Codex API; retrying with verify=False")
                content, tool_calls, finish_reason = await _request_codex(
                    url, headers, body, verify=False, proxy=self.proxy
                )
            return LLMResponse(
                content=content,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
            )
        except Exception as e:
            logger.exception("Codex request failed: {}", _format_exception(e, self.proxy))
            return LLMResponse(
                content=f"Error calling Codex: {_format_exception(e, self.proxy)}",
                finish_reason="error",
                error_status_code=getattr(e, "status_code", None),
                error_kind=_error_kind(e),
                error_retry_after_s=getattr(e, "retry_after", None),
            )

    def get_default_model(self) -> str:
        return self.default_model


def _strip_model_prefix(model: str) -> str:
    if model.startswith("openai-codex/") or model.startswith("openai_codex/"):
        return model.split("/", 1)[1]
    return model


def _build_headers(account_id: str, token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "x-openai-internal-codex-residency": "us",
        "originator": DEFAULT_ORIGINATOR,
        "User-Agent": "mira (python)",
        "accept": "text/event-stream",
        "content-type": "application/json",
    }


async def _request_codex(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    verify: bool,
    proxy: str | None = None,
) -> tuple[str, list[ToolCallRequest], str]:
    try:
        return await _request_codex_once(url, headers, body, verify=verify, proxy=proxy)
    except httpx.ConnectTimeout:
        if proxy:
            raise
        logger.warning("Codex connection timed out; retrying with IPv4-only transport")
        return await _request_codex_once(
            url,
            headers,
            body,
            verify=verify,
            proxy=None,
            force_ipv4=True,
        )


async def _request_codex_once(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    verify: bool,
    proxy: str | None = None,
    force_ipv4: bool = False,
) -> tuple[str, list[ToolCallRequest], str]:
    client_kwargs: dict[str, Any] = {
        "timeout": CODEX_TIMEOUT,
        "verify": verify,
        "proxy": proxy,
        "trust_env": True,
    }
    if force_ipv4:
        client_kwargs = {
            "timeout": CODEX_TIMEOUT,
            "transport": httpx.AsyncHTTPTransport(
                verify=verify,
                local_address="0.0.0.0",
                retries=1,
            ),
        }

    async with httpx.AsyncClient(**client_kwargs) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            if response.status_code != 200:
                text = await response.aread()
                raise CodexAPIError(
                    response.status_code,
                    _friendly_error(response.status_code, text.decode("utf-8", "ignore")),
                    retry_after=LLMProvider._extract_retry_after_from_headers(response.headers),
                )
            return await _consume_sse(response)


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI function-calling schema to Codex flat format."""
    converted: list[dict[str, Any]] = []
    for tool in tools:
        fn = (tool.get("function") or {}) if tool.get("type") == "function" else tool
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters") or {}
        converted.append({
            "type": "function",
            "name": name,
            "description": fn.get("description") or "",
            "parameters": params if isinstance(params, dict) else {},
        })
    return converted


def _convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system_prompt = ""
    input_items: list[dict[str, Any]] = []

    for idx, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            system_prompt = content if isinstance(content, str) else ""
            continue

        if role == "user":
            input_items.append(_convert_user_message(content))
            continue

        if role == "assistant":
            # Handle text first.
            if isinstance(content, str) and content:
                input_items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                        "status": "completed",
                        "id": f"msg_{idx}",
                    }
                )
            # Then handle tool calls.
            for tool_call in msg.get("tool_calls", []) or []:
                fn = tool_call.get("function") or {}
                call_id, item_id = _split_tool_call_id(tool_call.get("id"))
                call_id = call_id or f"call_{idx}"
                item_id = item_id or f"fc_{idx}"
                input_items.append(
                    {
                        "type": "function_call",
                        "id": item_id,
                        "call_id": call_id,
                        "name": fn.get("name"),
                        "arguments": fn.get("arguments") or "{}",
                    }
                )
            continue

        if role == "tool":
            call_id, _ = _split_tool_call_id(msg.get("tool_call_id"))
            output_text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output_text,
                }
            )
            continue

    # Purge orphaned function_call_outputs whose call_id has no matching
    # function_call.  This happens when the memory window clips mid-sequence
    # or the session contains tool calls from a different provider.
    valid_call_ids = {
        item["call_id"] for item in input_items if item.get("type") == "function_call"
    }
    input_items = [
        item
        for item in input_items
        if item.get("type") != "function_call_output" or item.get("call_id") in valid_call_ids
    ]

    return system_prompt, input_items


def _convert_user_message(content: Any) -> dict[str, Any]:
    if isinstance(content, str):
        return {"role": "user", "content": [{"type": "input_text", "text": content}]}
    if isinstance(content, list):
        converted: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                converted.append({"type": "input_text", "text": item.get("text", "")})
            elif item.get("type") == "image_url":
                url = (item.get("image_url") or {}).get("url")
                if url:
                    converted.append({"type": "input_image", "image_url": url, "detail": "auto"})
        if converted:
            return {"role": "user", "content": converted}
    return {"role": "user", "content": [{"type": "input_text", "text": ""}]}


def _split_tool_call_id(tool_call_id: Any) -> tuple[str, str | None]:
    if isinstance(tool_call_id, str) and tool_call_id:
        if "|" in tool_call_id:
            call_id, item_id = tool_call_id.split("|", 1)
            return call_id, item_id or None
        return tool_call_id, None
    return "call_0", None


def _prompt_cache_key(messages: list[dict[str, Any]]) -> str:
    raw = json.dumps(messages, ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _iter_sse(response: httpx.Response) -> AsyncGenerator[dict[str, Any], None]:
    buffer: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if buffer:
                data_lines = [l[5:].strip() for l in buffer if l.startswith("data:")]
                buffer = []
                if not data_lines:
                    continue
                data = "\n".join(data_lines).strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    yield json.loads(data)
                except Exception:
                    continue
            continue
        buffer.append(line)


async def _consume_sse(response: httpx.Response) -> tuple[str, list[ToolCallRequest], str]:
    content = ""
    tool_calls: list[ToolCallRequest] = []
    tool_call_buffers: dict[str, dict[str, Any]] = {}
    finish_reason = "stop"

    async for event in _iter_sse(response):
        event_type = event.get("type")
        if event_type == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                if not call_id:
                    continue
                tool_call_buffers[call_id] = {
                    "id": item.get("id") or "fc_0",
                    "name": item.get("name"),
                    "arguments": item.get("arguments") or "",
                }
        elif event_type == "response.output_text.delta":
            content += event.get("delta") or ""
        elif event_type == "response.function_call_arguments.delta":
            call_id = event.get("call_id")
            if call_id and call_id in tool_call_buffers:
                tool_call_buffers[call_id]["arguments"] += event.get("delta") or ""
        elif event_type == "response.function_call_arguments.done":
            call_id = event.get("call_id")
            if call_id and call_id in tool_call_buffers:
                tool_call_buffers[call_id]["arguments"] = event.get("arguments") or ""
        elif event_type == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                if not call_id:
                    continue
                buf = tool_call_buffers.get(call_id) or {}
                args_raw = buf.get("arguments") or item.get("arguments") or "{}"
                try:
                    args = json.loads(args_raw)
                except Exception:
                    args = {"raw": args_raw}
                tool_calls.append(
                    ToolCallRequest(
                        id=f"{call_id}|{buf.get('id') or item.get('id') or 'fc_0'}",
                        name=buf.get("name") or item.get("name"),
                        arguments=args,
                    )
                )
        elif event_type == "response.completed":
            status = (event.get("response") or {}).get("status")
            finish_reason = _map_finish_reason(status)
        elif event_type in {"error", "response.failed"}:
            raise RuntimeError(_event_error_message(event) or "Codex response failed")

    return content, tool_calls, finish_reason


_FINISH_REASON_MAP = {"completed": "stop", "incomplete": "length", "failed": "error", "cancelled": "error"}


def _map_finish_reason(status: str | None) -> str:
    return _FINISH_REASON_MAP.get(status or "completed", "stop")


def _friendly_error(status_code: int, raw: str) -> str:
    detail = _extract_error_message(raw) or raw
    if status_code == 429:
        return "ChatGPT usage quota exceeded or rate limit triggered. Please try again later."
    if status_code == 401:
        return "HTTP 401: Codex OAuth token was rejected. Run `mira onboard --wizard` and log in to OpenAI Codex again."
    if status_code == 403:
        return f"HTTP 403: Codex access was forbidden. {detail}".strip()
    return f"HTTP {status_code}: {detail}"


def _extract_error_message(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except Exception:
        return text
    if not isinstance(payload, dict):
        return text
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("message", "detail", "code", "type"):
            value = error.get(key)
            if value:
                return str(value)
    for key in ("message", "detail"):
        value = payload.get(key)
        if value:
            return str(value)
    return text


def _event_error_message(event: dict[str, Any]) -> str:
    error = event.get("error") or (event.get("response") or {}).get("error")
    if isinstance(error, dict):
        for key in ("message", "detail", "code", "type"):
            value = error.get(key)
            if value:
                return f"Codex response failed: {value}"
    if error:
        return f"Codex response failed: {error}"
    message = event.get("message")
    if message:
        return f"Codex response failed: {message}"
    return ""


def _format_exception(exc: Exception, proxy: str | None = None) -> str:
    message = str(exc).strip()
    message = message or type(exc).__name__
    if isinstance(exc, httpx.ConnectTimeout):
        if proxy:
            return f"{message} while connecting via proxy {_redact_proxy_url(proxy)}"
        return f"{message} while connecting to chatgpt.com (no explicit Mira proxy configured)"
    return message


def _error_kind(exc: Exception) -> str | None:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.NetworkError):
        return "connection"
    return None


def _redact_proxy_url(proxy: str) -> str:
    if "@" not in proxy:
        return proxy
    scheme, rest = proxy.split("://", 1) if "://" in proxy else ("", proxy)
    host = rest.rsplit("@", 1)[1]
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"
