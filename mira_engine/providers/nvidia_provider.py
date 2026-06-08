"""Direct NVIDIA NIM chat-completions provider."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import json_repair

from mira_engine.providers.base import LLMProvider, LLMResponse, ToolCallRequest

_DEFAULT_API_BASE = "https://inference-api.nvidia.com/v1"
_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"
_ALLOWED_MSG_KEYS = frozenset({"role", "content", "tool_calls", "tool_call_id", "name"})
_DEFAULT_TIMEOUT_S = 120.0
_DEFAULT_RATE_LIMIT_RETRIES = 2
_DEFAULT_RATE_LIMIT_BACKOFF_S = 15.0


def _normalize_base_url(value: str | None) -> str:
    clean = (value or _DEFAULT_API_BASE).strip().rstrip("/")
    if clean.endswith(_CHAT_COMPLETIONS_SUFFIX):
        clean = clean[: -len(_CHAT_COMPLETIONS_SUFFIX)].rstrip("/")
    parsed = urllib.parse.urlparse(clean)
    if parsed.netloc == "inference-api.nvidia.com" and parsed.path in {"", "/"}:
        clean = clean.rstrip("/") + "/v1"
    return clean or _DEFAULT_API_BASE


def _chat_completions_url(api_base: str) -> str:
    return urllib.parse.urljoin(api_base.rstrip("/") + "/", "chat/completions")


def _timeout_seconds() -> float:
    try:
        return float(os.environ.get("MIRA_NVIDIA_TIMEOUT_S", _DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_S


def _rate_limit_retries() -> int:
    try:
        return max(0, int(os.environ.get("MIRA_NVIDIA_RATE_LIMIT_RETRIES", _DEFAULT_RATE_LIMIT_RETRIES)))
    except (TypeError, ValueError):
        return _DEFAULT_RATE_LIMIT_RETRIES


def _rate_limit_backoff_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("MIRA_NVIDIA_RATE_LIMIT_BACKOFF_S", _DEFAULT_RATE_LIMIT_BACKOFF_S)))
    except (TypeError, ValueError):
        return _DEFAULT_RATE_LIMIT_BACKOFF_S


def _retry_delay_seconds(headers: Any, attempt: int) -> float:
    retry_after = headers.get("Retry-After") if headers else None
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            pass
    return _rate_limit_backoff_seconds() * (2**attempt)


def _try_parse_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _is_retryable_rate_limit(status: int, body: Any) -> bool:
    if status != 429:
        return False
    text = json.dumps(body, ensure_ascii=False).lower()
    return (
        "engineoverloaded" in text
        or "too many requests" in text
        or "throttling_error" in text
        or "ratelimiterror" in text
        or "rate_limit" in text
    )


def _is_ssl_eof_error(exc: Exception) -> bool:
    text = repr(exc)
    return "EOF occurred in violation of protocol" in text or "SSLEOFError" in text


def _secure_temp_file(content: bytes) -> str:
    fd, path = tempfile.mkstemp(prefix="mira-nvidia-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
    except Exception:
        os.close(fd)
        raise
    return path


def _curl_json_post(
    endpoint: str,
    payload: dict[str, Any],
    api_key: str,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, dict[str, str], str]:
    body_file = _secure_temp_file(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    out_file = _secure_temp_file(b"")
    config_lines = [
        f'url = "{endpoint}"',
        'request = "POST"',
        f'header = "Authorization: Bearer {api_key}"',
        'header = "Content-Type: application/json"',
        'header = "Accept: application/json"',
        f'data-binary = "@{body_file}"',
        f'output = "{out_file}"',
        f'max-time = {timeout}',
        'write-out = "\\n%{http_code}\\n%{content_type}"',
        "silent",
        "show-error",
    ]
    for key, value in headers.items():
        if key.lower() in {"authorization", "content-type", "accept"}:
            continue
        config_lines.append(f'header = "{key}: {value}"')
    config_file = _secure_temp_file("\n".join(config_lines).encode("utf-8"))
    try:
        proc = subprocess.run(
            ["curl", "--http2", "--config", config_file],
            capture_output=True,
            text=True,
            timeout=timeout + 10,
        )
        with open(out_file, "rb") as handle:
            raw = handle.read().decode("utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "curl failed")
        lines = proc.stdout.splitlines()
        status = int(lines[-2]) if len(lines) >= 2 and lines[-2].isdigit() else 0
        content_type = lines[-1] if lines else ""
        return status, {"Content-Type": content_type}, raw
    finally:
        for path in (body_file, out_file, config_file):
            try:
                os.unlink(path)
            except OSError:
                pass


class NvidiaProvider(LLMProvider):
    """Call NVIDIA's public inference API without SDK compatibility shims."""

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "nvidia/nvidia/llama-3.3-nemotron-super-49b-v1.5",
        extra_headers: dict[str, str] | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.api_base = _normalize_base_url(api_base)
        self.extra_headers = extra_headers or {}

    def get_default_model(self) -> str:
        return self.default_model

    def _sanitize_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sanitized = self._sanitize_request_messages(
            self._sanitize_empty_content(messages),
            _ALLOWED_MSG_KEYS,
        )
        id_map: dict[str, str] = {}

        def map_id(value: Any) -> Any:
            if not isinstance(value, str):
                return value
            if len(value) == 9 and value.isalnum():
                return value
            return id_map.setdefault(value, hashlib.sha1(value.encode()).hexdigest()[:9])

        for clean in sanitized:
            if isinstance(clean.get("tool_calls"), list):
                tool_calls = []
                for tc in clean["tool_calls"]:
                    if not isinstance(tc, dict):
                        tool_calls.append(tc)
                        continue
                    tc_clean = dict(tc)
                    tc_clean["id"] = map_id(tc_clean.get("id"))
                    tool_calls.append(tc_clean)
                clean["tool_calls"] = tool_calls
            if clean.get("tool_call_id"):
                clean["tool_call_id"] = map_id(clean["tool_call_id"])
        return sanitized

    @staticmethod
    def _supports_temperature(model_name: str, reasoning_effort: str | None = None) -> bool:
        if reasoning_effort and reasoning_effort.lower() != "none":
            return False
        lower = model_name.lower()
        if "claude-opus-4" in lower or "claude-4-opus" in lower:
            return False
        return True

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: Any | None,
        model: str | None,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None,
    ) -> dict[str, Any]:
        model_name = model or self.default_model
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": self._sanitize_messages(messages),
            "stream": False,
            "max_tokens": max(1, int(max_tokens)),
        }
        if self._supports_temperature(model_name, reasoning_effort):
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice if tool_choice is not None else "auto"
        return payload

    def _request_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key or ''}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        headers.update(self.extra_headers)
        return headers

    def _post_json(
        self,
        endpoint: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, str], Any]:
        headers = self._request_headers()
        timeout = _timeout_seconds()
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        attempt = 0
        while True:
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    return resp.getcode(), dict(resp.headers.items()), _try_parse_json(raw)
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                parsed = _try_parse_json(raw)
                if _is_retryable_rate_limit(exc.code, parsed) and attempt < _rate_limit_retries():
                    time.sleep(_retry_delay_seconds(exc.headers, attempt))
                    attempt += 1
                    continue
                return exc.code, dict(exc.headers.items()), parsed
            except Exception as exc:
                if _is_ssl_eof_error(exc):
                    status, curl_headers, raw = _curl_json_post(
                        endpoint,
                        payload,
                        self.api_key or "",
                        headers,
                        timeout,
                    )
                    return status, curl_headers, _try_parse_json(raw)
                raise

    @staticmethod
    def _extract_text_content(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("input_text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts) or None
        return str(value)

    @staticmethod
    def _extract_usage(response: Any) -> dict[str, int]:
        if not isinstance(response, dict) or not isinstance(response.get("usage"), dict):
            return {}
        usage = response["usage"]
        return {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }

    @classmethod
    def _parse_success(cls, response: Any) -> LLMResponse:
        if not isinstance(response, dict):
            return LLMResponse(content=str(response), finish_reason="stop")
        choices = response.get("choices") or []
        if not choices:
            content = cls._extract_text_content(response.get("content") or response.get("output_text"))
            if content:
                return LLMResponse(
                    content=content,
                    finish_reason=str(response.get("finish_reason") or "stop"),
                    usage=cls._extract_usage(response),
                )
            return LLMResponse(content="Error: NVIDIA API returned empty choices.", finish_reason="error")

        choice0 = choices[0] if isinstance(choices[0], dict) else {}
        message = choice0.get("message") if isinstance(choice0.get("message"), dict) else {}
        content = cls._extract_text_content(message.get("content"))
        finish_reason = str(choice0.get("finish_reason") or "stop")
        reasoning_content = cls._extract_text_content(message.get("reasoning_content") or message.get("reasoning"))

        tool_calls = []
        for raw_tool_call in message.get("tool_calls") or []:
            if not isinstance(raw_tool_call, dict):
                continue
            fn = raw_tool_call.get("function") if isinstance(raw_tool_call.get("function"), dict) else {}
            args = fn.get("arguments", {})
            if isinstance(args, str):
                args = json_repair.loads(args)
            tool_calls.append(
                ToolCallRequest(
                    id=str(raw_tool_call.get("id") or ""),
                    name=str(fn.get("name") or ""),
                    arguments=args if isinstance(args, dict) else {},
                )
            )

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=cls._extract_usage(response),
            reasoning_content=reasoning_content,
        )

    @classmethod
    def _parse_error(
        cls,
        *,
        status_code: int,
        headers: dict[str, str],
        body: Any,
    ) -> LLMResponse:
        error_type, error_code = LLMProvider._extract_error_type_code(body)
        retry_after = LLMProvider._extract_retry_after_from_headers(headers)
        body_text = json.dumps(body, ensure_ascii=False) if isinstance(body, (dict, list)) else str(body)
        content = f"Error: NVIDIA API returned HTTP {status_code}: {body_text[:500]}"
        return LLMResponse(
            content=content,
            finish_reason="error",
            retry_after=retry_after,
            error_status_code=status_code,
            error_type=error_type,
            error_code=error_code,
            error_retry_after_s=retry_after,
        )

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
        if not self.api_key:
            return LLMResponse(
                content="Error: NVIDIA API key is required.",
                finish_reason="error",
                error_type="missing_api_key",
            )
        endpoint = _chat_completions_url(self.api_base)
        payload = self._build_payload(
            messages,
            tools,
            tool_choice,
            model,
            max_tokens,
            temperature,
            reasoning_effort,
        )
        try:
            status, headers, body = await asyncio.to_thread(self._post_json, endpoint, payload)
        except Exception as exc:
            error_name = exc.__class__.__name__.lower()
            error_kind = "timeout" if "timeout" in error_name else "connection"
            return LLMResponse(
                content=f"Error calling NVIDIA API: {exc}",
                finish_reason="error",
                error_kind=error_kind,
            )

        if 200 <= status < 300:
            if isinstance(body, dict) and "error" in body and "choices" not in body:
                return self._parse_error(status_code=status, headers=headers, body=body)
            return self._parse_success(body)
        return self._parse_error(status_code=status, headers=headers, body=body)
