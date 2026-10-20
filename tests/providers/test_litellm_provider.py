from __future__ import annotations

from types import SimpleNamespace

from mira_engine.providers.litellm_provider import LiteLLMProvider


def _fake_response(message: object) -> SimpleNamespace:
    choice = SimpleNamespace(message=message, finish_reason="stop")
    usage = SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5)
    return SimpleNamespace(choices=[choice], usage=usage)


def test_litellm_parse_preserves_reasoning_from_provider_fields() -> None:
    provider = LiteLLMProvider()
    message = SimpleNamespace(
        content="final answer",
        tool_calls=None,
        provider_specific_fields={
            "reasoning_content": "hidden reasoning",
            "thinking_blocks": [{"type": "thinking", "thinking": "hidden"}],
        },
    )

    result = provider._parse_response(_fake_response(message))

    assert result.content == "final answer"
    assert result.reasoning_content == "hidden reasoning"
    assert result.thinking_blocks == [{"type": "thinking", "thinking": "hidden"}]


async def test_litellm_chat_stream_forwards_deltas_and_rebuilds_response(monkeypatch) -> None:
    from litellm.types.utils import ModelResponseStream

    import mira_engine.providers.litellm_provider as litellm_module
    from mira_engine.providers.litellm_provider import LiteLLMProvider

    captured: dict[str, object] = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)

        async def gen():
            yield ModelResponseStream(id="x", model="gpt-4o", choices=[{"index": 0, "delta": {"role": "assistant", "content": "Hel"}, "finish_reason": None}])
            yield ModelResponseStream(id="x", model="gpt-4o", choices=[{"index": 0, "delta": {"content": "lo"}, "finish_reason": None}])
            yield ModelResponseStream(id="x", model="gpt-4o", choices=[{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{\"p\":"}}]}, "finish_reason": None}])
            yield ModelResponseStream(id="x", model="gpt-4o", choices=[{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}, "finish_reason": "tool_calls"}])

        return gen()

    monkeypatch.setattr(litellm_module, "acompletion", fake_acompletion)
    provider = LiteLLMProvider(api_key="sk-test", default_model="openai/gpt-4o")

    deltas: list[str] = []

    async def on_delta(text: str) -> None:
        deltas.append(text)

    response = await provider.chat_stream_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        on_content_delta=on_delta,
    )

    assert captured["stream"] is True
    assert deltas == ["Hel", "lo"]
    assert response.content == "Hello"
    assert response.finish_reason == "tool_calls"
    assert [(tc.name, tc.arguments) for tc in response.tool_calls] == [("read_file", {"p": 1})]


async def test_litellm_chat_stream_falls_back_to_blocking_call_when_stream_fails_to_open(monkeypatch) -> None:
    import mira_engine.providers.litellm_provider as litellm_module
    from mira_engine.providers.litellm_provider import LiteLLMProvider

    calls: list[bool] = []

    async def fake_acompletion(**kwargs):
        calls.append(bool(kwargs.get("stream")))
        if kwargs.get("stream"):
            raise RuntimeError("streaming unsupported")
        from litellm import ModelResponse
        return ModelResponse(choices=[{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}])

    monkeypatch.setattr(litellm_module, "acompletion", fake_acompletion)
    provider = LiteLLMProvider(api_key="sk-test", default_model="openai/gpt-4o")

    response = await provider.chat_stream(messages=[{"role": "user", "content": "hi"}])

    assert calls == [True, False]
    assert response.content == "ok"
    assert response.finish_reason == "stop"
