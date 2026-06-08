from __future__ import annotations

import mira_engine.providers.nvidia_provider as nvidia_module
from mira_engine.providers.nvidia_provider import NvidiaProvider


def test_nvidia_provider_normalizes_host_only_base_url() -> None:
    provider = NvidiaProvider(
        api_key="nvapi-test-key",
        api_base="https://inference-api.nvidia.com",
    )

    assert provider.api_base == "https://inference-api.nvidia.com/v1"


async def test_nvidia_provider_posts_direct_chat_completion_payload(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def fake_post(endpoint, payload):
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        return (
            200,
            {"Content-Type": "application/json"},
            {
                "choices": [
                    {
                        "message": {"content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            },
        )

    monkeypatch.setattr(nvidia_module.asyncio, "to_thread", fake_to_thread)
    provider = NvidiaProvider(
        api_key="nvapi-test-key",
        api_base="https://inference-api.nvidia.com/v1/chat/completions",
        default_model="nvidia/deepseek-ai/deepseek-v4-pro",
    )
    monkeypatch.setattr(provider, "_post_json", fake_post)

    response = await provider.chat(
        messages=[{"role": "user", "content": "hello"}],
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {}}}],
        max_tokens=128,
        temperature=0.2,
        reasoning_effort="medium",
    )

    assert response.content == "ok"
    assert response.usage == {
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
    }
    assert captured["endpoint"] == "https://inference-api.nvidia.com/v1/chat/completions"
    assert captured["payload"] == {
        "model": "nvidia/deepseek-ai/deepseek-v4-pro",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
        "max_tokens": 128,
        "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {}}}],
        "tool_choice": "auto",
    }


async def test_nvidia_provider_parses_tool_calls(monkeypatch) -> None:
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def fake_post(endpoint, payload):
        return (
            200,
            {},
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_123",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"query":"nvidia"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        )

    monkeypatch.setattr(nvidia_module.asyncio, "to_thread", fake_to_thread)
    provider = NvidiaProvider(api_key="nvapi-test-key")
    monkeypatch.setattr(provider, "_post_json", fake_post)

    response = await provider.chat(messages=[{"role": "user", "content": "hello"}])

    assert response.finish_reason == "tool_calls"
    assert response.tool_calls[0].id == "call_123"
    assert response.tool_calls[0].name == "lookup"
    assert response.tool_calls[0].arguments == {"query": "nvidia"}


async def test_nvidia_provider_returns_structured_http_error(monkeypatch) -> None:
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def fake_post(endpoint, payload):
        return (
            429,
            {"Retry-After": "20"},
            {
                "error": {
                    "type": "rate_limit_exceeded",
                    "code": "rate_limit_exceeded",
                    "message": "Too many requests",
                }
            },
        )

    monkeypatch.setattr(nvidia_module.asyncio, "to_thread", fake_to_thread)
    provider = NvidiaProvider(api_key="nvapi-test-key")
    monkeypatch.setattr(provider, "_post_json", fake_post)

    response = await provider.chat(messages=[{"role": "user", "content": "hello"}])

    assert response.finish_reason == "error"
    assert response.error_status_code == 429
    assert response.error_type == "rate_limit_exceeded"
    assert response.error_code == "rate_limit_exceeded"
    assert response.retry_after == 20.0
    assert response.error_retry_after_s == 20.0
