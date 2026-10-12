from mira_engine.modules.profiles import (
    normalize_agent_profile,
    profile_system_instructions,
)


def test_profile_policy_preserves_custom_constraints_without_legacy_orchestration() -> None:
    research = profile_system_instructions("research")
    engineer = profile_system_instructions("engineer")
    team = profile_system_instructions("team")

    assert "Scientific Tenacity" in research
    assert "Proof of Causality" in research
    assert "Scientific Record Format" in research
    assert "results-oriented Machine Learning Engineering Assistant" in engineer
    assert "Implementation Record Format" in engineer
    assert "Bounded Debate Protocol" in team
    assert "Delegation Discipline" in team

    for policy in (research, engineer, team):
        assert "Interactive Plan Mode" not in policy
        assert "Scheduled Reminders" not in policy
        assert "set_plan` tool" not in policy
        assert "task_plan.json under" not in policy


def test_profile_normalization_uses_research_as_compatible_default() -> None:
    assert normalize_agent_profile(" ENGINEER ") == "engineer"
    assert normalize_agent_profile("team") == "team"
    assert normalize_agent_profile("unknown") == "research"
    assert normalize_agent_profile(None) == "research"
