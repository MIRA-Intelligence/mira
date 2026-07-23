from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mira_engine.agent.research_loop import ResearchAgentLoop
from mira_engine.bus.queue import MessageBus
from mira_engine.config.schema import ChannelsConfig, ExecToolConfig
from mira_engine.modules import (
    DeliveryExecutor,
    ExperimentExecutor,
    InitiationExecutor,
    ModuleExecutionContext,
    ModuleExecutorRegistry,
    PlanningExecutor,
)
from mira_engine.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from mira_engine.session.manager import SessionManager
from mira_engine.workflows import JsonWorkflowRepository, WorkflowService


class _CapturingProvider(LLMProvider):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        return LLMResponse(content="native executor response")

    def get_default_model(self) -> str:
        return "dummy/native"


class _ScriptedProvider(_CapturingProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__()
        self.responses = list(responses)

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _service(tmp_path: Path, project_id: str = "PRJ-native") -> WorkflowService:
    project_dir = tmp_path / project_id
    (project_dir / ".mira").mkdir(parents=True)
    (project_dir / ".mira" / "project.json").write_text(
        json.dumps({"schema_version": 1, "id": project_id}),
        encoding="utf-8",
    )
    return WorkflowService(JsonWorkflowRepository(project_dir, project_id))


def test_registry_contains_four_distinct_native_executors() -> None:
    registry = ModuleExecutorRegistry()
    assert isinstance(registry.get("initiation"), InitiationExecutor)
    assert isinstance(registry.get("planning"), PlanningExecutor)
    assert isinstance(registry.get("experiment"), ExperimentExecutor)
    assert isinstance(registry.get("delivery"), DeliveryExecutor)
    assert {
        registry.get(module_type).descriptor.executor_name for module_type in registry.module_types
    } == {
        "mira.initiation",
        "mira.planning",
        "mira.experiment",
        "mira.delivery",
    }


async def test_executor_validates_selected_artifact_contract(tmp_path: Path) -> None:
    service = _service(tmp_path)
    source = service.repository.project_dir / "direction.md"
    source.write_text("direction", encoding="utf-8")
    artifact, _ = service.import_artifact(
        command_id="artifact-direction",
        source_path="direction.md",
        artifact_type="research_direction",
    )
    receipt = service.create_run(
        command_id="run-planning",
        module_type="planning",
        inputs=[
            {
                "artifact_id": artifact.artifact_id,
                "expected_type": "experiment_result",
            }
        ],
    )
    run = service.repository.load_run(receipt.run_id or "")
    context = ModuleExecutionContext(
        run=run,
        artifacts=(artifact,),
        project_dir=service.repository.project_dir,
        user_content="Plan this direction",
        metadata={},
    )
    validation = await PlanningExecutor().validate_inputs(context)
    assert validation.valid is False
    assert "expected experiment_result" in validation.errors[0]


@pytest.mark.parametrize(
    ("module_type", "tool_name", "visible"),
    [
        ("initiation", "set_plan", False),
        ("planning", "exec", False),
        ("experiment", "exec", True),
        ("delivery", "set_plan", False),
    ],
)
async def test_research_loop_routes_module_run_through_native_executor_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module_type: str,
    tool_name: str,
    visible: bool,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    service = _service(tmp_path, project_id=f"PRJ-{module_type}")
    receipt = service.create_run(
        command_id=f"create-{module_type}",
        module_type=module_type,
        module_data={"intent": f"native {module_type} task"},
        enqueue=True,
    )
    run_id = receipt.run_id or ""
    service.claim_run(run_id, lease_owner="test:native")
    provider = _CapturingProvider()
    loop = ResearchAgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="dummy/native",
        channels_config=ChannelsConfig(),
        exec_config=ExecToolConfig(enable=True, timeout=5),
        session_manager=SessionManager(tmp_path),
    )

    response = await loop.process_direct(
        "Work on this module only.",
        session_key=f"ui:{service.repository.project_id}:run:{run_id}",
        channel="ui",
        chat_id=service.repository.project_id,
        metadata={
            "loop_mode": "project",
            "project_id": service.repository.project_id,
            "project_dir": str(service.repository.project_dir),
            "workflow_run_id": run_id,
            "workflow_module_type": module_type,
            "run_mode": "auto",
        },
    )

    assert getattr(response, "content", "") == "native executor response"
    assert len(provider.calls) == 1
    definitions = {item["function"]["name"] for item in provider.calls[0]["tools"]}
    assert (tool_name in definitions) is visible
    assert "request_workflow_interaction" in definitions
    assert "complete_workflow_run" in definitions
    assert "set_plan" not in definitions
    system_prompt = provider.calls[0]["messages"][0]["content"]
    assert f'"module_type": "{module_type}"' in system_prompt
    assert "task_plan.json under plan.answers" not in system_prompt
    assert "Interactive Plan Mode — Mandatory Before Experiments" not in system_prompt


async def test_native_planning_executor_requests_durable_interaction_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    service = _service(tmp_path)
    receipt = service.create_run(
        command_id="create-planning-interaction",
        module_type="planning",
        module_data={"intent": "approve an analysis plan"},
        enqueue=True,
    )
    run_id = receipt.run_id or ""
    service.claim_run(run_id, lease_owner="test:native")
    provider = _ScriptedProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="interaction-call",
                        name="request_workflow_interaction",
                        arguments={
                            "interaction_type": "approve_plan",
                            "title": "Approve plan",
                            "prompt": "Should this plan be approved?",
                            "choices": [{"id": "approve", "label": "Approve"}],
                            "allow_freeform": True,
                        },
                    )
                ],
            ),
            LLMResponse(content="The Run is waiting for your approval."),
        ]
    )
    loop = ResearchAgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="dummy/native",
        channels_config=ChannelsConfig(),
        exec_config=ExecToolConfig(enable=True, timeout=5),
        session_manager=SessionManager(tmp_path),
    )

    await loop.process_direct(
        "Draft the plan and ask me to approve it.",
        session_key=f"ui:{service.repository.project_id}:run:{run_id}",
        channel="ui",
        chat_id=service.repository.project_id,
        metadata={
            "loop_mode": "project",
            "project_id": service.repository.project_id,
            "project_dir": str(service.repository.project_dir),
            "workflow_run_id": run_id,
        },
    )

    waiting = service.repository.load_run(run_id)
    assert waiting.status == "waiting_input"
    assert waiting.checkpoint_ref
    assert waiting.module_data["interaction"]["interaction_type"] == "approve_plan"
    attempt = waiting.attempts[-1]
    checkpoint = service.repository.load_checkpoint(run_id, waiting.checkpoint_ref)
    assert attempt.executor_name == "mira.planning"
    assert attempt.executor_version == "1.0.0"
    assert checkpoint.executor_name == attempt.executor_name
    assert checkpoint.executor_version == attempt.executor_version


async def test_native_delivery_executor_can_complete_its_own_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    service = _service(tmp_path)
    receipt = service.create_run(
        command_id="create-delivery-completion",
        module_type="delivery",
        module_data={"intent": "polish the supplied text"},
        enqueue=True,
    )
    run_id = receipt.run_id or ""
    service.claim_run(run_id, lease_owner="test:native")
    provider = _ScriptedProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="completion-call",
                        name="complete_workflow_run",
                        arguments={
                            "summary": "Polishing completed",
                            "output_artifact_ids": [],
                            "suggested_handoffs": [],
                        },
                    )
                ],
            ),
            LLMResponse(content="Delivery completed."),
        ]
    )
    loop = ResearchAgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="dummy/native",
        channels_config=ChannelsConfig(),
        exec_config=ExecToolConfig(enable=True, timeout=5),
        session_manager=SessionManager(tmp_path),
    )

    await loop.process_direct(
        "Finish this delivery.",
        session_key=f"ui:{service.repository.project_id}:run:{run_id}",
        channel="ui",
        chat_id=service.repository.project_id,
        metadata={
            "loop_mode": "project",
            "project_id": service.repository.project_id,
            "project_dir": str(service.repository.project_dir),
            "workflow_run_id": run_id,
        },
    )

    completed = service.repository.load_run(run_id)
    assert completed.status == "completed"
    assert completed.module_data["completion_summary"] == "Polishing completed"


async def test_native_executor_acknowledges_pause_before_next_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    service = _service(tmp_path)
    receipt = service.create_run(
        command_id="create-paused-experiment",
        module_type="experiment",
        module_data={"intent": "run a controlled check"},
        enqueue=True,
    )
    run_id = receipt.run_id or ""
    service.claim_run(run_id, lease_owner="test:native")
    service.command_run(run_id, command_id="request-pause", command_type="pause")
    provider = _ScriptedProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="blocked-read",
                        name="read_file",
                        arguments={"path": "does-not-exist.txt"},
                    )
                ],
            ),
            LLMResponse(content="Stopped at the requested boundary."),
        ]
    )
    loop = ResearchAgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="dummy/native",
        channels_config=ChannelsConfig(),
        exec_config=ExecToolConfig(enable=True, timeout=5),
        session_manager=SessionManager(tmp_path),
    )

    response = await loop.process_direct(
        "Continue the experiment.",
        session_key=f"ui:{service.repository.project_id}:run:{run_id}",
        channel="ui",
        chat_id=service.repository.project_id,
        metadata={
            "loop_mode": "project",
            "project_id": service.repository.project_id,
            "project_dir": str(service.repository.project_dir),
            "workflow_run_id": run_id,
        },
    )

    paused = service.repository.load_run(run_id)
    assert paused.status == "paused"
    assert paused.checkpoint_ref
    checkpoint = service.repository.load_checkpoint(run_id, paused.checkpoint_ref)
    assert checkpoint.executor_name == "mira.experiment"
    assert checkpoint.executor_version == "1.0.0"
    assert getattr(response, "metadata", {}).get("_workflow_control_ack") == "paused"
