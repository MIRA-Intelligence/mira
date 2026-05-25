"""Research-specific agent loop coverage.

Sister file to ``tests/test_agent_loop_core.py`` (which now exercises only
``BaseAgentLoop``). Anything that exercises auto-mode orchestration, agent
profiles, automation policies, task-plan guardrails, or cumulative session
token accounting belongs here.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mira_engine.agent.base_loop import BaseAgentLoop
from mira_engine.agent.context import ContextBuilder
from mira_engine.agent.research_loop import ResearchAgentLoop
from mira_engine.agent.tools.registry import ToolRegistry
from mira_engine.bus.events import InboundMessage, OutboundMessage
from mira_engine.bus.queue import MessageBus
from mira_engine.config.schema import ChannelsConfig, ExecToolConfig
from mira_engine.providers.base import LLMProvider, LLMResponse
from mira_engine.session.manager import SessionManager


class _NoopProvider(LLMProvider):
    async def chat(self, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy/default"


def _make_loop(tmp_path: Path) -> ResearchAgentLoop:
    """Build a ResearchAgentLoop without running ``__init__`` (fast unit tests)."""
    loop = ResearchAgentLoop.__new__(ResearchAgentLoop)
    loop.max_iterations = 3
    loop.temperature = 0.1
    loop.max_tokens = 256
    loop.reasoning_effort = None
    loop.context = ContextBuilder(tmp_path)
    loop.tools = ToolRegistry()
    loop.model_router = SimpleNamespace(enabled=True)
    loop._session_run_modes = {}
    loop._session_agent_profiles = {}
    loop._session_automation_policies = {}
    loop._session_tokens_used = {}
    loop._last_task_plan_guard_issues = []
    loop._last_task_plan_guard_repairable_issues = []
    loop._last_task_plan_guard_fatal_issues = []
    loop._last_task_plan_guard_fixed = False
    loop._last_task_plan_guard_blocking = False
    loop._project_sessions = {}
    loop._TOOL_RESULT_MAX_CHARS = 20
    return loop


def _make_real_loop(tmp_path: Path) -> ResearchAgentLoop:
    return ResearchAgentLoop(
        bus=MessageBus(),
        provider=_NoopProvider(),
        workspace=tmp_path,
        model="dummy/default",
        channels_config=ChannelsConfig(),
        exec_config=ExecToolConfig(timeout=5),
        session_manager=SessionManager(tmp_path),
    )


def test_run_mode_profile_and_contract_helpers(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    assert loop._normalize_run_mode("AUTO") == "auto"
    assert loop._normalize_run_mode("invalid") == "manual"
    assert loop._parse_run_mode("manual") == "manual"
    assert loop._parse_run_mode("bad") is None
    assert loop._parse_agent_profile("RESEARCH") == "research"
    assert loop._parse_agent_profile("other") is None
    assert loop._resolve_session_run_mode("k", "auto") == "auto"
    assert loop._resolve_session_run_mode("k", None) == "auto"
    assert loop._resolve_session_agent_profile("k", "engineer") == "engineer"
    assert loop._resolve_session_agent_profile("k", None) == "engineer"
    assert loop._resolve_session_agent_profile("new", None) == "research"
    assert loop._agent_profile_to_agents_filename("research") == "AGENTS_RS.md"
    assert loop._agent_profile_to_agents_filename("engineer") == "AGENTS_EG.md"
    assert loop._agent_profile_to_agents_filename("default") == "AGENTS_RS.md"

    project = tmp_path / "PRJ-9"
    (project / ".mira").mkdir(parents=True)
    (project / ".mira" / "project.json").write_text(
        json.dumps({"agent_profile": "research", "contract_version": 2}),
        encoding="utf-8",
    )
    auto_msg = loop._build_auto_continue_message(
        channel="ui",
        chat_id="PRJ-9",
        project_dir=str(project),
        run_mode="auto",
        agent_profile="research",
    )
    assert "Execute exactly ONE pending experiment in this round" in auto_msg
    assert "If no pending experiment exists but the project's research goals" in auto_msg
    assert "immediately update and write task_plan.json" in auto_msg
    assert "Task-plan contract requirements" in auto_msg
    assert "theoretical_proof" in auto_msg
    # PR 2: prompt explicitly forbids auto-mode confirmation prompts.
    assert "Do NOT stop for confirmation" in auto_msg
    assert "Do NOT end your reply with a question to the user" in auto_msg
    assert "shall I proceed" in auto_msg
    assert "是否继续" in auto_msg

    checkpoint_msg = loop._build_auto_checkpoint_sync_message(
        channel="ui",
        chat_id="PRJ-9",
        project_dir=str(project),
        run_mode="auto",
        running_ids=["Exp001"],
        agent_profile="research",
    )
    assert "Checkpoint barrier" in checkpoint_msg
    assert "Exp001" in checkpoint_msg
    assert "do not mark an experiment as completed" in checkpoint_msg

    assert loop._is_strict_contract_enforced(
        project_dir=str(project),
        agent_profile="research",
    ) is True
    assert loop._is_strict_contract_enforced(
        project_dir=None,
        agent_profile="research",
    ) is False


def test_auto_run_decision_helpers(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    assert ResearchAgentLoop._looks_like_user_input_request("Please confirm your choice.") is True
    assert ResearchAgentLoop._looks_like_failure_response("Traceback (most recent call last): ...") is True
    assert ResearchAgentLoop._looks_like_failure_response("hypothesis failed but continue") is False
    # PR 1: tightened heuristics — generic mid-text phrasing no longer halts.
    assert (
        ResearchAgentLoop._looks_like_user_input_request(
            "Could you tell me more about the dataset later? "
            "I'll proceed with the next experiment now."
        )
        is False
    )
    assert (
        ResearchAgentLoop._looks_like_user_input_request(
            "I want to clarify that the metric improved.\n\n"
            "Starting Exp003 next."
        )
        is False
    )

    assert (
        ResearchAgentLoop._looks_like_user_input_request(
            "实验完成。\n\n继续下一步实验，无需你介入。"
        )
        is False
    )
    # Closing-paragraph asks (real blockers) still halt.
    assert (
        ResearchAgentLoop._looks_like_user_input_request(
            "已完成 Exp001。\n\n请确认是否进入下一阶段？"
        )
        is True
    )
    assert (
        ResearchAgentLoop._looks_like_user_input_request(
            "Summary written.\n\nWhat would you like to do next?"
        )
        is True
    )
    # PR 1: tightened failure heuristic — log-style snippets no longer halt.
    assert ResearchAgentLoop._looks_like_failure_response("[stderr] exit code: 0") is False
    assert ResearchAgentLoop._looks_like_failure_response("[stderr] exit code: 1; recorded as failed in task_plan") is False
    assert ResearchAgentLoop._looks_like_failure_response("ModuleNotFoundError will be fixed by installing X") is False
    assert ResearchAgentLoop._looks_like_failure_response("error: shell call returned non-zero, retrying") is False
    assert ResearchAgentLoop._looks_like_failure_response("出现错误但已捕获，继续下一步。") is False
    # System-level blockers and explicit "I cannot continue" verdicts still halt.
    assert ResearchAgentLoop._looks_like_failure_response("Tool call failed: provider unreachable.") is True
    assert (
        ResearchAgentLoop._looks_like_failure_response(
            "Error calling LLM: Error -3 while decompressing data: incorrect header check"
        )
        is True
    )
    assert ResearchAgentLoop._looks_like_failure_response("Memory archival failed during /new.") is True
    assert (
        ResearchAgentLoop._looks_like_failure_response(
            "Analysis complete.\n\nI cannot continue without write access."
        )
        is True
    )

    project = tmp_path / "PRJ-1"
    project.mkdir()
    (project / "task_plan.json").write_text(
        json.dumps({"experiments": [{"status": "pending"}]}), encoding="utf-8"
    )
    loaded = ResearchAgentLoop._load_task_plan(str(project))
    assert loaded is not None
    assert ResearchAgentLoop._plan_has_pending_work(loaded) is True
    assert ResearchAgentLoop._running_experiment_ids(loaded) == []

    assert loop._should_continue_auto_ui(
        run_mode="auto",
        project_dir=str(project),
        final_content="all good",
        auto_round=0,
    ) is True
    # PR 2 follow-up: research auto mode no longer filters on channel — any
    # channel reaching ResearchAgentLoop is by definition the research surface.
    assert loop._should_continue_auto_ui(
        run_mode="manual",
        project_dir=str(project),
        final_content="all good",
        auto_round=0,
    ) is False
    assert loop._should_continue_auto_ui(
        run_mode="auto",
        project_dir=str(project),
        final_content="please confirm",
        auto_round=0,
    ) is False
    # PR 1: strictHeuristics=False bypasses the user-input/failure heuristics
    # so the loop only stops on hard guards (round / experiment / token /
    # explicit tool failure). With pending work in the plan, the same input
    # that halts above must continue here.
    relaxed_policy = loop._parse_automation_policy(
        {"goals": [], "strictHeuristics": False}
    )
    assert loop._should_continue_auto_ui(
        run_mode="auto",
        project_dir=str(project),
        final_content="please confirm",
        auto_round=0,
        automation_policy=relaxed_policy,
    ) is True
    assert loop._should_continue_auto_ui(
        run_mode="auto",
        project_dir=str(project),
        final_content="Traceback (most recent call last): ...",
        auto_round=0,
        automation_policy=relaxed_policy,
    ) is True

    bad_project = tmp_path / "PRJ-bad"
    bad_project.mkdir()
    (bad_project / "task_plan.json").write_text("{", encoding="utf-8")
    assert loop._should_continue_auto_ui(
        run_mode="auto",
        project_dir=str(bad_project),
        final_content="all good",
        auto_round=0,
    ) is False

    compat_project = tmp_path / "PRJ-compat"
    (compat_project / ".mira").mkdir(parents=True)
    (compat_project / ".mira" / "project.json").write_text(
        json.dumps({"agent_profile": "research", "contract_version": 1}),
        encoding="utf-8",
    )
    (compat_project / "task_plan.json").write_text(
        json.dumps(
            {
                "experiments": [
                    {
                        "id": "Exp001",
                        "status": "completed",
                        "results": {"metrics": {"Dice": 0.78}},
                        "conclusion": "baseline established",
                    },
                    {"id": "Exp002", "status": "pending"},
                ]
            }
        ),
        encoding="utf-8",
    )
    decision, reason = loop._evaluate_continuation(
        run_mode="auto",
        project_dir=str(compat_project),
        final_content="all good",
        auto_round=0,
        agent_profile="research",
    )
    assert decision is True and reason is None

    strict_project = tmp_path / "PRJ-strict"
    (strict_project / ".mira").mkdir(parents=True)
    (strict_project / ".mira" / "project.json").write_text(
        json.dumps({"agent_profile": "research", "contract_version": 2}),
        encoding="utf-8",
    )
    (strict_project / "task_plan.json").write_text(
        (compat_project / "task_plan.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    decision, reason = loop._evaluate_continuation(
        run_mode="auto",
        project_dir=str(strict_project),
        final_content="all good",
        auto_round=0,
        agent_profile="research",
    )
    assert decision is False
    assert reason == "task_plan guardrail blocking"

    exhausted_policy = loop._parse_automation_policy(
        {
            "logic": "AND",
            "goals": [{"metric": "Dice", "operator": ">=", "value": 0.9}],
            "maxExperiments": 8,
        }
    )
    assert exhausted_policy is not None
    (project / "task_plan.json").write_text(
        json.dumps(
            {
                "experiments": [
                    {"status": "completed", "results": {"metrics": {"Dice": 0.78}}}
                    for _ in range(7)
                ]
            }
        ),
        encoding="utf-8",
    )
    assert loop._should_continue_auto_ui(
        run_mode="auto",
        project_dir=str(project),
        final_content="all good",
        auto_round=0,
        automation_policy=exhausted_policy,
        tokens_used=100,
    ) is True
    (project / "task_plan.json").write_text(
        json.dumps(
            {
                "experiments": [
                    {"status": "completed", "results": {"metrics": {"Dice": 0.78}}}
                    for _ in range(8)
                ]
            }
        ),
        encoding="utf-8",
    )
    assert loop._should_continue_auto_ui(
        run_mode="auto",
        project_dir=str(project),
        final_content="all good",
        auto_round=0,
        automation_policy=exhausted_policy,
        tokens_used=100,
    ) is False

    # PR 2: replan when queue empty + goals unmet, even WITHOUT maxExperiments.
    goals_only_policy = loop._parse_automation_policy(
        {
            "logic": "AND",
            "goals": [{"metric": "Dice", "operator": ">=", "value": 0.9}],
        }
    )
    assert goals_only_policy is not None
    assert "maxExperiments" not in goals_only_policy
    (project / "task_plan.json").write_text(
        json.dumps(
            {
                "experiments": [
                    {"status": "completed", "results": {"metrics": {"Dice": 0.78}}}
                ]
            }
        ),
        encoding="utf-8",
    )
    assert loop._should_continue_auto_ui(
        run_mode="auto",
        project_dir=str(project),
        final_content="all good",
        auto_round=0,
        automation_policy=goals_only_policy,
    ) is True

    # Queue empty + no policy should stop instead of replanning generic chat.
    (project / "task_plan.json").write_text(
        json.dumps(
            {
                "experiments": [
                    {
                        "status": "completed",
                        "results": {"metrics": {"Dice": 0.81}},
                        "conclusion": "baseline established",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert loop._should_continue_auto_ui(
        run_mode="auto",
        project_dir=str(project),
        final_content="all good",
        auto_round=0,
    ) is False
    decision, reason = loop._evaluate_continuation(
        run_mode="auto",
        project_dir=str(project),
        final_content="all good",
        auto_round=0,
    )
    assert decision is False
    assert reason == "queue exhausted, no replan condition met"

    # PR 2: structured stop reasons surface from _evaluate_continuation.
    decision, reason = loop._evaluate_continuation(
        run_mode="manual",
        project_dir=str(project),
        final_content="all good",
        auto_round=0,
    )
    assert decision is False and reason is None  # silent no-op for non-auto
    decision, reason = loop._evaluate_continuation(
        run_mode="auto",
        project_dir=str(project),
        final_content="all good",
        auto_round=ResearchAgentLoop._AUTO_MAX_ROUNDS,
    )
    assert decision is False
    assert reason is not None and "max rounds reached" in reason
    decision, reason = loop._evaluate_continuation(
        run_mode="auto",
        project_dir=str(project),
        final_content="please confirm before continuing",
        auto_round=0,
    )
    assert decision is False
    assert reason == "user-input heuristic matched"
    decision, reason = loop._evaluate_continuation(
        run_mode="auto",
        project_dir=str(project),
        final_content="Tool call failed: provider unreachable.",
        auto_round=0,
    )
    assert decision is False
    assert reason == "failure heuristic matched"
    (project / "task_plan.json").write_text(
        json.dumps({"experiments": [{"status": "pending"}]}),
        encoding="utf-8",
    )
    decision, reason = loop._evaluate_continuation(
        run_mode="auto",
        project_dir=str(project),
        final_content="all good",
        auto_round=0,
    )
    assert decision is True and reason is None

    before = {
        "experiments": [
            {"id": "Exp001", "status": "running", "results": {"metrics": {}}},
            {"id": "Exp002", "status": "pending"},
        ]
    }
    after_unchanged = {
        "experiments": [
            {"id": "Exp001", "status": "running", "results": {"metrics": {}}},
            {"id": "Exp002", "status": "pending"},
        ]
    }
    after_updated = {
        "experiments": [
            {"id": "Exp001", "status": "completed", "results": {"metrics": {"Dice": 0.8}}},
            {"id": "Exp002", "status": "pending"},
        ]
    }
    after_multi = {
        "experiments": [
            {"id": "Exp001", "status": "completed", "results": {"metrics": {"Dice": 0.8}}},
            {"id": "Exp002", "status": "failed"},
        ]
    }
    assert ResearchAgentLoop._has_experiment_checkpoint_update(before, after_unchanged) is False
    assert ResearchAgentLoop._has_experiment_checkpoint_update(before, after_updated) is True
    assert ResearchAgentLoop._experiments_crossed_boundary(before, after_updated) == ["Exp001"]
    assert ResearchAgentLoop._experiments_crossed_boundary(before, after_multi) == ["Exp001", "Exp002"]

    before_result = {"experiments": [], "result": {"summary": "keep me"}}
    after_result = {
        "experiments": [],
        "result": {"summary": "auto generated", "output_path": "outputs/", "output_type": "analysis"},
    }
    assert ResearchAgentLoop._has_result_section_update(before_result, after_result) is True
    assert ResearchAgentLoop._looks_like_result_request("Manual export request for PRJ-1.") is True
    assert ResearchAgentLoop._looks_like_result_request("continue experiments") is False
    assert ResearchAgentLoop._looks_like_result_request(
        "continue experiments", {"_allow_result_write": True}
    ) is True

    plan_file = project / "task_plan.json"
    plan_file.write_text(json.dumps(after_result), encoding="utf-8")
    restored, changed = loop._restore_result_section(
        str(project),
        before_plan=before_result,
        after_plan=after_result,
    )
    assert changed is True
    assert isinstance(restored, dict)
    assert restored.get("result") == before_result["result"]
    persisted = json.loads(plan_file.read_text(encoding="utf-8"))
    assert persisted.get("result") == before_result["result"]

    after_completed = {
        "status": "completed",
        "experiments": [{"id": "Exp001", "status": "completed"}],
    }
    plan_file.write_text(json.dumps(after_completed), encoding="utf-8")
    status_restored, status_changed = loop._restore_completion_status(
        str(project),
        after_plan=after_completed,
    )
    assert status_changed is True
    assert isinstance(status_restored, dict)
    assert status_restored.get("status") == "in_progress"
    persisted = json.loads(plan_file.read_text(encoding="utf-8"))
    assert persisted.get("status") == "in_progress"


async def test_normal_loop_mode_uses_base_loop_without_project_metadata(
    monkeypatch, tmp_path: Path
) -> None:
    loop = _make_real_loop(tmp_path)
    captured: dict[str, Any] = {}

    async def _base_process(self, msg, *args, **kwargs):
        captured["metadata"] = dict(msg.metadata)
        captured["session_key"] = msg.session_key
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="base ok")

    monkeypatch.setattr(BaseAgentLoop, "_process_message", _base_process)
    msg = InboundMessage(
        channel="ui",
        sender_id="u1",
        chat_id="__normal__",
        content="hello",
        metadata={
            "loop_mode": "normal",
            "project_dir": str(tmp_path / "PRJ-1"),
            "_ui_system_instructions": "research ui prompt",
        },
        session_key_override="ui:__normal__",
    )

    out = await loop._process_message(msg)

    assert out is not None
    assert out.content == "base ok"
    assert captured["session_key"] == "ui:__normal__"
    assert captured["metadata"]["loop_mode"] == "normal"
    assert "project_dir" not in captured["metadata"]
    assert "_ui_system_instructions" not in captured["metadata"]


def test_automation_policy_helpers(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)

    policy = loop._parse_automation_policy(
        {
            "logic": "OR",
            "goals": [
                {"metric": "Dice", "operator": ">", "value": 0.8},
                {"metric": "HD95", "operator": "<", "value": 5.0},
            ],
            "maxExperiments": 8,
            "maxTokens": 1000,
        }
    )
    assert policy is not None
    assert policy["logic"] == "OR"
    assert len(policy["goals"]) == 2
    assert policy["strictHeuristics"] is True

    # PR 1: the strictHeuristics flag round-trips through parsing.
    relaxed = loop._parse_automation_policy(
        {"goals": [], "strictHeuristics": False}
    )
    assert relaxed is not None
    assert relaxed["strictHeuristics"] is False
    assert ResearchAgentLoop._strict_heuristics_from_policy(relaxed) is False
    assert ResearchAgentLoop._strict_heuristics_from_policy(None) is True
    assert ResearchAgentLoop._strict_heuristics_from_policy({}) is True
    # Non-bool values fall back to default (True).
    assert (
        ResearchAgentLoop._strict_heuristics_from_policy({"strictHeuristics": "off"}) is True
    )

    plan = {
        "experiments": [
            {"status": "completed", "results": {"metrics": {"Dice": 0.82, "HD95": 6.1}}},
            {"status": "pending", "results": {"metrics": {}}},
        ]
    }
    stop_reason = ResearchAgentLoop._evaluate_automation_stop_policy(policy, plan=plan, tokens_used=100)
    assert stop_reason == "automation goals reached"

    strict_policy = loop._parse_automation_policy(
        {
            "logic": "AND",
            "goals": [{"metric": "Dice", "operator": ">=", "value": 0.9}],
            "maxExperiments": 1,
        }
    )
    assert strict_policy is not None
    exp_reason = ResearchAgentLoop._evaluate_automation_stop_policy(strict_policy, plan=plan, tokens_used=100)
    assert "max experiments reached" in (exp_reason or "")

    token_policy = loop._parse_automation_policy({"maxTokens": 200})
    assert token_policy is not None
    token_reason = ResearchAgentLoop._evaluate_automation_stop_policy(token_policy, plan=plan, tokens_used=250)
    assert "token budget reached" in (token_reason or "")


async def test_handle_set_mode_control_message(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    loop.bus = MessageBus()
    await loop._handle_set_mode(
        InboundMessage(
            channel="ui",
            sender_id="u",
            chat_id="c",
            content="",
            metadata={"run_mode": "AUTO"},
        )
    )
    mode_ack = await loop.bus.consume_outbound()
    assert mode_ack.metadata["run_mode"] == "auto"
    assert mode_ack.metadata["_control"] == "set_mode_ack"


async def test_handle_control_routes_set_mode(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    loop.bus = MessageBus()
    handled = await loop._handle_control(
        InboundMessage(
            channel="ui",
            sender_id="u",
            chat_id="c",
            content="",
            metadata={"_control": "set_mode", "run_mode": "auto"},
        ),
        "set_mode",
    )
    assert handled is True
    # Unknown control falls back to base (no-op, returns False).
    assert await loop._handle_control(
        InboundMessage(channel="ui", sender_id="u", chat_id="c", content=""),
        "unknown_control",
    ) is False


async def test_process_message_auto_continue_round(monkeypatch, tmp_path: Path) -> None:
    loop = _make_real_loop(tmp_path)
    progress_events: list[str] = []
    # PR 2: _process_message now consumes _evaluate_continuation directly so
    # progress emissions can include a structured stop reason.
    decisions: list[tuple[bool, str | None]] = [
        (True, None),
        (False, "queue exhausted, no replan condition met"),
    ]
    iter_decisions = iter(decisions)
    calls = {"n": 0}

    def _decide(**kwargs):
        return next(iter_decisions)

    async def _fake_run(messages, model_runtime, on_progress=None, audit_hook=None):
        calls["n"] += 1
        return f"round-{calls['n']}", [], messages + [{"role": "assistant", "content": f"round-{calls['n']}"}]

    async def _progress(msg: str) -> None:
        progress_events.append(msg)

    monkeypatch.setattr(loop, "_evaluate_continuation", _decide)
    monkeypatch.setattr(loop, "_run_agent_loop", _fake_run)

    msg = InboundMessage(
        channel="ui",
        sender_id="u",
        chat_id="PRJ-5",
        content="go",
        metadata={"run_mode": "auto", "project_dir": str(tmp_path / "PRJ-5")},
    )
    out = await loop._process_message(msg, on_progress=_progress)
    assert out.content == "round-2"
    assert any("auto-run round 1" in item for item in progress_events)
    assert any(
        "auto-run stop reason: queue exhausted" in item for item in progress_events
    )


async def test_process_message_auto_continue_round_non_ui_channel(
    monkeypatch, tmp_path: Path
) -> None:
    """Auto mode now fires for non-UI channels too (channel filter removed)."""
    loop = _make_real_loop(tmp_path)
    progress_events: list[str] = []
    decisions: list[tuple[bool, str | None]] = [
        (True, None),
        (False, "queue exhausted, no replan condition met"),
    ]
    iter_decisions = iter(decisions)
    calls = {"n": 0}

    def _decide(**kwargs):
        return next(iter_decisions)

    async def _fake_run(messages, model_runtime, on_progress=None, audit_hook=None):
        calls["n"] += 1
        return f"round-{calls['n']}", [], messages + [{"role": "assistant", "content": f"round-{calls['n']}"}]

    async def _progress(msg: str) -> None:
        progress_events.append(msg)

    monkeypatch.setattr(loop, "_evaluate_continuation", _decide)
    monkeypatch.setattr(loop, "_run_agent_loop", _fake_run)

    msg = InboundMessage(
        channel="cli",
        sender_id="u",
        chat_id="PRJ-CLI",
        content="go",
        metadata={"run_mode": "auto", "project_dir": str(tmp_path / "PRJ-CLI")},
    )
    out = await loop._process_message(msg, on_progress=_progress)
    assert out.content == "round-2"
    assert any("auto-run round 1" in item for item in progress_events)
    assert any(
        "auto-run stop reason: queue exhausted" in item for item in progress_events
    )


async def test_process_message_auto_guardrail_repair_round(monkeypatch, tmp_path: Path) -> None:
    loop = _make_real_loop(tmp_path)
    progress_events: list[str] = []
    calls = {"n": 0, "decide": 0}

    def _decide(**kwargs):
        calls["decide"] += 1
        if calls["decide"] == 1:
            loop._last_task_plan_guard_issues = ["Exp001: missing theoretical_proof"]
            loop._last_task_plan_guard_repairable_issues = [
                "Exp001: missing theoretical_proof"
            ]
            loop._last_task_plan_guard_fatal_issues = []
            return False, "task_plan guardrail blocking"
        else:
            loop._last_task_plan_guard_issues = []
            loop._last_task_plan_guard_repairable_issues = []
            loop._last_task_plan_guard_fatal_issues = []
        return False, "queue exhausted, no replan condition met"

    async def _fake_run(messages, model_runtime, on_progress=None, audit_hook=None):
        calls["n"] += 1
        return f"round-{calls['n']}", [], messages + [{"role": "assistant", "content": f"round-{calls['n']}"}]

    async def _progress(msg: str) -> None:
        progress_events.append(msg)

    monkeypatch.setattr(loop, "_evaluate_continuation", _decide)
    monkeypatch.setattr(loop, "_run_agent_loop", _fake_run)

    msg = InboundMessage(
        channel="ui",
        sender_id="u",
        chat_id="PRJ-7",
        content="go",
        metadata={"run_mode": "auto", "project_dir": str(tmp_path / "PRJ-7")},
    )
    out = await loop._process_message(msg, on_progress=_progress)
    assert out.content == "round-2"
    assert any("guardrail repair 1" in item for item in progress_events)
    assert not any("task_plan guardrail blocking" in item for item in progress_events)


async def test_process_message_broadcasts_token_usage_and_resets_on_new(
    monkeypatch, tmp_path: Path
) -> None:
    loop = _make_real_loop(tmp_path)

    token_script = iter([1500, 700, 0])

    async def _fake_run(messages, model_runtime, on_progress=None, audit_hook=None):
        loop._last_loop_tokens_used = next(token_script)
        if on_progress is not None:
            await on_progress("midway-progress")
        return "done", [], messages + [{"role": "assistant", "content": "done"}]

    monkeypatch.setattr(loop, "_run_agent_loop", _fake_run)

    msg1 = InboundMessage(
        channel="ui",
        sender_id="u",
        chat_id="PRJ-T1",
        content="first",
        metadata={"automation_policy": {"maxTokens": 50000}},
    )
    out1 = await loop._process_message(msg1)
    assert out1.metadata["tokens_used_session"] == 1500
    assert out1.metadata["max_tokens"] == 50000
    assert loop._session_tokens_used["ui:PRJ-T1"] == 1500

    first_progress: list[OutboundMessage] = []
    while loop.bus.outbound_size:
        first_progress.append(await loop.bus.consume_outbound())
    # Progress fires inside the first loop, before the post-loop accumulator
    # has run, so the cumulative figure here is the *pre-loop* total (0).
    assert any(
        m.metadata.get("_progress") is True
        and m.metadata.get("tokens_used_session") == 0
        and m.metadata.get("max_tokens") == 50000
        for m in first_progress
    )

    msg2 = InboundMessage(
        channel="ui",
        sender_id="u",
        chat_id="PRJ-T1",
        content="second",
    )
    out2 = await loop._process_message(msg2)
    assert out2.metadata["tokens_used_session"] == 2200
    assert out2.metadata["max_tokens"] == 50000
    assert loop._session_tokens_used["ui:PRJ-T1"] == 2200

    # Progress emitted during the second message picks up the cumulative
    # total carried over from the previous message.
    second_progress: list[OutboundMessage] = []
    while loop.bus.outbound_size:
        second_progress.append(await loop.bus.consume_outbound())
    assert any(
        m.metadata.get("_progress") is True
        and m.metadata.get("tokens_used_session") == 1500
        for m in second_progress
    )

    async def _consolidate(*args, **kwargs):
        return True

    monkeypatch.setattr(loop, "_consolidate_memory", _consolidate)
    new_resp = await loop._process_message(
        InboundMessage(channel="ui", sender_id="u", chat_id="PRJ-T1", content="/new")
    )
    assert new_resp.content == "New session started."
    assert "ui:PRJ-T1" not in loop._session_tokens_used


async def test_accumulate_session_tokens_helper() -> None:
    loop = ResearchAgentLoop.__new__(ResearchAgentLoop)
    loop._session_tokens_used = {}
    assert loop._accumulate_session_tokens("k", 100) == 100
    assert loop._accumulate_session_tokens("k", 50) == 150
    assert loop._accumulate_session_tokens("k", 0) == 150
    assert loop._accumulate_session_tokens("k", -10) == 150
    assert loop._accumulate_session_tokens("other", 25) == 25
    assert loop._session_tokens_used == {"k": 150, "other": 25}


def test_max_tokens_from_policy_helper() -> None:
    loop = ResearchAgentLoop.__new__(ResearchAgentLoop)
    assert loop._max_tokens_from_policy(None) is None
    assert loop._max_tokens_from_policy({}) is None
    assert loop._max_tokens_from_policy({"maxTokens": 0}) is None
    assert loop._max_tokens_from_policy({"maxTokens": -5}) is None
    assert loop._max_tokens_from_policy({"maxTokens": "1000"}) is None
    assert loop._max_tokens_from_policy({"maxTokens": 50_000}) == 50_000


async def test_run_main_loop_dispatches_set_mode_control(monkeypatch, tmp_path: Path) -> None:
    """``run`` routes ``_control == set_mode`` to the research handler."""
    loop = _make_real_loop(tmp_path)

    async def _noop_connect():
        return None

    async def _fake_dispatch(_msg):
        await asyncio.sleep(0.01)

    set_mode_calls: list[InboundMessage] = []

    async def _fake_set_mode(msg):
        set_mode_calls.append(msg)

    async def _fake_stop(_msg):
        loop._running = False

    monkeypatch.setattr(loop, "_connect_mcp", _noop_connect)
    monkeypatch.setattr(loop, "_dispatch", _fake_dispatch)
    monkeypatch.setattr(loop, "_handle_set_mode", _fake_set_mode)
    monkeypatch.setattr(loop, "_handle_stop", _fake_stop)

    runner = asyncio.create_task(loop.run())
    await loop.bus.publish_inbound(
        InboundMessage(
            channel="ui",
            sender_id="u",
            chat_id="PRJ-6",
            content="",
            metadata={"_control": "set_mode", "run_mode": "auto"},
        )
    )
    await loop.bus.publish_inbound(
        InboundMessage(channel="ui", sender_id="u", chat_id="PRJ-6", content="normal message")
    )
    await loop.bus.publish_inbound(
        InboundMessage(channel="ui", sender_id="u", chat_id="PRJ-6", content="/stop")
    )
    await runner
    assert loop._running is False
    assert len(set_mode_calls) == 1
    assert set_mode_calls[0].metadata.get("run_mode") == "auto"


async def test_session_reset_drops_research_state(tmp_path: Path) -> None:
    loop = _make_real_loop(tmp_path)
    loop._session_run_modes["ui:PRJ-X"] = "auto"
    loop._session_agent_profiles["ui:PRJ-X"] = "research"
    loop._session_automation_policies["ui:PRJ-X"] = {"logic": "AND", "goals": []}
    loop._session_tokens_used["ui:PRJ-X"] = 1234

    loop._on_session_reset("ui:PRJ-X")
    assert "ui:PRJ-X" not in loop._session_automation_policies
    assert "ui:PRJ-X" not in loop._session_tokens_used
    # Run modes / profiles are intentionally retained across /new so the next
    # turn keeps using the same UI selection unless the user toggles it.
    assert loop._session_run_modes.get("ui:PRJ-X") == "auto"
    assert loop._session_agent_profiles.get("ui:PRJ-X") == "research"
