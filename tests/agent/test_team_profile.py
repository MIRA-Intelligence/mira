"""Tests for the team profile: role tools, role providers, and consult."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mira_engine.agent.research_loop import ResearchAgentLoop
from mira_engine.agent.runner import AgentRunResult
from mira_engine.agent.subagent import ROLE_CONFIGS, SubagentManager
from mira_engine.agent.tools.consult import ConsultRoleTool
from mira_engine.agent.tools.spawn import SpawnTool
from mira_engine.bus.queue import MessageBus
from mira_engine.config.schema import AgentDefaults, Config, ExecToolConfig
from mira_engine.providers.base import LLMProvider, LLMResponse
from mira_engine.task_plan import guardrails


class _RecordingProvider(LLMProvider):
    """Fake provider that records the model it was asked to run and replies once."""

    def __init__(self, reply: str, default_model: str):
        super().__init__()
        self.reply = reply
        self._default_model = default_model
        self.models_seen: list[str | None] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        self.models_seen.append(model)
        return LLMResponse(content=self.reply)

    def get_default_model(self) -> str:
        return self._default_model

    def supports_vision(self, model: str | None = None) -> bool:
        return False


def _manager(tmp_path: Path, role_factory=None) -> SubagentManager:
    return SubagentManager(
        provider=_RecordingProvider("default-reply", "default/model"),
        workspace=tmp_path,
        bus=MessageBus(),
        model="default/model",
        exec_config=ExecToolConfig(enable=True),
        role_provider_factory=role_factory,
    )


def test_build_tools_critic_is_read_only(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    critic_tools = set(mgr._build_tools(tmp_path, "critic").tool_names)
    student_tools = set(mgr._build_tools(tmp_path, "student").tool_names)

    # Critic must not be able to mutate the workspace.
    assert "write_file" not in critic_tools
    assert "edit_file" not in critic_tools
    assert "exec" not in critic_tools
    assert "read_file" in critic_tools
    assert critic_tools <= ROLE_CONFIGS["critic"]["allowed_tools"]

    # Student is a full implementer.
    assert {"write_file", "edit_file", "exec", "read_file"} <= student_tools


def test_build_subagent_prompt_includes_role_block(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    critic_prompt = mgr._build_subagent_prompt(tmp_path, "critic")
    # The critic reviews the design before a run and the evidence after it.
    for verdict in ("[DESIGN: OKAY]", "[DESIGN: REVISE]", "[SUPPORTED]", "[PARTIAL]", "[NOT SUPPORTED]"):
        assert verdict in critic_prompt
    assert "[BLOCKING]" in critic_prompt and "Alternative explanations" in critic_prompt
    student_prompt = mgr._build_subagent_prompt(tmp_path, "student")
    assert "Role: Student (Executor)" in student_prompt
    assert "## What was obtained" in student_prompt and "IDs per split" in student_prompt


def test_the_supervisor_is_never_a_delegate() -> None:
    # The main loop plays the supervisor; only the student and critic are delegates.
    assert set(ROLE_CONFIGS) == set(SubagentManager._ROLE_PROMPTS) == {"student", "critic"}


async def test_delegates_get_the_main_loop_step_budget(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    mgr.max_iterations = 321
    specs = []

    async def _capture(runner, spec):
        specs.append(spec)
        return AgentRunResult(final_content="ok", messages=spec.initial_messages)

    mgr._run_delegate = _capture
    await mgr.consult("student", "run the sweep")
    await mgr._run_subagent("t1", "long run", "long", {"channel": "cli", "chat_id": "direct"}, role="student")

    assert [s.max_iterations for s in specs] == [321, 321]
    assert "321 steps" in specs[0].max_iterations_message.format(max_iterations=321)


async def test_spawn_tool_passes_the_team_role(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    seen = {}

    async def _spawn(**kwargs):
        seen.update(kwargs)
        return "started"

    mgr.spawn = _spawn
    tool = SpawnTool(manager=mgr)
    assert tool.parameters["properties"]["role"]["enum"] == ["student", "critic"]

    assert await tool.execute(task="train", role="Student") == "started"
    assert seen["role"] == "student"
    assert (await tool.execute(task="x", role="supervisor")).startswith("Error: unknown role")


async def test_consult_uses_role_provider_and_continues_session(tmp_path: Path) -> None:
    providers = {
        "critic": _RecordingProvider("Looks rigorous. **[DESIGN: OKAY]**", "anthropic/claude-opus-4-5"),
        "student": _RecordingProvider("done", "deepseek/deepseek-chat"),
    }

    def role_factory(role: str):
        p = providers[role]
        return p, p.get_default_model(), (p.get_default_model(),)

    mgr = _manager(tmp_path, role_factory=role_factory)

    result, session_id = await mgr.consult("critic", "review this plan")
    assert "[DESIGN: OKAY]" in result
    assert providers["critic"].models_seen == ["anthropic/claude-opus-4-5"]
    assert session_id in mgr._consult_sessions

    history_len = len(mgr._consult_sessions[session_id])
    result2, session_id2 = await mgr.consult("critic", "and now the revision", session_id=session_id)
    assert session_id2 == session_id
    # The same session accumulated more turns (continuation, not a fresh start).
    assert len(mgr._consult_sessions[session_id]) > history_len


async def test_consult_tool_formats_output_and_returns_session(tmp_path: Path) -> None:
    provider = _RecordingProvider("**[NOT SUPPORTED]** add a control", "anthropic/claude-opus-4-5")

    def role_factory(role: str):
        return provider, provider.get_default_model(), (provider.get_default_model(),)

    mgr = _manager(tmp_path, role_factory=role_factory)
    tool = ConsultRoleTool(manager=mgr)

    out = await tool.execute(role="critic", task="my plan")
    assert "[consult:critic session_id=" in out
    assert "[NOT SUPPORTED]" in out

    for bad_role in ("nope", "supervisor"):
        bad = await tool.execute(role=bad_role, task="x")
        assert bad.startswith("Error: unknown role")


def test_team_profile_registration() -> None:
    assert ResearchAgentLoop._parse_agent_profile("team") == "team"
    assert ResearchAgentLoop._agent_profile_to_agents_filename("team") == "AGENTS_TM.md"


def test_team_profile_guardrails_match_research() -> None:
    team = guardrails.get_task_plan_contract(
        profile="team", contract_version=guardrails.STRICT_CONTRACT_VERSION
    )
    research = guardrails.get_task_plan_contract(
        profile="research", contract_version=guardrails.STRICT_CONTRACT_VERSION
    )
    assert team["required_completed_fields"] == research["required_completed_fields"]
    assert team["profile"] == "team"


def test_role_provider_cross_provider_override() -> None:
    """A role can target a provider different from the global forced provider."""
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "openrouter",
                    "model": "claude-3-opus",
                    "studentProvider": "deepseek",
                    "studentModel": "deepseek-chat",
                }
            }
        }
    )
    defaults = config.agents.defaults
    assert defaults.student_model == "deepseek/deepseek-chat"
    # The global provider remains openrouter; the role overrides only itself.
    assert defaults.provider == "openrouter"
    assert defaults.role_provider("student") == "deepseek"


def test_role_provider_override_ignored_without_role_model() -> None:
    """A provider override with no role model must not force the inherited model.

    Regression: a role with ``criticProvider=nvidia`` but no ``criticModel``
    inherits the deepseek *primary* model. Forcing the NVIDIA provider onto it
    routed the deepseek model to the NVIDIA endpoint ("Error calling NVIDIA
    API"). The role should inherit the primary's provider instead.
    """
    from mira_engine.providers.factory import make_role_provider
    from mira_engine.providers.openai_compat_provider import OpenAICompatProvider

    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "deepseek",
                    "model": "deepseek/deepseek-v4-pro",
                    "criticProvider": "nvidia",
                }
            }
        }
    )
    config.providers.deepseek.api_key = "k-deepseek"
    config.providers.nvidia.api_key = "k-nvidia"

    provider, model, _candidates = make_role_provider(config, "critic")

    assert model == "deepseek/deepseek-v4-pro"
    assert isinstance(provider, OpenAICompatProvider)
