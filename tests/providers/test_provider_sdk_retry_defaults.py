from types import SimpleNamespace
from unittest.mock import Mock, patch

from mira_engine.providers.anthropic_provider import AnthropicProvider
from mira_engine.providers.azure_openai_provider import AzureOpenAIProvider
from mira_engine.providers.openai_compat_provider import OpenAICompatProvider


def test_openai_compat_disables_sdk_retries_by_default() -> None:
    with patch("mira_engine.providers.openai_compat_provider.AsyncOpenAI") as mock_client:
        OpenAICompatProvider(api_key="sk-test", default_model="gpt-4o")

    kwargs = mock_client.call_args.kwargs
    assert kwargs["max_retries"] == 0


def test_anthropic_disables_sdk_retries_by_default() -> None:
    fake_client = Mock()
    fake_anthropic = SimpleNamespace(AsyncAnthropic=fake_client)
    with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
        AnthropicProvider(api_key="sk-test", default_model="claude-sonnet-4-5")

    kwargs = fake_client.call_args.kwargs
    assert kwargs["max_retries"] == 0


def test_azure_openai_disables_sdk_retries_by_default() -> None:
    with patch("mira_engine.providers.azure_openai_provider.AsyncOpenAI") as mock_client:
        AzureOpenAIProvider(
            api_key="sk-test",
            api_base="https://example.openai.azure.com",
            default_model="gpt-4.1",
        )

    kwargs = mock_client.call_args.kwargs
    assert kwargs["max_retries"] == 0
