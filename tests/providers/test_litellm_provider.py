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


# ---------------------------------------------------------------------------
# Moonshot / Kimi temperature=1 enforcement
# ---------------------------------------------------------------------------


def test_moonshot_k2_family_gets_temperature_override() -> None:
    """All kimi-k2* and kimi-thinking* names should be clamped to temperature=1.0."""
    provider = LiteLLMProvider(default_model="kimi-k2-turbo-preview")
    for model in (
        "moonshot/kimi-k2",
        "moonshot/kimi-k2-turbo",
        "moonshot/kimi-k2-turbo-preview",
        "moonshot/kimi-k2.5",
        "moonshot/kimi-k2.5-turbo",
        "moonshot/kimi-thinking-preview",
    ):
        kwargs = {"temperature": 0.7}
        provider._apply_model_overrides(model, kwargs)
        assert kwargs["temperature"] == 1.0, f"{model} should be clamped to 1.0"


def test_moonshot_v1_models_keep_caller_temperature() -> None:
    """Plain moonshot-v1-* chat models accept any temperature; no override."""
    provider = LiteLLMProvider(default_model="moonshot-v1-128k")
    kwargs = {"temperature": 0.3}
    provider._apply_model_overrides("moonshot/moonshot-v1-128k", kwargs)
    assert kwargs["temperature"] == 0.3


@pytest.mark.parametrize(
    "message",
    [
        "MoonshotException - invalid temperature: only 1 is allowed for this model",
        "Bad temperature, only 1.0 is allowed",
        "temperature must be 1",
    ],
)
def test_is_temperature_one_required_recognizes_provider_messages(message: str) -> None:
    err = RuntimeError(message)
    assert LiteLLMProvider._is_temperature_one_required(err) is True


def test_is_temperature_one_required_ignores_unrelated_errors() -> None:
    assert LiteLLMProvider._is_temperature_one_required(RuntimeError("rate limit")) is False
    assert LiteLLMProvider._is_temperature_one_required(RuntimeError("temperature too high")) is False


@pytest.mark.asyncio
async def test_chat_retries_with_temperature_one_on_provider_rejection() -> None:
    """If the API rejects the request demanding temperature=1, retry once with 1.0."""
    success_message = SimpleNamespace(content="ok", tool_calls=None, provider_specific_fields=None)
    success_response = _fake_response(success_message)

    mock_acompletion = AsyncMock(
        side_effect=[
            RuntimeError(
                "litellm.BadRequestError: MoonshotException - "
                "invalid temperature: only 1 is allowed for this model"
            ),
            success_response,
        ]
    )

    with patch("mira_engine.providers.litellm_provider.acompletion", mock_acompletion):
        provider = LiteLLMProvider(default_model="moonshot/kimi-future-model")
        result = await provider.chat(
            messages=[{"role": "user", "content": "hello"}],
            model="moonshot/kimi-future-model",
            temperature=0.5,
        )

    assert result.finish_reason == "stop"
    assert result.content == "ok"
    assert mock_acompletion.await_count == 2
    assert mock_acompletion.await_args_list[0].kwargs["temperature"] == 0.5
    assert mock_acompletion.await_args_list[1].kwargs["temperature"] == 1.0


@pytest.mark.asyncio
async def test_chat_does_not_retry_when_already_at_temperature_one() -> None:
    """No infinite retries — if we already sent temperature=1.0, bubble the error up."""
    mock_acompletion = AsyncMock(
        side_effect=RuntimeError(
            "MoonshotException - invalid temperature: only 1 is allowed for this model"
        )
    )

    with patch("mira_engine.providers.litellm_provider.acompletion", mock_acompletion):
        provider = LiteLLMProvider(default_model="moonshot/kimi-k2-turbo")
        result = await provider.chat(
            messages=[{"role": "user", "content": "hello"}],
            model="moonshot/kimi-k2-turbo",
            temperature=0.5,  # will be overridden to 1.0 by registry → no retry
        )

    assert result.finish_reason == "error"
    assert "only 1 is allowed" in result.content
    assert mock_acompletion.await_count == 1
    assert mock_acompletion.await_args_list[0].kwargs["temperature"] == 1.0
