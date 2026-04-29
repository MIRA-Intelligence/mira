"""Smoke tests for the new ``mira research`` CLI subcommand.

The bulk of orchestration logic is exercised in
``tests/test_research_loop_core.py``. These tests focus on the CLI entry
point itself: command registration and the ``--mode/--profile/--max-tokens``
flag → ``InboundMessage.metadata`` translation.
"""

from __future__ import annotations

from typer.testing import CliRunner

from mira_engine.cli.commands import _build_research_inbound_metadata, app

runner = CliRunner()


def test_research_command_registered() -> None:
    """``mira research`` shows up in CLI help."""
    result = runner.invoke(app, ["research", "--help"])
    assert result.exit_code == 0, result.output
    assert "Interact with the research-flavoured agent" in result.output
    assert "--mode" in result.output
    assert "--profile" in result.output
    assert "--max-tokens" in result.output
    assert "--max-experiments" in result.output


def test_research_metadata_with_all_flags() -> None:
    """Every research flag maps to the expected metadata keys."""
    metadata = _build_research_inbound_metadata(
        mode="auto",
        profile="engineer",
        max_tokens=50_000,
        max_experiments=8,
        project_dir="/tmp/PRJ-1",
    )
    assert metadata["run_mode"] == "auto"
    assert metadata["agent_profile"] == "engineer"
    assert metadata["project_dir"] == "/tmp/PRJ-1"
    policy = metadata["automation_policy"]
    assert isinstance(policy, dict)
    assert policy["maxTokens"] == 50_000
    assert policy["maxExperiments"] == 8
    # ResearchAgentLoop._parse_automation_policy expects logic + goals to be
    # present even if no goals were supplied; the helper backfills both.
    assert policy["logic"] == "AND"
    assert policy["goals"] == []


def test_research_metadata_omits_policy_when_no_thresholds() -> None:
    """Without --max-tokens / --max-experiments, no automation_policy is sent."""
    metadata = _build_research_inbound_metadata(
        mode="manual",
        profile="default",
        max_tokens=None,
        max_experiments=None,
        project_dir=None,
    )
    assert metadata == {"run_mode": "manual", "agent_profile": "default"}
    assert "automation_policy" not in metadata
    assert "project_dir" not in metadata


def test_research_metadata_partial_policy() -> None:
    """Only one threshold is enough to materialise an automation_policy."""
    metadata = _build_research_inbound_metadata(
        mode="auto",
        profile="research",
        max_tokens=None,
        max_experiments=3,
        project_dir=None,
    )
    policy = metadata["automation_policy"]
    assert policy["maxExperiments"] == 3
    assert "maxTokens" not in policy


def test_research_metadata_parsed_by_research_loop() -> None:
    """End-to-end: metadata produced by the CLI must round-trip through
    ResearchAgentLoop._parse_automation_policy without being dropped."""
    from mira_engine.agent.research_loop import ResearchAgentLoop

    metadata = _build_research_inbound_metadata(
        mode="auto",
        profile="research",
        max_tokens=20_000,
        max_experiments=5,
        project_dir="/tmp/PRJ-2",
    )
    parsed = ResearchAgentLoop._parse_automation_policy(metadata["automation_policy"])
    assert parsed is not None
    assert parsed["maxTokens"] == 20_000
    assert parsed["maxExperiments"] == 5


def test_research_command_rejects_invalid_mode() -> None:
    result = runner.invoke(app, ["research", "--mode", "bogus", "--message", "hi"])
    assert result.exit_code != 0
    assert "Invalid --mode" in result.output


def test_research_command_rejects_invalid_profile() -> None:
    result = runner.invoke(app, ["research", "--profile", "bogus", "--message", "hi"])
    assert result.exit_code != 0
    assert "Invalid --profile" in result.output


def test_research_command_rejects_non_positive_thresholds() -> None:
    bad_tokens = runner.invoke(app, ["research", "--max-tokens", "0", "--message", "hi"])
    assert bad_tokens.exit_code != 0
    assert "--max-tokens must be a positive integer" in bad_tokens.output

    bad_experiments = runner.invoke(
        app, ["research", "--max-experiments", "-1", "--message", "hi"]
    )
    assert bad_experiments.exit_code != 0
    assert "--max-experiments must be a positive integer" in bad_experiments.output
