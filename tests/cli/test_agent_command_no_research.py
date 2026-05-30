"""Smoke test: ``mira agent`` uses ``BaseAgentLoop`` (no research baggage).

The architectural intent of the loop split is that ``mira agent`` runs a
nanobot-shaped baseline. This test fails loudly if a regression slips a
research-only attribute back into the base loop or back into the ``mira
agent`` CLI wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mira_engine.agent.base_loop import BaseAgentLoop
from mira_engine.agent.research_loop import ResearchAgentLoop
from mira_engine.bus.events import InboundMessage
from mira_engine.bus.queue import MessageBus
from mira_engine.cli.commands import app
from mira_engine.config.schema import ChannelsConfig, ExecToolConfig
from mira_engine.providers.base import LLMProvider, LLMResponse
from mira_engine.session.manager import SessionManager

runner = CliRunner()


class _NoopProvider(LLMProvider):
    async def chat(self, **kwargs):  # type: ignore[override]
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy/default"


def _make_base_loop(tmp_path: Path) -> BaseAgentLoop:
    return BaseAgentLoop(
        bus=MessageBus(),
        provider=_NoopProvider(),
        workspace=tmp_path,
        model="dummy/default",
        channels_config=ChannelsConfig(),
        exec_config=ExecToolConfig(timeout=5),
        session_manager=SessionManager(tmp_path),
    )


def test_agent_command_registered() -> None:
    """``mira agent`` is still registered after the split."""
    result = runner.invoke(app, ["agent", "--help"])
    assert result.exit_code == 0, result.output
    assert "Interact with the general-purpose agent" in result.output
    # The ``agent`` command must NOT advertise research-only flags; those
    # belong to ``mira research``.
    assert "--mode" not in result.output
    assert "--profile" not in result.output
    assert "--max-tokens" not in result.output


def test_base_loop_lacks_research_state(tmp_path: Path) -> None:
    """Concrete BaseAgentLoop instance must not carry research-only state."""
    loop = _make_base_loop(tmp_path)
    research_attrs = (
        "_session_run_modes",
        "_session_agent_profiles",
        "_session_automation_policies",
        "_session_tokens_used",
        "_last_task_plan_guard_issues",
        "_last_task_plan_guard_fixed",
    )
    for attr in research_attrs:
        assert not hasattr(loop, attr), (
            f"BaseAgentLoop instance unexpectedly carries {attr}"
        )


@pytest.mark.asyncio
async def test_base_loop_processes_message_without_research_metadata(
    monkeypatch, tmp_path: Path,
) -> None:
    """``mira agent`` style flow: send a message through ``BaseAgentLoop``.

    The response must NOT include research-specific metadata fields
    (``tokens_used_session`` / ``max_tokens``) and the loop must not lazily
    grow research-only attributes after processing.
    """
    loop = _make_base_loop(tmp_path)

    async def _fake_run(messages, model_runtime, on_progress=None, audit_hook=None):
        return "done", [], messages + [{"role": "assistant", "content": "done"}]

    monkeypatch.setattr(loop, "_run_agent_loop", _fake_run)

    msg = InboundMessage(
        channel="cli",
        sender_id="user",
        chat_id="direct",
        content="hello",
    )
    response = await loop._process_message(msg, session_key="cli:direct")
    assert response is not None
    assert response.content == "done"
    # Research-only metadata fields should be absent on the BaseAgentLoop path.
    assert "tokens_used_session" not in (response.metadata or {})
    assert "max_tokens" not in (response.metadata or {})

    # Even after processing, no research-only state should have appeared.
    assert not hasattr(loop, "_session_tokens_used")
    assert not hasattr(loop, "_session_run_modes")
    assert not hasattr(loop, "_session_automation_policies")


def test_research_loop_still_subclasses_base() -> None:
    """Sanity check: the alias surface stays intact."""
    assert issubclass(ResearchAgentLoop, BaseAgentLoop)
    # AgentLoop alias should resolve to ResearchAgentLoop.
    from mira_engine.agent.loop import AgentLoop

    assert AgentLoop is ResearchAgentLoop
