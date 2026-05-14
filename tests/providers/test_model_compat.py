"""Tests for the shared model-compatibility helper."""

from __future__ import annotations

from mira_engine.providers.model_compat import (
    TEMPERATURE_UNSUPPORTED_MODEL_TOKENS,
    model_supports_temperature,
)


def test_ordinary_models_support_temperature():
    assert model_supports_temperature("anthropic/claude-sonnet-4-5") is True
    assert model_supports_temperature("openai/gpt-4o") is True
    assert model_supports_temperature("gpt-4.1-mini") is True


def test_empty_or_none_model_defaults_to_supported():
    # We don't want to silently strip temperature for unknown models —
    # only an explicit token match disables it.
    assert model_supports_temperature(None) is True
    assert model_supports_temperature("") is True


def test_claude_opus_4_7_is_blocked_under_every_provider_prefix():
    # Bug 1: in production we observed `azure/anthropic/claude-opus-4-7`
    # rejecting `temperature` with
    #   invalid_request_error: `temperature` is deprecated for this model.
    # The token rule must catch the model under every prefix variant.
    assert model_supports_temperature("claude-opus-4-7") is False
    assert model_supports_temperature("anthropic/claude-opus-4-7") is False
    assert model_supports_temperature("azure/anthropic/claude-opus-4-7") is False
    assert model_supports_temperature("openrouter/anthropic/claude-opus-4-7") is False
    assert model_supports_temperature("Azure/Anthropic/Claude-Opus-4-7") is False


def test_blocklist_is_a_frozenset():
    # Guards against accidental in-place mutation from another module.
    assert isinstance(TEMPERATURE_UNSUPPORTED_MODEL_TOKENS, frozenset)
    assert "claude-opus-4-7" in TEMPERATURE_UNSUPPORTED_MODEL_TOKENS
