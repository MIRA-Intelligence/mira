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
