from __future__ import annotations

import mira_engine.providers.registry as registry
from mira_engine.providers.registry import ProviderSpec, model_overrides_for


def test_global_rule_drops_temperature_for_gpt5_on_any_provider() -> None:
    # gpt-5 served through a gateway should still have temperature dropped,
    # because the rule is global rather than tied to the openai spec.
    assert model_overrides_for("openrouter/openai/gpt-5") == {"temperature": None}
    assert model_overrides_for("gpt-5-chat") == {"temperature": None}


def test_global_rule_matches_short_form_after_slash() -> None:
    assert model_overrides_for("nvidia/openai/o3-mini") == {"temperature": None}


def test_provider_spec_override_wins_over_global(monkeypatch) -> None:
    # A provider-specific rule layered on top of a global one wins on conflict.
    fake_rules = (("*custommodel*", {"temperature": None}),)
    monkeypatch.setattr(registry, "MODEL_PARAM_RULES", fake_rules)

    spec = ProviderSpec(
        name="acme",
        keywords=("custommodel",),
        env_key="",
        model_overrides=(("custommodel", {"temperature": 1.0}),),
    )

    assert model_overrides_for("acme/custommodel-v2", spec) == {"temperature": 1.0}


def test_no_match_returns_empty() -> None:
    assert model_overrides_for("gpt-4o") == {}
    assert model_overrides_for(None) == {}


def test_substring_pattern_still_matches() -> None:
    # The existing Moonshot Kimi K2.5 rule uses a plain substring pattern.
    assert model_overrides_for("moonshot/kimi-k2.5") == {"temperature": 1.0}
