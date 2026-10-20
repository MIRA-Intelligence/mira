"""Native team routing and delegation must preserve ModuleRun boundaries."""

import asyncio
from typing import Any

import pytest

from mira_engine.agent.research_loop import ResearchAgentLoop
from mira_engine.agent.subagent import DelegationPolicy, SubagentManager
from mira_engine.bus.events import InboundMessage
from mira_engine.bus.queue import MessageBus
from mira_engine.config.schema import ChannelsConfig, ExecToolConfig
from mira_engine.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from mira_engine.session.manager import SessionManager
from mira_engine.workflows import JsonWorkflowRepository, WorkflowService


class Provider(LLMProvider):
    def __init__(self, responses=()):
        super().__init__()
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0) if self.responses else LLMResponse(content="Done")

    def get_default_model(self):
        return "test/default"


def call(name, **arguments):
    return LLMResponse(content=None, tool_calls=[ToolCallRequest(id=name, name=name, arguments=arguments)])


def complete():
    return call("complete_workflow_run", summary="Verified", output_artifact_ids=[], suggested_handoffs=[])


@pytest.fixture(autouse=True)
def isolate_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_HOME", str(tmp_path / "global"))


async def run_native(tmp_path, main, role_factory=None, *, stage="planning", profile="team",
                     metadata_profile=None, module_data=None, streaming=False, strict=False):
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    service = WorkflowService(JsonWorkflowRepository(project_dir, "QA-team"))
    run_id = service.create_run(command_id="create", module_type=stage,
        module_data={"intent": "Verify team scope", **(module_data or {})}, enqueue=True).run_id
    service.claim_run(run_id, lease_owner="test", agent_profile=profile)
    loop = ResearchAgentLoop(bus=MessageBus(), provider=main, workspace=tmp_path,
        restrict_to_workspace=strict,
        model="test/main", channels_config=ChannelsConfig(),
        exec_config=ExecToolConfig(enable=True, timeout=5),
        session_manager=SessionManager(tmp_path), role_provider_factory=role_factory)
    async def on_stream(delta):
        pass
    message = InboundMessage(content="Verify team scope", channel="ui", chat_id="QA-team",
        sender_id="user", metadata={
            "loop_mode": "project", "project_id": "QA-team", "project_dir": str(project_dir),
            "workflow_run_id": run_id, "agent_profile": metadata_profile or profile,
        })
    await loop._process_message(message, session_key=f"ui:QA-team:run:{run_id}",
        on_stream=on_stream if streaming else None)
    return loop, service


@pytest.mark.parametrize("profile", ["research", "engineer", "team"])
async def test_native_external_input_read_grants_are_scoped_and_read_only(tmp_path, profile):
    source = tmp_path / "user-data.txt"
    source.write_text("AUTHORIZED EXTERNAL EVIDENCE")
    main = Provider([call("read_file", path=str(source)),
                     call("write_file", path=str(source), content="changed"), complete()])
    loop, _ = await run_native(tmp_path, main, stage="experiment", profile=profile, strict=True,
        module_data={"external_read_paths": [str(source)]})
    replies = [m["content"] for m in main.calls[-1]["messages"] if m["role"] == "tool"]
    assert any("AUTHORIZED EXTERNAL EVIDENCE" in r for r in replies)
    assert any("outside allowed" in r for r in replies)
    assert source.read_text() == "AUTHORIZED EXTERNAL EVIDENCE"
    assert "outside allowed" in await loop.tools.get("read_file").execute(path=str(source))
    assert "external_read_paths" in main.calls[0]["messages"][0]["content"]


async def test_team_delegates_inherit_external_input_grants(tmp_path):
    source = tmp_path / "user-data.txt"
    source.write_text("DELEGATE EVIDENCE")
    supervisor = Provider([call("consult", role="student", task="Read supplied file"), complete()])
    student = Provider([call("read_file", path=str(source)), LLMResponse(content="Read it")])
    def factory(role):
        return (supervisor if role == "supervisor" else student), f"test/{role}", (f"test/{role}",)
    await run_native(tmp_path, supervisor, factory, stage="experiment", strict=True,
        module_data={"external_read_paths": [str(source)]})
    assert any("DELEGATE EVIDENCE" in m["content"] for m in student.calls[-1]["messages"] if m["role"] == "tool")


@pytest.mark.parametrize("profile", ["research", "engineer", "team"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_supervisor_model_is_selected_only_for_team(tmp_path, profile, streaming):
    main, supervisor = Provider([complete()]), Provider([complete()])
    roles = []

    def factory(role):
        roles.append(role)
        return supervisor, "test/supervisor", ("test/supervisor",)

    await run_native(tmp_path, main, factory, profile=profile, streaming=streaming)
    chosen = supervisor if profile == "team" else main
    assert chosen.calls
    assert chosen.calls[0]["model"] == ("test/supervisor" if profile == "team" else "test/main")
    assert roles == (["supervisor"] if profile == "team" else [])


@pytest.mark.parametrize("stage", ["initiation", "planning", "experiment", "delivery"])
async def test_student_inherits_stage_capabilities(tmp_path, stage):
    supervisor = Provider([call("consult", role="student", task="Check capabilities"), complete()])
    student = Provider([call("exec", command="printf team-probe"), LLMResponse(content="Checked")])

    def factory(role):
        provider = supervisor if role == "supervisor" else student
        return provider, f"test/{role}", (f"test/{role}",)

    # Pass supervisor as default too so the old code reaches the actual delegation bug.
    await run_native(tmp_path, supervisor, factory, stage=stage)
    tools = {tool["function"]["name"] for tool in student.calls[0]["tools"]}
    assert ("exec" in tools) == (stage in {"experiment", "delivery"})
    replies = [m["content"] for m in student.calls[1]["messages"] if m["role"] == "tool"]
    assert any("not found" in r for r in replies) == (stage in {"initiation", "planning"})
    assert "Native executor contract" in student.calls[0]["messages"][0]["content"]


def manager(tmp_path, provider):
    return SubagentManager(provider=provider, workspace=tmp_path, bus=MessageBus(),
        model="test/student", exec_config=ExecToolConfig(enable=True, timeout=5))


def policy(*, allowed=("read_file",), guard=None, stop=None, scope="project:run:attempt"):
    async def allow(*args):
        return None
    return DelegationPolicy(scope_id=scope, allowed_tools=frozenset(allowed),
        system_instructions="Native executor contract: planning only",
        execution_guard=guard or allow, stop_guard=stop or allow, run_id="run")


async def test_critic_remains_read_only_under_execution_policy(tmp_path):
    provider = Provider()
    mgr = manager(tmp_path, provider)
    with mgr.delegation_scope(policy(allowed=("read_file", "write_file", "exec"))):
        await mgr.consult("critic", "Review")
    names = {t["function"]["name"] for t in provider.calls[0]["tools"]}
    assert names == {"read_file"}


@pytest.mark.parametrize("scope,role", [("other-run", "student"), ("project:run:attempt", "critic")])
async def test_consult_history_cannot_cross_attempt_or_role(tmp_path, scope, role):
    mgr = manager(tmp_path, Provider())
    with mgr.delegation_scope(policy()):
        _, session = await mgr.consult("student", "First")
        await mgr.consult("student", "Followup", session_id=session)
    with mgr.delegation_scope(policy(scope=scope)):
        with pytest.raises(ValueError, match="does not belong"):
            await mgr.consult(role, "Wrong context", session_id=session)
    # Neither returning to legacy mode nor moving workspace may reuse it.
    with pytest.raises(ValueError, match="does not belong"):
        await mgr.consult("student", "Legacy", session_id=session)


async def test_closed_parent_stops_delegate_before_another_model_call(tmp_path):
    provider = Provider([call("write_file", path=str(tmp_path / "must-not-exist"), content="bad")])
    mgr = manager(tmp_path, provider)
    state = {"closed": False}
    async def guard(name, args):
        state["closed"] = True
        return "Parent paused"
    async def stop():
        return "Parent paused" if state["closed"] else None
    with mgr.delegation_scope(policy(allowed=("write_file",), guard=guard, stop=stop)):
        result, _ = await mgr.consult("student", "Probe pause")
    assert result == "Parent paused"
    assert len(provider.calls) == 1
    assert not (tmp_path / "must-not-exist").exists()


async def test_background_delegate_inherits_policy_and_does_not_start_legacy_turn(tmp_path):
    provider = Provider([call("exec", command="printf must-not-run"), LLMResponse(content="Done")])
    mgr = manager(tmp_path, provider)
    with mgr.delegation_scope(policy()):
        await mgr.spawn("Read-only probe", origin_channel="ui", origin_chat_id="QA", project_id="QA")
        pending = list(mgr._running_tasks.values())
    # Context is copied into the task even after the parent's scope exits.
    await asyncio.gather(*pending)
    assert "exec" not in {t["function"]["name"] for t in provider.calls[0]["tools"]}
    assert mgr.bus.inbound_size == 0
    notice = await mgr.bus.consume_outbound()
    assert notice.metadata["workflow_run_id"] == "run"
    assert notice.metadata["_subagent_result"] is True


async def test_parallel_delegation_scopes_do_not_leak(tmp_path):
    mgr = manager(tmp_path, Provider())
    async def inspect(allowed, scope):
        with mgr.delegation_scope(policy(allowed=allowed, scope=scope)):
            await asyncio.sleep(0)
            return set(mgr._build_tools(tmp_path, "student").tool_names)
    restricted, executable = await asyncio.gather(
        inspect(("read_file",), "planning"), inspect(("read_file", "exec"), "experiment"))
    assert restricted == {"read_file"}
    assert executable == {"read_file", "exec"}
    assert "write_file" in mgr._build_tools(tmp_path, "student").tool_names


async def test_team_route_isolated_from_default_and_cleared_on_reset(tmp_path):
    main, supervisor = Provider(), Provider()
    loop = ResearchAgentLoop(bus=MessageBus(), provider=main, workspace=tmp_path,
        model="test/main", role_provider_factory=lambda role: (supervisor, "test/supervisor", ("test/supervisor",)))
    team = loop._get_model_runtime("shared", role="supervisor")
    plain = loop._get_model_runtime("shared")
    (team_provider, team_route), (plain_provider, plain_route) = await asyncio.gather(
        team.resolve([]), plain.resolve([]))
    assert (team_provider, team_route.model) == (supervisor, "test/supervisor")
    assert (plain_provider, plain_route.model) == (main, "test/main")
    assert loop.provider is main and loop.model == "test/main"
    loop._on_session_reset("shared")
    assert "shared:role:supervisor" not in loop._session_model_runtimes


@pytest.mark.parametrize("control,status", [("pause", "paused"), ("cancel", "cancelled"),
                                           ("clarify", "waiting_input"), ("new_attempt", "running")])
async def test_native_guard_blocks_child_after_control_during_model_call(tmp_path, control, status):
    supervisor = Provider([call("consult", role="student", task="Pause probe"), complete()])
    target = tmp_path / "project" / "must-not-write"
    class PauseProvider(Provider):
        async def chat(self, **kwargs):
            service = WorkflowService(JsonWorkflowRepository(tmp_path / "project", "QA-team"))
            run = service.repository.list_runs()[0]
            if control in {"clarify", "new_attempt"}:
                service.request_interaction(run.run_id, command_id="ask", interaction={
                    "interaction_id": "question", "interaction_type": "clarify_requirements",
                    "title": "Clarify", "prompt": "Stop before further execution",
                    "response_schema": {"type": "object"}})
                if control == "new_attempt":
                    service.command_run(run.run_id, command_id="answer", command_type="submit_interaction",
                        payload={"interaction_id": "question", "response": {"selection": "yes"}})
                    service.claim_run(run.run_id, lease_owner="new", agent_profile="research")
            else:
                service.command_run(run.run_id, command_id="control-child", command_type=control)
            return await super().chat(**kwargs)
    child = PauseProvider([call("write_file", path=str(target), content="bad")])
    def factory(role):
        return (supervisor if role == "supervisor" else child), f"test/{role}", (f"test/{role}",)
    _, service = await run_native(tmp_path, supervisor, factory, stage="experiment")
    assert not target.exists()
    assert service.repository.list_runs()[0].status == status
    assert len(child.calls) == 1


async def test_pending_approval_cannot_be_bypassed_via_student(tmp_path):
    supervisor = Provider([call("consult", role="student", task="Try before approval"),
        call("request_workflow_interaction", interaction_type="approve_plan", title="Approve?",
            prompt="Review", choices=[{"id": "approve", "label": "Approve"}], allow_freeform=True)])
    child = Provider()
    def factory(role):
        return (supervisor if role == "supervisor" else child), f"test/{role}", (f"test/{role}",)
    _, service = await run_native(tmp_path, supervisor, factory,
        module_data={"confirmation_policy": "required"})
    assert not child.calls
    assert service.repository.list_runs()[0].status == "waiting_input"


async def test_attempt_profile_wins_over_stale_message_profile(tmp_path):
    main, supervisor = Provider(), Provider([complete()])
    await run_native(tmp_path, main,
        lambda role: (supervisor, "test/supervisor", ("test/supervisor",)), metadata_profile="engineer")
    assert not main.calls


async def test_legacy_team_turn_also_uses_supervisor_model(tmp_path):
    main, supervisor = Provider(), Provider()
    loop = ResearchAgentLoop(bus=MessageBus(), provider=main, workspace=tmp_path,
        model="test/main", role_provider_factory=lambda role: (supervisor, "test/supervisor", ("test/supervisor",)))
    await loop.process_direct("Read-only planning check", channel="ui", chat_id="legacy-team",
        metadata={"agent_profile": "team", "run_mode": "manual"})
    assert supervisor.calls[0]["model"] == "test/supervisor"
    assert not main.calls
    assert supervisor.calls[0]["model"] == "test/supervisor"


async def test_role_provider_failure_does_not_silently_use_global_model(tmp_path):
    main = Provider()
    def fail(role):
        raise ValueError("Role not configured")
    _, service = await run_native(tmp_path, main, fail)
    assert service.repository.list_runs()[0].status == "failed"
    assert not main.calls


async def test_supervisor_fallback_preserves_role_provider(tmp_path):
    supervisor = Provider([LLMResponse(content="rate limit", finish_reason="error"),
                           LLMResponse(content="Fallback succeeded")])
    main = Provider()
    loop = ResearchAgentLoop(bus=MessageBus(), provider=main, workspace=tmp_path,
        model="test/main", provider_factory=lambda model: main,
        role_provider_factory=lambda role: (supervisor, "test/primary", ("test/primary", "test/backup")))
    route = loop._get_model_runtime("role-fallback", role="supervisor")
    _, selected = await route.resolve([])
    response, selected = await route.chat(selected, messages=[])
    assert response.content == "Fallback succeeded"
    assert [c["model"] for c in supervisor.calls] == ["test/primary", "test/backup"]
    assert not main.calls
