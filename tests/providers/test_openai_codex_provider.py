from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

import mira_plugins.openai_codex.provider as codex_provider
from mira_plugins.openai_codex.provider import (
    OpenAICodexProvider,
    _consume_sse,
    _error_kind,
    _extract_error_message,
    _format_exception,
    _friendly_error,
    _request_codex,
)


class _FakeSSE:
    def __init__(self, events: list[dict]):
        self._lines: list[str] = []
        for event in events:
            self._lines.extend([
                f"event: {event['type']}",
                f"data: {json.dumps(event)}",
                "",
            ])

    async def aiter_lines(self):
        for line in self._lines:
            yield line


def test_format_exception_includes_type_for_blank_timeout() -> None:
    exc = httpx.ReadTimeout("")

    assert _format_exception(exc) == "ReadTimeout"
    assert _error_kind(exc) == "timeout"


def test_format_exception_adds_connect_timeout_proxy_hint() -> None:
    exc = httpx.ConnectTimeout("")

    assert _format_exception(exc) == (
        "ConnectTimeout while connecting to chatgpt.com (no explicit Mira proxy configured)"
    )
    assert _format_exception(exc, "http://user:pass@127.0.0.1:7890") == (
        "ConnectTimeout while connecting via proxy http://***@127.0.0.1:7890"
    )


def test_friendly_error_extracts_json_detail() -> None:
    raw = json.dumps({"detail": "Unauthorized"})

    assert _extract_error_message(raw) == "Unauthorized"
    assert "OAuth token was rejected" in _friendly_error(401, raw)


@pytest.mark.asyncio
async def test_consume_sse_uses_response_failed_message() -> None:
    response = _FakeSSE([
        {
            "type": "response.failed",
            "response": {"error": {"message": "model is not available"}},
        }
    ])

    with pytest.raises(RuntimeError, match="model is not available"):
        await _consume_sse(response)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_request_codex_retries_ipv4_after_connect_timeout(monkeypatch) -> None:
    calls: list[bool] = []

    async def fake_request_once(*args, force_ipv4: bool = False, **kwargs):
        calls.append(force_ipv4)
        if not force_ipv4:
            raise httpx.ConnectTimeout("")
        return "ok", [], "stop", {}

    monkeypatch.setattr(codex_provider, "_request_codex_once", fake_request_once)

    result = await _request_codex("https://example.test", {}, {}, verify=True)

    assert result == ("ok", [], "stop", {})
    assert calls == [False, True]


@pytest.mark.asyncio
async def test_openai_codex_chat_prepares_oauth_state_before_getting_token(monkeypatch) -> None:
    calls: list[str] = []

    def fake_prepare() -> None:
        calls.append("prepare")

    async def fake_request_codex(*args, **kwargs):
        return "ok", [], "stop", {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "cached_tokens": 80}

    monkeypatch.setattr(codex_provider, "ensure_oauth_state_dirs_for_runtime", fake_prepare)
    monkeypatch.setattr(
        codex_provider,
        "get_codex_token",
        lambda: SimpleNamespace(access="access-token", account_id="account-id"),
    )
    monkeypatch.setattr(codex_provider, "_request_codex", fake_request_codex)

    provider = OpenAICodexProvider()
    response = await provider.chat([{"role": "user", "content": "hi"}])

    assert response.content == "ok"
    assert response.usage["cached_tokens"] == 80
    assert calls == ["prepare"]


@pytest.mark.parametrize("cached", [0, 80, None])
async def test_codex_sse_retains_usage(cached):
    usage = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
             "input_tokens_details": {"cached_tokens": cached}}
    response = _FakeSSE([{"type": "response.completed", "response": {"status": "completed", "usage": usage}}])
    _, _, _, result = await _consume_sse(response)
    assert result["prompt_tokens"] == 100
    if cached is None:
        assert "cached_tokens" not in result
    else:
        assert result["cached_tokens"] == cached
