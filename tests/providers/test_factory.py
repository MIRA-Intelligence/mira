from __future__ import annotations

import pytest

from medpilot.config.schema import Config
from medpilot.providers.factory import make_provider


def test_make_provider_raises_clear_error_when_provider_cannot_be_matched() -> None:
    config = Config()
    config.agents.defaults.model = "unknown-model-name"
    config.agents.defaults.provider = "auto"

    with pytest.raises(ValueError, match="Unable to match provider for model 'unknown-model-name'"):
        make_provider(config)


def test_make_provider_raises_error_for_custom_without_api_base() -> None:
    """Custom provider requires explicit apiBase configuration."""
    config = Config()
    config.agents.defaults.model = "custom/my-model"
    config.agents.defaults.provider = "custom"
    config.providers.custom.api_key = "test-key"
    # Intentionally not setting api_base

    with pytest.raises(ValueError, match="Custom provider requires.*apiBase"):
        make_provider(config)


def test_make_provider_succeeds_for_custom_with_api_base() -> None:
    """Custom provider works when apiBase is configured."""
    config = Config()
    config.agents.defaults.model = "custom/my-model"
    config.agents.defaults.provider = "custom"
    config.providers.custom.api_key = "test-key"
    config.providers.custom.api_base = "http://localhost:8000/v1"

    # Should not raise
    provider = make_provider(config)
    assert provider is not None
