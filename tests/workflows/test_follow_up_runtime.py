"""A chat message on a completed ModuleRun is a real follow-up turn."""

import json
from typing import Any

import pytest

from mira_engine.agent.research_loop import ResearchAgentLoop
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
        if not self.responses:
            return LLMResponse(content="Done")
        response = self.responses.pop(0)
        # A callable reply can depend on earlier tool results.
        return response(kwargs) if callable(response) else response

    def get_default_model(self):
        return "test/default"


def call(name, **arguments):
    return LLMResponse(content=None, tool_calls=[ToolCallRequest(id=name, name=name, arguments=arguments)])


@pytest.fixture(autouse=True)
def isolate_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_HOME", str(tmp_path / "global"))


def completed_run(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "report.md").write_text("# Report\n", encoding="utf-8")
    service = WorkflowService(JsonWorkflowRepository(project_dir, "QA-follow"))
    run_id = service.create_run(command_id="create", module_type="delivery", enqueue=True).run_id or ""
    service.claim_run(run_id, lease_owner="test")
    first, _ = service.import_artifact(command_id="import", source_path="report.md", artifact_type="report")
    service.command_run(run_id, command_id="complete", command_type="complete", payload={
        "outputs": [first.artifact_id], "summary": "PREVIOUS COMPLETION SUMMARY",
    })
    return project_dir, service, run_id, first.artifact_id


async def follow_up(tmp_path, project_dir, service, run_id, provider, text):
    service.prepare_for_chat(run_id, text=text)
    loop = ResearchAgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
        model="test/main", channels_config=ChannelsConfig(),
        exec_config=ExecToolConfig(enable=True, timeout=5), session_manager=SessionManager(tmp_path))
    message = InboundMessage(content=text, channel="ui", chat_id="QA-follow", sender_id="user", metadata={
        "loop_mode": "project", "project_id": "QA-follow", "project_dir": str(project_dir),
        "workflow_run_id": run_id, "agent_profile": "research",
        "_workflow_chat_instructions": "FOLLOW-UP NOTE: the Run stays completed.",
    })
    return await loop._process_message(message, session_key=f"ui:QA-follow:run:{run_id}")


async def test_unrelated_message_on_completed_run_is_answered_by_the_model(tmp_path):
    project_dir, service, run_id, artifact_id = completed_run(tmp_path)
    provider = Provider([LLMResponse(content="Paris is the capital of France.")])

    response = await follow_up(tmp_path, project_dir, service, run_id, provider, "What is the capital of France?")

    # The turn used to end before any model call, echoing the completion summary.
    assert response is not None
    assert response.content == "Paris is the capital of France."
    assert "PREVIOUS COMPLETION SUMMARY" not in response.content
    assert len(provider.calls) == 1
    system = provider.calls[0]["messages"][0]["content"]
    assert "FOLLOW-UP NOTE: the Run stays completed." in system
    tool_names = {tool["function"]["name"] for tool in provider.calls[0]["tools"]}
    assert "request_workflow_interaction" not in tool_names
    run = service.repository.load_run(run_id)
    assert run.status == "completed"
    assert run.outputs == [artifact_id]


async def test_follow_up_turn_can_replace_a_deliverable(tmp_path):
    project_dir, service, run_id, _ = completed_run(tmp_path)
    (project_dir / "report-v2.md").write_text("# Polished\n", encoding="utf-8")

    def complete_with_published(kwargs):
        published = next(m["content"] for m in kwargs["messages"] if m["role"] == "tool")
        return call("complete_workflow_run", summary="Polished",
                    output_artifact_ids=[json.loads(published)["artifact_id"]], suggested_handoffs=[])

    provider = Provider([
        call("publish_workflow_artifact", source_path="report-v2.md", artifact_type="report",
             label="Report (polished)", media_type="text/markdown"),
        complete_with_published,
    ])

    response = await follow_up(tmp_path, project_dir, service, run_id, provider, "Polish the report")

    run = service.repository.load_run(run_id)
    assert response is not None
    assert run.status == "completed"
    assert len(run.outputs) == 1
    assert service.repository.load_artifact(run.outputs[0]).label == "Report (polished)"
    assert run.attempt.number == 1
