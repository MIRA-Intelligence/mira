import pytest

from medpilot.config.schema import AgentDefaults, normalize_model_candidates, primary_model_candidate


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, []),
        ("m1", ["m1"]),
        (["a", "b"], ["a", "b"]),
        (["a", "a", "b"], ["a", "b"]),
        (["  x  ", "y"], ["x", "y"]),
        (["", "  ", "ok"], ["ok"]),
        ([], []),
    ],
)
def test_normalize_model_candidates(value, expected) -> None:
    assert normalize_model_candidates(value) == expected


@pytest.mark.parametrize(
    ("value", "fallback", "expected"),
    [
        ("first", None, "first"),
        (["a", "b"], None, "a"),
        (None, "fb", "fb"),
        ([], "fb", "fb"),
        (None, None, None),
    ],
)
def test_primary_model_candidate(value, fallback, expected) -> None:
    assert primary_model_candidate(value, fallback=fallback) == expected


def test_agent_defaults_prepends_provider_prefix() -> None:
    """Test that model without '/' gets provider prefix."""
    defaults = AgentDefaults.model_validate({"provider": "openrouter", "model": "claude-3-opus"})
    assert defaults.model == "openrouter/claude-3-opus"
    assert defaults.model_candidates == ["openrouter/claude-3-opus"]


def test_agent_defaults_prepends_provider_prefix_to_tiers() -> None:
    """Test that tier models without '/' get provider prefix."""
    defaults = AgentDefaults.model_validate(
        {
            "provider": "openrouter",
            "model": "claude-3-opus",
            "smallModel": "gpt-4o-mini",
        }
    )
    assert defaults.small_model == "openrouter/gpt-4o-mini"
    assert defaults.small_model_candidates == ["openrouter/gpt-4o-mini"]


def test_agent_defaults_skips_prefix_if_already_present() -> None:
    """Test that models already containing '/' are not prefixed."""
    defaults = AgentDefaults.model_validate({"provider": "openrouter", "model": "anthropic/claude-3"})
    assert defaults.model == "anthropic/claude-3"


def test_agent_defaults_skips_prefix_if_provider_auto() -> None:
    """Test that models are not prefixed if provider is 'auto'."""
    defaults = AgentDefaults.model_validate({"provider": "auto", "model": "gpt-4o"})
    assert defaults.model == "gpt-4o"


def test_agent_defaults_skips_prefix_if_provider_has_no_litellm_prefix() -> None:
    """Test that models are not prefixed if the provider has an empty litellm_prefix."""
    defaults = AgentDefaults.model_validate({"provider": "openai", "model": "gpt-4o"})
    # openai spec has litellm_prefix=""
    assert defaults.model == "gpt-4o"
