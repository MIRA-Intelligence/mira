"""BaseAgentLoop unit tests.

Anything specific to Mira's research orchestration (auto-mode, agent
profiles, automation policies, task-plan guardrails, cumulative session
token broadcasting) lives in ``tests/test_research_loop_core.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mira_engine.agent.base_loop import BaseAgentLoop
from mira_engine.agent.context import ContextBuilder
from mira_engine.agent.routing import RoutedModel
from mira_engine.agent.tools.base import Tool
from mira_engine.agent.tools.filesystem import _resolve_path
from mira_engine.agent.tools.message import MessageTool
from mira_engine.agent.tools.registry import ToolRegistry
from mira_engine.bus.events import InboundMessage, OutboundMessage
from mira_engine.bus.queue import MessageBus
from mira_engine.config.schema import ChannelsConfig, ExecToolConfig
from mira_engine.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from mira_engine.session.manager import Session, SessionManager


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


def _make_loop(tmp_path: Path) -> BaseAgentLoop:
    """Build a BaseAgentLoop without running ``__init__`` (fast unit tests)."""
    loop = BaseAgentLoop.__new__(BaseAgentLoop)
    loop.max_iterations = 3
    loop.temperature = 0.1
    loop.max_tokens = 256
    loop.reasoning_effort = None
    loop.context = ContextBuilder(tmp_path)
    loop.tools = ToolRegistry()
    loop.tools.register(_EchoTool())
    loop.model_router = SimpleNamespace(enabled=True)
    loop._project_sessions = {}
    loop._TOOL_RESULT_MAX_CHARS = 20
    return loop


def _make_real_loop(tmp_path: Path) -> BaseAgentLoop:
    return BaseAgentLoop(
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

    loop = BaseAgentLoop(
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


def test_static_helper_methods(tmp_path: Path) -> None:
    """Generic helpers that have no research dependency."""
    loop = _make_loop(tmp_path)
    assert BaseAgentLoop._strip_think("<think>x</think>hello") == "hello"
    assert BaseAgentLoop._strip_think(None) is None
    assert BaseAgentLoop._extract_read_file_path({"path": "/tmp/a"}) == "/tmp/a"
    assert BaseAgentLoop._extract_read_file_path({"bad": "x"}) is None
    assert BaseAgentLoop._extract_skill_name_from_path("/a/skills/research/SKILL.md") == "research"
    assert BaseAgentLoop._extract_skill_name_from_path("/a/skills/research/readme.md") is None
    assert BaseAgentLoop._build_skill_invoked_event(
        tool_name="read_file", arguments={"path": "/a/skills/demo/SKILL.md"}
    ) == {"tool": "read_file", "skill_name": "demo", "path": "/a/skills/demo/SKILL.md"}
    assert BaseAgentLoop._build_skill_invoked_event(tool_name="exec", arguments={}) is None
    assert "score=3" in BaseAgentLoop._route_hint("small", "m", ("x",), 3, "instinct", "r")

    merged = loop._compose_extra_system("UI rules", "Guard notice")
    assert merged == "UI rules\n\nGuard notice"
    assert loop._compose_extra_system("", "Guard notice") == "Guard notice"
    assert loop._compose_extra_system(None, None) is None


def test_base_loop_omits_research_state() -> None:
    """Defensive: base attributes must NOT include research-only dicts.

    Acts as a smoke-test that the split has not silently regressed.
    """
    loop = BaseAgentLoop.__new__(BaseAgentLoop)
    research_attrs = (
        "_session_run_modes",
        "_session_agent_profiles",
        "_session_automation_policies",
        "_session_tokens_used",
        "_last_task_plan_guard_issues",
        "_last_task_plan_guard_fixed",
    )
    for attr in research_attrs:
        assert not hasattr(loop, attr), f"BaseAgentLoop unexpectedly carries {attr}"
    research_methods = (
        "_resolve_session_run_mode",
        "_resolve_session_agent_profile",
        "_resolve_session_automation_policy",
        "_evaluate_automation_stop_policy",
        "_load_task_plan",
        "_should_continue_auto_web",
        "_guard_task_plan_structure",
        "_build_auto_continue_message",
        "_accumulate_session_tokens",
        "_max_tokens_from_policy",
        "_handle_set_mode",
    )
    for method in research_methods:
        assert not hasattr(BaseAgentLoop, method), (
            f"BaseAgentLoop unexpectedly exposes research-only method {method}"
        )


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


async def test_dispatch_and_stop_handlers(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    loop.bus = MessageBus()
    loop._processing_lock = asyncio.Lock()
    loop.subagents = SimpleNamespace(cancel_by_session=lambda _k: asyncio.sleep(0, result=1))
    loop._active_tasks = {}

    msg = InboundMessage(channel="web", sender_id="u", chat_id="c", content="x")

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


async def test_run_main_loop_and_process_direct(monkeypatch, tmp_path: Path) -> None:
    loop = _make_real_loop(tmp_path)

    async def _noop_connect():
        return None

    async def _fake_dispatch(_msg):
        await asyncio.sleep(0.01)

    async def _fake_stop(_msg):
        loop._running = False

    monkeypatch.setattr(loop, "_connect_mcp", _noop_connect)
    monkeypatch.setattr(loop, "_dispatch", _fake_dispatch)
    monkeypatch.setattr(loop, "_handle_stop", _fake_stop)

    runner = asyncio.create_task(loop.run())
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
