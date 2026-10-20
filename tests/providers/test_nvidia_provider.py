from __future__ import annotations

import json

import httpx

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


def _sse_transport(events: list[str], *, status: int = 200, content_type: str = "text/event-stream"):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        captured["accept"] = request.headers.get("accept")
        body = "".join(f"data: {event}\n\n" for event in events)
        return httpx.Response(status, headers={"content-type": content_type}, content=body.encode("utf-8"))

    return httpx.MockTransport(handler), captured


def _install_transport(provider: NvidiaProvider, transport: httpx.MockTransport) -> None:
    provider._http = httpx.AsyncClient(transport=transport)


async def test_nvidia_provider_streams_content_deltas() -> None:
    transport, captured = _sse_transport([
        json.dumps({"choices": [{"delta": {"role": "assistant", "content": "Hel"}, "finish_reason": None}]}),
        json.dumps({"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]}),
        json.dumps({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}),
        "[DONE]",
    ])
    provider = NvidiaProvider(api_key="nvapi-test-key")
    _install_transport(provider, transport)

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

    assert deltas == ["Hel", "lo"]
    assert response.content == "Hello"
    assert response.finish_reason == "stop"
    assert response.usage.get("prompt_tokens") == 3
    assert captured["payload"]["stream"] is True
    assert captured["accept"] == "text/event-stream"


async def test_nvidia_provider_stream_assembles_tool_calls() -> None:
    transport, _ = _sse_transport([
        json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "read_file", "arguments": ""}}]}}]}),
        json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"path\": "}}]}}]}),
        json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\"a.txt\"}"}}]}, "finish_reason": "tool_calls"}]}),
        "[DONE]",
    ])
    provider = NvidiaProvider(api_key="nvapi-test-key")
    _install_transport(provider, transport)

    response = await provider.chat_stream(messages=[{"role": "user", "content": "go"}])

    assert response.finish_reason == "tool_calls"
    assert [(tc.id, tc.name, tc.arguments) for tc in response.tool_calls] == [("call_1", "read_file", {"path": "a.txt"})]


async def test_nvidia_provider_stream_reports_http_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"content-type": "application/json", "Retry-After": "7"},
                              json={"error": {"type": "rate_limit_error", "message": "Too many requests"}})

    provider = NvidiaProvider(api_key="nvapi-test-key")
    _install_transport(provider, httpx.MockTransport(handler))

    response = await provider.chat_stream(messages=[{"role": "user", "content": "go"}])

    assert response.finish_reason == "error"
    assert response.error_status_code == 429
    assert response.retry_after == 7


async def test_nvidia_provider_stream_falls_back_to_blocking_chat_when_stream_cannot_open(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def fake_post(endpoint, payload):
        assert payload["stream"] is False
        return 200, {}, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(nvidia_module.asyncio, "to_thread", fake_to_thread)
    provider = NvidiaProvider(api_key="nvapi-test-key")
    _install_transport(provider, httpx.MockTransport(handler))
    monkeypatch.setattr(provider, "_post_json", fake_post)

    response = await provider.chat_stream(messages=[{"role": "user", "content": "hello"}])

    assert response.content == "ok"
    assert response.finish_reason == "stop"


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
