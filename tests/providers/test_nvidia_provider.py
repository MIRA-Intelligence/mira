from __future__ import annotations

import mira_engine.providers.nvidia_provider as nvidia_module
from mira_engine.providers.nvidia_provider import NvidiaProvider
from mira_engine.providers.registry import ProviderSpec


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


async def test_nvidia_provider_chat_stream_with_retry_ignores_content_delta(monkeypatch) -> None:
    """Regression: the streaming entrypoint must not forward the streaming-only
    ``on_content_delta`` callback to a non-streaming ``chat()`` implementation."""

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def fake_post(endpoint, payload):
        return (
            200,
            {},
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    monkeypatch.setattr(nvidia_module.asyncio, "to_thread", fake_to_thread)
    provider = NvidiaProvider(api_key="nvapi-test-key")
    monkeypatch.setattr(provider, "_post_json", fake_post)

    deltas: list[str] = []

    async def on_delta(text: str) -> None:
        deltas.append(text)

    response = await provider.chat_stream_with_retry(
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=128,
        temperature=0.2,
        reasoning_effort=None,
        on_content_delta=on_delta,
    )

    assert response.content == "ok"
    assert response.finish_reason == "stop"
    # A non-streaming provider produces no incremental deltas.
    assert deltas == []


def _spec_with_overrides(overrides) -> ProviderSpec:
    return ProviderSpec(
        name="nvidia",
        keywords=("nvidia", "nemotron"),
        env_key="NVIDIA_API_KEY",
        model_overrides=overrides,
        is_direct=True,
    )


async def test_nvidia_provider_applies_registry_model_overrides(monkeypatch) -> None:
    """A registry override can force a required temperature for an NVIDIA model."""

    monkeypatch.setattr(
        nvidia_module,
        "find_by_name",
        lambda name: _spec_with_overrides((("nemotron", {"temperature": 1.0}),)),
    )
    provider = NvidiaProvider(api_key="nvapi-test-key")

    payload = provider._build_payload(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        tool_choice=None,
        model="nvidia/nvidia/llama-3.3-nemotron-super-49b-v1.5",
        max_tokens=64,
        temperature=0.2,
        reasoning_effort=None,
    )

    assert payload["temperature"] == 1.0


async def test_nvidia_provider_override_can_drop_temperature(monkeypatch) -> None:
    """A ``None`` override drops a parameter the model rejects."""

    monkeypatch.setattr(
        nvidia_module,
        "find_by_name",
        lambda name: _spec_with_overrides((("nemotron", {"temperature": None}),)),
    )
    provider = NvidiaProvider(api_key="nvapi-test-key")

    payload = provider._build_payload(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        tool_choice=None,
        model="nvidia/nvidia/llama-3.3-nemotron-super-49b-v1.5",
        max_tokens=64,
        temperature=0.2,
        reasoning_effort=None,
    )

    assert "temperature" not in payload
