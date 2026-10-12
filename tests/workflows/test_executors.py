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


@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_experiment_mode_enforces_repeatable_boundaries(tmp_path: Path, monkeypatch, mode):
    monkeypatch.setenv("MIRA_HOME", str(tmp_path / "home"))
    service = _service(tmp_path)
    run_id = service.create_run(command_id="create", module_type="experiment", enqueue=True,
                                module_data={"experiment_execution_mode": mode}).run_id

    def experiment_turn(number):
        return LLMResponse(content=None, tool_calls=[
            ToolCallRequest(id=f"unregistered-{number}", name="exec", arguments={
                "command": f"touch '{service.repository.project_dir / f'unregistered-{number}.txt'}'",
            }),
            *[ToolCallRequest(id=f"{number}-{state}", name="record_workflow_experiment", arguments={
                "experiment": {"id": f"Exp00{number}", "title": f"Experiment {number}", "status": state},
            }) for state in ["pending", "running", "completed"]],
            ToolCallRequest(id=f"after-{number}", name="write_file", arguments={
                "path": str(service.repository.project_dir / f"after-{number}.txt"), "content": "continued",
            }),
        ])

    provider = _ScriptedProvider([
        experiment_turn(1),
        *([LLMResponse(content="First experiment done; I will continue later.")] if mode == "auto" else []),
        experiment_turn(2),
        LLMResponse(content=None, tool_calls=[ToolCallRequest(id="complete", name="complete_workflow_run", arguments={
            "summary": "Finished", "output_artifact_ids": [], "suggested_handoffs": [],
        })]),
    ])
    loop = ResearchAgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="dummy/native",
                             channels_config=ChannelsConfig(), exec_config=ExecToolConfig(enable=True, timeout=5),
                             session_manager=SessionManager(tmp_path))

    async def execute():
        service.claim_run(run_id, lease_owner="test")
        await loop.process_direct("Continue", session_key=f"ui:PRJ-native:run:{run_id}", channel="ui",
                                  chat_id="PRJ-native", metadata={"project_id": "PRJ-native",
                                  "project_dir": str(service.repository.project_dir), "workflow_run_id": run_id,
                                  "run_mode": "manual" if mode == "auto" else "auto"})

    await execute()
    if mode == "manual":
        for number, decision in [(1, "continue"), (2, "finish")]:
            run = service.repository.load_run(run_id)
            assert run.status == "waiting_input"
            assert len(provider.calls) == number
            assert not (service.repository.project_dir / f"after-{number}.txt").exists()
            assert not (service.repository.project_dir / f"unregistered-{number}.txt").exists()
            assert run.module_data["experiments"][-1]["status"] == "completed"
            interaction = run.module_data["interaction"]
            assert interaction["interaction_type"] == "confirm_experiment_continuation"
            service.command_run(run_id, command_id=f"answer-{number}", command_type="submit_interaction", payload={
                "interaction_id": interaction["interaction_id"], "response": {"selection": decision},
            })
            if decision == "finish":
                service.claim_run(run_id, lease_owner="test")
                from mira_engine.workflows import WorkflowError
                with pytest.raises(WorkflowError, match="confirm continuation"):
                    service.command_run(run_id, command_id="bad-next", command_type="record_experiment", payload={
                        "experiment": {"id": "Exp003", "title": "Unapproved", "status": "running"},
                    })
            await execute()
    else:
        assert (service.repository.project_dir / "after-1.txt").exists()
        assert (service.repository.project_dir / "after-2.txt").exists()
        assert (service.repository.project_dir / "unregistered-1.txt").exists()
    run = service.repository.load_run(run_id)
    assert run.status == "completed"
    assert len(run.module_data["experiments"]) == 2
    assert len(provider.calls) == (3 if mode == "manual" else 4)


@pytest.mark.parametrize("module_type", ["initiation", "planning", "experiment", "delivery"])
async def test_repeatable_clarifications_stop_batches_and_restore_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module_type: str,
) -> None:
    from mira_engine.workflows.confirmations import expected_confirmation_type

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    service = _service(tmp_path)
    run_id = service.create_run(
        command_id="create", module_type=module_type, enqueue=True,
        module_data={"intent": "Investigate useful options", "confirmation_policy": "required"},
    ).run_id or ""
    service.claim_run(run_id, lease_owner="test:initial")
    service.request_interaction(
        run_id, command_id="initial-approval", interaction={
            "interaction_id": "approved-brief", "interaction_type": expected_confirmation_type(module_type),
            "title": "Original scope", "prompt": "Use existing data only",
            "choices": [{"id": "approve", "label": "Approve"}],
            "response_schema": {"type": "object", "required": ["selection"]},
        },
    )
    service.command_run(run_id, command_id="approve", command_type="submit_interaction", payload={
        "interaction_id": "approved-brief", "response": {"selection": "approve"},
    })

    def question(kind: str, title: str, label: str) -> LLMResponse:
        return LLMResponse(content=None, tool_calls=[
            ToolCallRequest(id=kind, name="request_workflow_interaction", arguments={
                "interaction_type": kind, "title": title, "prompt": f"Decide: {title}",
                "choices": [{"id": "option_a", "label": label}], "allow_freeform": True,
            }),
            ToolCallRequest(id=f"bad-write-{kind}", name="write_file", arguments={
                "path": str(service.repository.project_dir / "must-not-exist.txt"),
                "content": "Must not write while waiting",
            }),
            ToolCallRequest(id=f"bad-{kind}", name="complete_workflow_run", arguments={
                "summary": "Must not complete while waiting", "output_artifact_ids": [],
                "suggested_handoffs": [],
            }),
        ])

    provider = _ScriptedProvider([
        question("clarify_requirements", "Which outcome?", "Feasible research candidates"),
        question("resolve_conflict", "Missing external cohort", "Use internal validation with caveats"),
        LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id="complete", name="complete_workflow_run", arguments={
                "summary": "Finished within clarified scope", "output_artifact_ids": [],
                "suggested_handoffs": [],
            },
        )]),
    ])
    loop = ResearchAgentLoop(
        bus=MessageBus(), provider=provider, workspace=tmp_path, model="dummy/native",
        channels_config=ChannelsConfig(), exec_config=ExecToolConfig(enable=True, timeout=5),
        session_manager=SessionManager(tmp_path),
    )
    for index in range(3):
        service.claim_run(run_id, lease_owner=f"test:turn-{index}")
        await loop.process_direct(
            "Continue", session_key=f"ui:{service.repository.project_id}:run:{run_id}",
            channel="ui", chat_id=service.repository.project_id, metadata={
                "loop_mode": "project", "project_id": service.repository.project_id,
                "project_dir": str(service.repository.project_dir), "workflow_run_id": run_id,
            },
        )
        assert len(provider.calls) == index + 1  # no extra model call after a question
        run = service.repository.load_run(run_id)
        assert not (service.repository.project_dir / "must-not-exist.txt").exists()
        if index < 2:
            assert run.status == "waiting_input"  # second tool in batch was blocked
            interaction = run.module_data["interaction"]
            service.command_run(run_id, command_id=f"answer-{index}", command_type="submit_interaction", payload={
                "interaction_id": interaction["interaction_id"],
                "response": {"selection": "option_a", "notes": "Keep the agreed scope"},
            })
        else:
            assert run.status == "completed"
            assert len(run.module_data["interaction_history"]) == 3
            prompt = provider.calls[-1]["messages"][0]["content"]
            assert "Feasible research candidates" in prompt
            assert "Missing external cohort" in prompt
            assert "Use internal validation with caveats" in prompt
            assert "Keep the agreed scope" in prompt


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
        agent_profile="research",
    )
    validation = await PlanningExecutor().validate_inputs(context)
    assert validation.valid is False
    assert "expected experiment_result" in validation.errors[0]


def test_executor_instructions_include_user_module_configuration(tmp_path: Path) -> None:
    service = _service(tmp_path)
    receipt = service.create_run(
        command_id="run-initiation-config",
        module_type="initiation",
        module_data={
            "intent": "Review the field",
            "literature_review": {
                "enabled": True,
                "sources": ["pubmed", "arxiv"],
            },
            "references": "doi:10.1000/example",
        },
    )
    run = service.repository.load_run(receipt.run_id or "")
    context = ModuleExecutionContext(
        run=run,
        artifacts=(),
        project_dir=service.repository.project_dir,
        user_content="Review the field",
        metadata={},
        agent_profile="research",
    )

    instructions = InitiationExecutor().system_instructions(context)

    assert '"configuration"' in instructions
    assert '"sources": [' in instructions
    assert '"pubmed"' in instructions
    assert '"references": "doi:10.1000/example"' in instructions
    assert '"intent": "Review the field"' in instructions
    assert "call record_initiation_research_result" in instructions
    assert "complete paper metadata, notes, gaps, and candidate directions" in instructions


@pytest.mark.parametrize(
    ("module_type", "interaction_type"),
    [
        ("initiation", "approve_research_scope"),
        ("planning", "approve_plan"),
        ("experiment", "approve_experiment_plan"),
        ("delivery", "approve_delivery_brief"),
    ],
)
def test_required_module_instructions_embed_the_local_confirmation_gate(
    tmp_path: Path,
    module_type: str,
    interaction_type: str,
) -> None:
    service = _service(tmp_path, project_id=f"PRJ-confirm-{module_type}")
    receipt = service.create_run(
        command_id=f"create-confirm-{module_type}",
        module_type=module_type,
        module_data={
            "intent": f"confirm {module_type}",
            "confirmation_policy": {"mode": "required"},
        },
    )
    run = service.repository.load_run(receipt.run_id or "")
    context = ModuleExecutionContext(
        run=run,
        artifacts=(),
        project_dir=service.repository.project_dir,
        user_content=f"Confirm {module_type}",
        metadata={},
        agent_profile="research",
    )

    instructions = ModuleExecutorRegistry().get(module_type).system_instructions(context)

    assert interaction_type in instructions
    assert '"required": true' in instructions
    assert "exactly two choices" in instructions
    assert "latest" in instructions


@pytest.mark.parametrize(
    ("module_type", "tool_name", "visible"),
    [
        ("initiation", "set_plan", False),
        ("planning", "exec", False),
        ("experiment", "exec", True),
        ("delivery", "set_plan", False),
        ("initiation", "record_workflow_experiment", False),
        ("planning", "record_workflow_experiment", False),
        ("experiment", "record_workflow_experiment", True),
        ("delivery", "record_workflow_experiment", False),
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
    assert ("record_initiation_research_result" in definitions) is (module_type == "initiation")
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


async def test_required_confirmation_blocks_completion_tool_until_interaction_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    service = _service(tmp_path, project_id="PRJ-confirmation-guard")
    receipt = service.create_run(
        command_id="create-confirmed-experiment",
        module_type="experiment",
        module_data={
            "intent": "run a controlled check",
            "confirmation_policy": {"mode": "required"},
        },
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
                        id="premature-completion",
                        name="complete_workflow_run",
                        arguments={
                            "summary": "Done too early",
                            "output_artifact_ids": [],
                            "suggested_handoffs": [],
                        },
                    ),
                    ToolCallRequest(
                        id="pending-experiment",
                        name="record_workflow_experiment",
                        arguments={"experiment": {
                            "id": "Exp001", "title": "Awaiting scope approval", "status": "pending",
                        }},
                    ),
                ],
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="confirmation-call",
                        name="request_workflow_interaction",
                        arguments={
                            "interaction_type": "approve_experiment_plan",
                            "title": "Approve experiment brief",
                            "prompt": "Hypothesis, method, budget, metrics, and risks.",
                            "choices": [
                                {"id": "approve", "label": "Approve", "value": "approve"},
                                {"id": "revise", "label": "Revise", "value": "revise"},
                            ],
                            "allow_freeform": True,
                        },
                    )
                ],
            ),
            LLMResponse(content="The Run is waiting for confirmation."),
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
        "Execute the experiment.",
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
    assert waiting.module_data["interaction"]["interaction_type"] == "approve_experiment_plan"
    assert waiting.module_data["experiments"][0]["status"] == "pending"
    assert "workflow_confirmation" in json.dumps(provider.calls[1]["messages"])


async def test_native_initiation_records_research_before_direction_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    service = _service(tmp_path, project_id="PRJ-initiation-structured")
    receipt = service.create_run(
        command_id="create-initiation-structured",
        module_type="initiation",
        module_data={"intent": "survey the evidence"},
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
                        id="research-result-call",
                        name="record_initiation_research_result",
                        arguments={
                            "survey": "Evidence synthesis.",
                            "references": [
                                {
                                    "id": "ref-1",
                                    "title": "Structured evidence",
                                    "year": "2026",
                                    "url": "https://doi.org/10.1000/structured",
                                    "summary": "Multicenter evidence.",
                                    "relevance": "Directly relevant.",
                                }
                            ],
                            "notes": ["One limitation."],
                            "gaps": ["One gap."],
                            "directions": [
                                {
                                    "id": "direction-1",
                                    "title": "Validate prospectively",
                                    "rationale": "Closes the gap.",
                                }
                            ],
                        },
                    )
                ],
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="direction-choice-call",
                        name="request_workflow_interaction",
                        arguments={
                            "interaction_type": "choose_direction",
                            "title": "Choose a direction",
                            "prompt": "Which direction should continue?",
                            "choices": [
                                {"id": "direction-1", "label": "Validate prospectively"}
                            ],
                            "allow_freeform": True,
                        },
                    )
                ],
            ),
            LLMResponse(content="The structured survey is ready for review."),
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
        "Survey this field.",
        session_key=f"ui:{service.repository.project_id}:run:{run_id}",
        channel="ui",
        chat_id=service.repository.project_id,
        metadata={
            "loop_mode": "project",
            "project_id": service.repository.project_id,
            "project_dir": str(service.repository.project_dir),
            "workflow_run_id": run_id,
            "workflow_module_type": "initiation",
            "run_mode": "manual",
        },
    )

    run = service.repository.load_run(run_id)
    assert run.status == "waiting_input"
    assert run.module_data["research_result"]["survey"] == "Evidence synthesis."
    assert run.module_data["research_result"]["references"][0]["title"] == "Structured evidence"
    assert run.module_data["interaction"]["interaction_type"] == "choose_direction"


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


@pytest.mark.parametrize(
    ("agent_profile", "prompt_marker", "team_tools_visible"),
    [
        ("research", "Scientific Tenacity", False),
        ("engineer", "results-oriented Machine Learning Engineering Assistant", False),
        ("team", "Bounded Debate Protocol", True),
    ],
)
async def test_native_executor_composes_profile_policy_and_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_profile: str,
    prompt_marker: str,
    team_tools_visible: bool,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    service = _service(tmp_path, project_id=f"PRJ-profile-{agent_profile}")
    receipt = service.create_run(
        command_id=f"create-profile-{agent_profile}",
        module_type="planning",
        module_data={"intent": "create an approved plan"},
        enqueue=True,
    )
    run_id = receipt.run_id or ""
    service.claim_run(
        run_id,
        lease_owner="test:native",
        agent_profile=agent_profile,
    )
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

    await loop.process_direct(
        "Prepare this planning module.",
        session_key=f"ui:{service.repository.project_id}:run:{run_id}",
        channel="ui",
        chat_id=service.repository.project_id,
        metadata={
            "loop_mode": "project",
            "project_id": service.repository.project_id,
            "project_dir": str(service.repository.project_dir),
            "workflow_run_id": run_id,
            "agent_profile": agent_profile,
        },
    )

    definitions = {
        item["function"]["name"] for item in provider.calls[0]["tools"]
    }
    assert ("consult" in definitions) is team_tools_visible
    assert ("spawn" in definitions) is team_tools_visible
    assert "exec" not in definitions
    system_prompt = provider.calls[0]["messages"][0]["content"]
    assert prompt_marker in system_prompt
    assert "Interactive Plan Mode" not in system_prompt
    assert "task_plan.json under plan.answers" not in system_prompt


async def test_native_executor_enriches_skill_audit_with_run_policy_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    service = _service(tmp_path, project_id="PRJ-native-audit")
    skill_path = service.repository.project_dir / "skills" / "audit-policy" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("# Audit policy\n", encoding="utf-8")
    receipt = service.create_run(
        command_id="create-audited-run",
        module_type="experiment",
        module_data={"intent": "inspect the audit policy"},
        enqueue=True,
    )
    run_id = receipt.run_id or ""
    service.claim_run(
        run_id,
        lease_owner="test:native",
        agent_profile="engineer",
    )
    provider = _ScriptedProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="audit-read",
                        name="read_file",
                        arguments={"path": "skills/audit-policy/SKILL.md"},
                    )
                ],
            ),
            LLMResponse(content="Policy inspected."),
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
    audit_events: list[dict[str, Any]] = []

    async def audit_hook(details: dict[str, Any]) -> None:
        audit_events.append(details)

    await loop.process_direct(
        "Inspect the policy.",
        session_key=f"ui:{service.repository.project_id}:run:{run_id}",
        channel="ui",
        chat_id=service.repository.project_id,
        metadata={
            "loop_mode": "project",
            "project_id": service.repository.project_id,
            "project_dir": str(service.repository.project_dir),
            "workflow_run_id": run_id,
            "agent_profile": "engineer",
        },
        audit_hook=audit_hook,
    )

    assert audit_events == [
        {
            "project_id": service.repository.project_id,
            "run_id": run_id,
            "module_type": "experiment",
            "executor_name": "mira.experiment",
            "executor_version": "1.0.0",
            "agent_profile": "engineer",
            "tool": "read_file",
            "skill_name": "audit-policy",
            "path": "skills/audit-policy/SKILL.md",
        }
    ]
