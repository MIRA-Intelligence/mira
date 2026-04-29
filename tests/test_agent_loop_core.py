from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mira_engine.agent.context import ContextBuilder
from mira_engine.agent.loop import AgentLoop
from mira_engine.agent.routing import RoutedModel
from mira_engine.agent.tools.base import Tool
from mira_engine.agent.tools.message import MessageTool
from mira_engine.agent.tools.registry import ToolRegistry
from mira_engine.bus.events import InboundMessage, OutboundMessage
from mira_engine.bus.queue import MessageBus
from mira_engine.config.schema import ChannelsConfig, ExecToolConfig
from mira_engine.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from mira_engine.session.manager import Session, SessionManager
from mira_engine.agent.tools.filesystem import _resolve_path


class _NoopProvider(LLMProvider):
    async def chat(self, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy/default"


class _EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "echo"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        }

    async def execute(self, value: int, **kwargs: Any) -> str:
        return f"value={value}"


class _RuntimeStub:
    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.route = RoutedModel(
            tier="small",
            model="dummy/default",
            candidates=("dummy/default",),
            score=10,
            source="instinct",
            reason="test",
        )

    async def resolve(self, messages: list[dict[str, Any]], iteration: int = 1):
        return object(), self.route

    async def chat(self, route: RoutedModel, **kwargs: Any):
        if self._responses:
            response = self._responses.pop(0)
        else:
            response = LLMResponse(content="done")
        return response, route


def _make_loop(tmp_path: Path) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop.max_iterations = 3
    loop.temperature = 0.1
    loop.max_tokens = 256
    loop.reasoning_effort = None
    loop.context = ContextBuilder(tmp_path)
    loop.tools = ToolRegistry()
    loop.tools.register(_EchoTool())
    loop.model_router = SimpleNamespace(enabled=True)
    loop._session_run_modes = {}
    loop._session_agent_profiles = {}
    loop._project_sessions = {}
    loop._TOOL_RESULT_MAX_CHARS = 20
    return loop


def _make_real_loop(tmp_path: Path) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=_NoopProvider(),
        workspace=tmp_path,
        model="dummy/default",
        channels_config=ChannelsConfig(),
        exec_config=ExecToolConfig(timeout=5),
        session_manager=SessionManager(tmp_path),
    )


def test_restrict_workspace_allows_nested_workspace_mira_skills_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    nested_skills = workspace / ".mira" / "skills" / "medical-imaging" / "medical-image-dl-pipeline"
    nested_skills.mkdir(parents=True)
    skill_file = nested_skills / "SKILL.md"
    skill_file.write_text("# skill", encoding="utf-8")

    loop = AgentLoop(
        bus=MessageBus(),
        provider=_NoopProvider(),
        workspace=workspace,
        model="dummy/default",
        channels_config=ChannelsConfig(),
        exec_config=ExecToolConfig(timeout=5),
        session_manager=SessionManager(workspace),
        restrict_to_workspace=True,
    )

    read_tool = loop.tools.get("read_file")
    assert read_tool is not None
    resolved = _resolve_path(
        str(skill_file),
        workspace=read_tool._workspace,
        allowed_dir=read_tool._allowed_dir,
        extra_allowed_dirs=read_tool._extra_allowed_dirs,
    )
    assert resolved == skill_file.resolve()


def test_parse_and_route_helper_methods(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    assert AgentLoop._strip_think("<think>x</think>hello") == "hello"
    assert AgentLoop._strip_think(None) is None
    assert AgentLoop._extract_read_file_path({"path": "/tmp/a"}) == "/tmp/a"
    assert AgentLoop._extract_read_file_path({"bad": "x"}) is None
    assert AgentLoop._extract_skill_name_from_path("/a/skills/research/SKILL.md") == "research"
    assert AgentLoop._extract_skill_name_from_path("/a/skills/research/readme.md") is None
    assert AgentLoop._build_skill_invoked_event(
        tool_name="read_file", arguments={"path": "/a/skills/demo/SKILL.md"}
    ) == {"tool": "read_file", "skill_name": "demo", "path": "/a/skills/demo/SKILL.md"}
    assert AgentLoop._build_skill_invoked_event(tool_name="exec", arguments={}) is None
    assert "score=3" in AgentLoop._route_hint("small", "m", ("x",), 3, "instinct", "r")

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
    assert loop._agent_profile_to_agents_filename("research") == "AGENTS_RS.md"
    assert loop._agent_profile_to_agents_filename("engineer") == "AGENTS_EG.md"
    assert loop._agent_profile_to_agents_filename("default") == "AGENTS.md"
    merged = loop._compose_extra_system("UI rules", "Guard notice")
    assert merged == "UI rules\n\nGuard notice"
    assert loop._compose_extra_system("", "Guard notice") == "Guard notice"
    assert loop._compose_extra_system(None, None) is None
    project = tmp_path / "PRJ-9"
    (project / ".mira").mkdir(parents=True)
    (project / ".mira" / "project.json").write_text(
        json.dumps({"agent_profile": "research", "contract_version": 2}),
        encoding="utf-8",
    )
    auto_msg = loop._build_auto_continue_message(
        channel="web",
        chat_id="PRJ-9",
        project_dir=str(project),
        run_mode="auto",
        agent_profile="research",
    )
    assert "Execute exactly ONE pending experiment in this round" in auto_msg
    assert "If no pending experiment exists but automation goals are still unmet" in auto_msg
    assert "immediately update and write task_plan.json" in auto_msg
    assert "Task-plan contract requirements" in auto_msg
    assert "theoretical_proof" in auto_msg
    checkpoint_msg = loop._build_auto_checkpoint_sync_message(
        channel="web",
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
        agent_profile="default",
    ) is False


def test_auto_run_decision_helpers(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    assert AgentLoop._looks_like_user_input_request("Please confirm your choice.") is True
    assert AgentLoop._looks_like_failure_response("Traceback (most recent call last): ...") is True
    assert AgentLoop._looks_like_failure_response("hypothesis failed but continue") is False

    project = tmp_path / "PRJ-1"
    project.mkdir()
    (project / "task_plan.json").write_text(
        json.dumps({"experiments": [{"status": "pending"}]}), encoding="utf-8"
    )
    loaded = AgentLoop._load_task_plan(str(project))
    assert loaded is not None
    assert AgentLoop._plan_has_pending_work(loaded) is True
    assert AgentLoop._running_experiment_ids(loaded) == []

    assert loop._should_continue_auto_web(
        channel="web",
        run_mode="auto",
        project_dir=str(project),
        final_content="all good",
        auto_round=0,
    ) is True
    assert loop._should_continue_auto_web(
        channel="cli",
        run_mode="auto",
        project_dir=str(project),
        final_content="all good",
        auto_round=0,
    ) is False
    assert loop._should_continue_auto_web(
        channel="web",
        run_mode="auto",
        project_dir=str(project),
        final_content="please confirm",
        auto_round=0,
    ) is False

    bad_project = tmp_path / "PRJ-bad"
    bad_project.mkdir()
    (bad_project / "task_plan.json").write_text("{", encoding="utf-8")
    assert loop._should_continue_auto_web(
        channel="web",
        run_mode="auto",
        project_dir=str(bad_project),
        final_content="all good",
        auto_round=0,
    ) is False

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
    assert loop._should_continue_auto_web(
        channel="web",
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
    assert loop._should_continue_auto_web(
        channel="web",
        run_mode="auto",
        project_dir=str(project),
        final_content="all good",
        auto_round=0,
        automation_policy=exhausted_policy,
        tokens_used=100,
    ) is False

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
    assert AgentLoop._has_experiment_checkpoint_update(before, after_unchanged) is False
    assert AgentLoop._has_experiment_checkpoint_update(before, after_updated) is True
    assert AgentLoop._experiments_crossed_boundary(before, after_updated) == ["Exp001"]
    assert AgentLoop._experiments_crossed_boundary(before, after_multi) == ["Exp001", "Exp002"]

    before_result = {"experiments": [], "result": {"summary": "keep me"}}
    after_result = {
        "experiments": [],
        "result": {"summary": "auto generated", "output_path": "outputs/", "output_type": "analysis"},
    }
    assert AgentLoop._has_result_section_update(before_result, after_result) is True
    assert AgentLoop._looks_like_result_request("Manual export request for PRJ-1.") is True
    assert AgentLoop._looks_like_result_request("continue experiments") is False
    assert AgentLoop._looks_like_result_request(
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

    plan = {
        "experiments": [
            {"status": "completed", "results": {"metrics": {"Dice": 0.82, "HD95": 6.1}}},
            {"status": "pending", "results": {"metrics": {}}},
        ]
    }
    stop_reason = AgentLoop._evaluate_automation_stop_policy(policy, plan=plan, tokens_used=100)
    assert stop_reason == "automation goals reached"

    strict_policy = loop._parse_automation_policy(
        {
            "logic": "AND",
            "goals": [{"metric": "Dice", "operator": ">=", "value": 0.9}],
            "maxExperiments": 1,
        }
    )
    assert strict_policy is not None
    exp_reason = AgentLoop._evaluate_automation_stop_policy(strict_policy, plan=plan, tokens_used=100)
    assert "max experiments reached" in (exp_reason or "")

    token_policy = loop._parse_automation_policy({"maxTokens": 200})
    assert token_policy is not None
    token_reason = AgentLoop._evaluate_automation_stop_policy(token_policy, plan=plan, tokens_used=250)
    assert "token budget reached" in (token_reason or "")

async def test_run_agent_loop_tool_call_and_finish(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    runtime = _RuntimeStub(
        [
            LLMResponse(
                content="<think>internal</think>working",
                tool_calls=[ToolCallRequest(id="call-1", name="echo", arguments={"value": "7"})],
            ),
            LLMResponse(content="final answer"),
        ]
    )
    progress: list[tuple[str, bool]] = []

    async def _progress(content: str, tool_hint: bool = False) -> None:
        progress.append((content, tool_hint))

    final, tools_used, messages = await loop._run_agent_loop(
        [{"role": "user", "content": "hi"}],
        model_runtime=runtime,
        on_progress=_progress,
    )
    assert final == "final answer"
    assert tools_used == ["echo"]
    assert any(item[0] == "working" for item in progress)
    assert any(item[1] for item in progress)
    assert any(m.get("role") == "tool" for m in messages)


async def test_run_agent_loop_error_and_max_iterations(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    runtime_error = _RuntimeStub([LLMResponse(content="provider fail", finish_reason="error")])
    final, _, messages = await loop._run_agent_loop(
        [{"role": "user", "content": "hi"}],
        model_runtime=runtime_error,
    )
    assert final == "provider fail"
    assert messages[-1]["content"] == "(error — see previous log)"

    loop.max_iterations = 1
    runtime_max = _RuntimeStub(
        [
            LLMResponse(
                content="need tool",
                tool_calls=[ToolCallRequest(id="call-1", name="echo", arguments={"value": 1})],
            )
        ]
    )
    final2, _, _ = await loop._run_agent_loop(
        [{"role": "user", "content": "go"}],
        model_runtime=runtime_max,
    )
    assert "maximum number of tool call iterations" in final2


async def test_dispatch_and_control_handlers(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    loop.bus = MessageBus()
    loop._processing_lock = asyncio.Lock()
    loop.subagents = SimpleNamespace(cancel_by_session=lambda _k: asyncio.sleep(0, result=1))
    loop._active_tasks = {}

    msg = InboundMessage(channel="web", sender_id="u", chat_id="c", content="x")
    await loop._handle_set_mode(
        InboundMessage(
            channel="web",
            sender_id="u",
            chat_id="c",
            content="",
            metadata={"run_mode": "AUTO"},
        )
    )
    mode_ack = await loop.bus.consume_outbound()
    assert mode_ack.metadata["run_mode"] == "auto"

    running = asyncio.create_task(asyncio.sleep(10))
    loop._active_tasks[msg.session_key] = [running]
    await loop._handle_stop(msg)
    stopped = await loop.bus.consume_outbound()
    assert "Stopped" in stopped.content

    async def _ok(_msg):
        return OutboundMessage(channel="web", chat_id="c", content="ok")

    loop._process_message = _ok
    await loop._dispatch(msg)
    dispatched = await loop.bus.consume_outbound()
    assert dispatched.content == "ok"

    async def _none(_msg):
        return None

    cli_msg = InboundMessage(channel="cli", sender_id="u", chat_id="c", content="x")
    loop._process_message = _none
    await loop._dispatch(cli_msg)
    empty = await loop.bus.consume_outbound()
    assert empty.content == ""

    async def _boom(_msg):
        raise RuntimeError("fail")

    loop._process_message = _boom
    await loop._dispatch(msg)
    err = await loop.bus.consume_outbound()
    assert err.content == "Sorry, I encountered an error."

    cli_err_msg = InboundMessage(channel="cli", sender_id="u", chat_id="c", content="x")
    loop._process_message = _boom
    await loop._dispatch(cli_err_msg)
    cli_err = await loop.bus.consume_outbound()
    assert "Sorry, I encountered an error." in cli_err.content
    assert "mira agent --logs" in cli_err.content


def test_save_turn_and_project_session_cache(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    session = Session(key="web:c")
    runtime_tag = ContextBuilder._RUNTIME_CONTEXT_TAG
    long_tool = "x" * 80
    messages = [
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": long_tool, "tool_call_id": "1", "name": "echo"},
        {"role": "user", "content": f"{runtime_tag}\nctx\n\nHello user"},
        {"role": "user", "content": f"{runtime_tag}\nctx only"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"{runtime_tag}\nctx"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                {"type": "text", "text": "Body"},
            ],
        },
    ]
    loop._save_turn(session, messages, skip=0)
    assert len(session.messages) == 4
    assert session.messages[1]["content"].endswith("... (truncated)")
    assert session.messages[2]["content"] == "Hello user"
    assert session.messages[3]["content"][0]["text"] == "[image]"
    assert session.messages[3]["content"][1]["text"] == "Body"

    first = loop._get_project_sessions(str(tmp_path / "PRJ-1"))
    second = loop._get_project_sessions(str(tmp_path / "PRJ-1"))
    assert first is second


def test_set_tool_context_calls_supported_tools(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    calls: list[tuple[str, tuple[Any, ...]]] = []

    class _CtxTool:
        def __init__(self, name: str):
            self.name = name

        def set_context(self, *args):
            calls.append((self.name, args))

    tools = {"message": _CtxTool("message"), "spawn": _CtxTool("spawn"), "cron": _CtxTool("cron")}
    loop.tools = SimpleNamespace(get=lambda name: tools.get(name))
    loop._set_tool_context("web", "chat-1", "msg-9")
    assert ("message", ("web", "chat-1", "msg-9")) in calls
    assert ("spawn", ("web", "chat-1")) in calls
    assert ("cron", ("web", "chat-1")) in calls


def test_real_loop_initialization_registers_default_tools(tmp_path: Path) -> None:
    loop = _make_real_loop(tmp_path)
    names = set(loop.tools.tool_names)
    assert {
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "exec",
        "web_search",
        "web_fetch",
        "message",
        "spawn",
    }.issubset(names)


async def test_connect_and_close_mcp_paths(monkeypatch, tmp_path: Path) -> None:
    loop = _make_real_loop(tmp_path)
    loop._mcp_servers = {"s": {"type": "stdio", "command": "echo"}}
    called = {"count": 0}

    async def _ok_connect(servers, tools, stack):
        called["count"] += 1

    monkeypatch.setattr("mira_engine.agent.tools.mcp.connect_mcp_servers", _ok_connect)
    await loop._connect_mcp()
    assert loop._mcp_connected is True
    assert loop._mcp_connecting is False
    assert called["count"] == 1

    await loop.close_mcp()
    assert loop._mcp_stack is None

    loop2 = _make_real_loop(tmp_path)
    loop2._mcp_servers = {"s": {"type": "stdio", "command": "echo"}}

    async def _boom_connect(*_args, **_kwargs):
        raise RuntimeError("mcp down")

    monkeypatch.setattr("mira_engine.agent.tools.mcp.connect_mcp_servers", _boom_connect)
    await loop2._connect_mcp()
    assert loop2._mcp_connected is False
    assert loop2._mcp_connecting is False


async def test_process_message_system_help_new_and_normal(monkeypatch, tmp_path: Path) -> None:
    loop = _make_real_loop(tmp_path)

    async def _fake_run(messages, model_runtime, on_progress=None, audit_hook=None):
        return "done", [], messages + [{"role": "assistant", "content": "done"}]

    monkeypatch.setattr(loop, "_run_agent_loop", _fake_run)

    system = InboundMessage(channel="system", sender_id="s", chat_id="web:PRJ-1", content="hello")
    sys_resp = await loop._process_message(system)
    assert sys_resp.channel == "web"
    assert sys_resp.chat_id == "PRJ-1"
    assert sys_resp.content == "done"

    help_msg = InboundMessage(channel="web", sender_id="u", chat_id="PRJ-1", content="/help")
    help_resp = await loop._process_message(help_msg)
    assert "/new" in help_resp.content

    session = loop.sessions.get_or_create("web:PRJ-1")
    session.messages = [{"role": "user", "content": "old"}]
    loop.sessions.save(session)

    async def _consolidate(*args, **kwargs):
        return True

    monkeypatch.setattr(loop, "_consolidate_memory", _consolidate)
    new_msg = InboundMessage(channel="web", sender_id="u", chat_id="PRJ-1", content="/new")
    new_resp = await loop._process_message(new_msg)
    assert new_resp.content == "New session started."

    normal = InboundMessage(channel="web", sender_id="u", chat_id="PRJ-2", content="hi")
    norm_resp = await loop._process_message(normal)
    assert norm_resp.content == "done"


async def test_process_message_updates_recent_skills_metadata(monkeypatch, tmp_path: Path) -> None:
    loop = _make_real_loop(tmp_path)

    async def _fake_run(messages, model_runtime, on_progress=None, audit_hook=None):
        if audit_hook:
            await audit_hook({"tool": "read_file", "skill_name": "medical-image-dl-pipeline", "path": "/tmp/SKILL.md"})
        return "done", [], messages + [{"role": "assistant", "content": "done"}]

    monkeypatch.setattr(loop, "_run_agent_loop", _fake_run)
    msg = InboundMessage(channel="web", sender_id="u", chat_id="PRJ-7", content="继续之前任务")
    out = await loop._process_message(msg)
    assert out.content == "done"
    session = loop.sessions.get_or_create("web:PRJ-7")
    assert session.metadata.get("_recent_skills") == ["medical-image-dl-pipeline"]


async def test_process_message_injects_active_skills_into_context(monkeypatch, tmp_path: Path) -> None:
    loop = _make_real_loop(tmp_path)
    captured: dict[str, Any] = {}

    original_build_messages = loop.context.build_messages

    def _capture_build_messages(*args, **kwargs):
        captured["skill_names"] = kwargs.get("skill_names")
        return original_build_messages(*args, **kwargs)

    monkeypatch.setattr(loop.context, "build_messages", _capture_build_messages)

    async def _fake_run(messages, model_runtime, on_progress=None, audit_hook=None):
        return "done", [], messages + [{"role": "assistant", "content": "done"}]

    monkeypatch.setattr(loop, "_run_agent_loop", _fake_run)
    msg = InboundMessage(
        channel="web",
        sender_id="u",
        chat_id="PRJ-8",
        content="继续之前的医学影像去伪影任务",
    )
    out = await loop._process_message(msg)
    assert out.content == "done"
    assert captured.get("skill_names")
    assert "medical-image-dl-pipeline" in captured["skill_names"]


async def test_process_message_new_failure_and_message_tool_short_circuit(monkeypatch, tmp_path: Path) -> None:
    loop = _make_real_loop(tmp_path)
    session = loop.sessions.get_or_create("web:PRJ-3")
    session.messages = [{"role": "user", "content": "old"}]
    loop.sessions.save(session)

    async def _fail_consolidate(*args, **kwargs):
        return False

    monkeypatch.setattr(loop, "_consolidate_memory", _fail_consolidate)
    failed = await loop._process_message(
        InboundMessage(channel="web", sender_id="u", chat_id="PRJ-3", content="/new")
    )
    assert "Memory archival failed" in failed.content

    async def _fake_run(messages, model_runtime, on_progress=None, audit_hook=None):
        message_tool = loop.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool._sent_in_turn = True
        return "done", [], messages + [{"role": "assistant", "content": "done"}]

    monkeypatch.setattr(loop, "_run_agent_loop", _fake_run)
    no_outbound = await loop._process_message(
        InboundMessage(channel="web", sender_id="u", chat_id="PRJ-4", content="send via tool")
    )
    assert no_outbound is None


async def test_process_message_auto_continue_round(monkeypatch, tmp_path: Path) -> None:
    loop = _make_real_loop(tmp_path)
    progress_events: list[str] = []
    decisions = iter([True, False])
    calls = {"n": 0}

    def _decide(**kwargs):
        return next(decisions)

    async def _fake_run(messages, model_runtime, on_progress=None, audit_hook=None):
        calls["n"] += 1
        return f"round-{calls['n']}", [], messages + [{"role": "assistant", "content": f"round-{calls['n']}"}]

    async def _progress(msg: str) -> None:
        progress_events.append(msg)

    monkeypatch.setattr(loop, "_should_continue_auto_web", _decide)
    monkeypatch.setattr(loop, "_run_agent_loop", _fake_run)

    msg = InboundMessage(
        channel="web",
        sender_id="u",
        chat_id="PRJ-5",
        content="go",
        metadata={"run_mode": "auto", "project_dir": str(tmp_path / "PRJ-5")},
    )
    out = await loop._process_message(msg, on_progress=_progress)
    assert out.content == "round-2"
    assert any("auto-run round 1" in item for item in progress_events)


async def test_process_message_auto_guardrail_repair_round(monkeypatch, tmp_path: Path) -> None:
    loop = _make_real_loop(tmp_path)
    progress_events: list[str] = []
    calls = {"n": 0, "decide": 0}

    def _decide(**kwargs):
        calls["decide"] += 1
        if calls["decide"] == 1:
            loop._last_task_plan_guard_issues = ["Exp001: missing theoretical_proof"]
        else:
            loop._last_task_plan_guard_issues = []
        return False

    async def _fake_run(messages, model_runtime, on_progress=None, audit_hook=None):
        calls["n"] += 1
        return f"round-{calls['n']}", [], messages + [{"role": "assistant", "content": f"round-{calls['n']}"}]

    async def _progress(msg: str) -> None:
        progress_events.append(msg)

    monkeypatch.setattr(loop, "_should_continue_auto_web", _decide)
    monkeypatch.setattr(loop, "_run_agent_loop", _fake_run)

    msg = InboundMessage(
        channel="web",
        sender_id="u",
        chat_id="PRJ-7",
        content="go",
        metadata={"run_mode": "auto", "project_dir": str(tmp_path / "PRJ-7")},
    )
    out = await loop._process_message(msg, on_progress=_progress)
    assert out.content == "round-2"
    assert any("guardrail repair 1" in item for item in progress_events)


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
        channel="web",
        sender_id="u",
        chat_id="PRJ-T1",
        content="first",
        metadata={"automation_policy": {"maxTokens": 50000}},
    )
    out1 = await loop._process_message(msg1)
    assert out1.metadata["tokens_used_session"] == 1500
    assert out1.metadata["max_tokens"] == 50000
    assert loop._session_tokens_used["web:PRJ-T1"] == 1500

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
        channel="web",
        sender_id="u",
        chat_id="PRJ-T1",
        content="second",
    )
    out2 = await loop._process_message(msg2)
    assert out2.metadata["tokens_used_session"] == 2200
    assert out2.metadata["max_tokens"] == 50000
    assert loop._session_tokens_used["web:PRJ-T1"] == 2200

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
        InboundMessage(channel="web", sender_id="u", chat_id="PRJ-T1", content="/new")
    )
    assert new_resp.content == "New session started."
    assert "web:PRJ-T1" not in loop._session_tokens_used


async def test_accumulate_session_tokens_helper() -> None:
    loop = AgentLoop.__new__(AgentLoop)
    loop._session_tokens_used = {}
    assert loop._accumulate_session_tokens("k", 100) == 100
    assert loop._accumulate_session_tokens("k", 50) == 150
    assert loop._accumulate_session_tokens("k", 0) == 150
    assert loop._accumulate_session_tokens("k", -10) == 150
    assert loop._accumulate_session_tokens("other", 25) == 25
    assert loop._session_tokens_used == {"k": 150, "other": 25}


def test_max_tokens_from_policy_helper() -> None:
    loop = AgentLoop.__new__(AgentLoop)
    assert loop._max_tokens_from_policy(None) is None
    assert loop._max_tokens_from_policy({}) is None
    assert loop._max_tokens_from_policy({"maxTokens": 0}) is None
    assert loop._max_tokens_from_policy({"maxTokens": -5}) is None
    assert loop._max_tokens_from_policy({"maxTokens": "1000"}) is None
    assert loop._max_tokens_from_policy({"maxTokens": 50_000}) == 50_000


async def test_run_main_loop_and_process_direct(monkeypatch, tmp_path: Path) -> None:
    loop = _make_real_loop(tmp_path)

    async def _noop_connect():
        return None

    async def _fake_dispatch(_msg):
        await asyncio.sleep(0.01)

    async def _fake_set_mode(_msg):
        return None

    async def _fake_stop(_msg):
        loop._running = False

    monkeypatch.setattr(loop, "_connect_mcp", _noop_connect)
    monkeypatch.setattr(loop, "_dispatch", _fake_dispatch)
    monkeypatch.setattr(loop, "_handle_set_mode", _fake_set_mode)
    monkeypatch.setattr(loop, "_handle_stop", _fake_stop)

    runner = asyncio.create_task(loop.run())
    await loop.bus.publish_inbound(
        InboundMessage(
            channel="web",
            sender_id="u",
            chat_id="PRJ-6",
            content="",
            metadata={"_control": "set_mode", "run_mode": "auto"},
        )
    )
    await loop.bus.publish_inbound(
        InboundMessage(channel="web", sender_id="u", chat_id="PRJ-6", content="normal message")
    )
    await loop.bus.publish_inbound(InboundMessage(channel="web", sender_id="u", chat_id="PRJ-6", content="/stop"))
    await runner
    assert loop._running is False

    async def _proc_ok(msg, session_key=None, on_progress=None, audit_hook=None):
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="direct-ok")

    monkeypatch.setattr(loop, "_process_message", _proc_ok)
    assert await loop.process_direct("hello") == "direct-ok"

    async def _proc_none(msg, session_key=None, on_progress=None, audit_hook=None):
        return None

    monkeypatch.setattr(loop, "_process_message", _proc_none)
    assert await loop.process_direct("hello") == ""
    loop.stop()
    assert loop._running is False
