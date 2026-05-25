from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

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


@pytest.mark.asyncio
async def test_litellm_retries_deepseek_after_backfilling_reasoning_content() -> None:
    provider = LiteLLMProvider(default_model="deepseek/deepseek-v4-pro")
    deepseek_error = Exception(
        "litellm.BadRequestError: DeepseekException - "
        '{"error":{"message":"The `reasoning_content` in the thinking mode '
        'must be passed back to the API."}}'
    )
    ok_message = SimpleNamespace(content="ok", tool_calls=None)
    mock_acompletion = AsyncMock(side_effect=[deepseek_error, _fake_response(ok_message)])

    messages = [
        {"role": "user", "content": "start"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_original_123",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_original_123",
            "name": "read_file",
            "content": "done",
        },
        {"role": "user", "content": "continue"},
    ]

    with patch("mira_engine.providers.litellm_provider.acompletion", mock_acompletion):
        result = await provider.chat(messages=messages, model="deepseek/deepseek-v4-pro")

    assert result.content == "ok"
    assert mock_acompletion.await_count == 2
    first_messages = mock_acompletion.await_args_list[0].kwargs["messages"]
    retry_messages = mock_acompletion.await_args_list[1].kwargs["messages"]
    assert "reasoning_content" not in first_messages[1]
    assert retry_messages[1]["reasoning_content"] == " "
