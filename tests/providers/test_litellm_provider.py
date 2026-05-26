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
