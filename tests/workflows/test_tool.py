from __future__ import annotations

import json
from pathlib import Path

import pytest

from mira_engine.modules.tools import (
    InitiationResearchResultTool,
    WorkflowArtifactTool,
    WorkflowCompletionTool,
    WorkflowInteractionTool,
)
from mira_engine.workflows import JsonWorkflowRepository, WorkflowError, WorkflowService


async def test_agent_interaction_tool_commits_a_waiting_checkpoint(tmp_path: Path) -> None:
    project_dir = tmp_path / "PRJ-tool"
    (project_dir / ".mira").mkdir(parents=True)
    (project_dir / ".mira" / "project.json").write_text(
        json.dumps({"schema_version": 1, "id": "PRJ-tool"}), encoding="utf-8"
    )
    service = WorkflowService(JsonWorkflowRepository(project_dir, "PRJ-tool"))
    created = service.create_run(command_id="cmd-create-tool", module_type="planning", enqueue=True)
    run_id = created.run_id or ""
    service.claim_run(run_id, lease_owner="agent:test")

    tool = WorkflowInteractionTool()
    tool.set_project_context("PRJ-tool", str(project_dir))
    tool.set_workflow_run_context(run_id)
    result = await tool.execute(
        interaction_type="approve_plan",
        title="Approve this plan",
        prompt="Should the experiment proceed?",
        choices=[{"id": "approve", "label": "Approve"}],
        allow_freeform=True,
    )

    payload = json.loads(result)
    run = service.repository.load_run(run_id)
    assert payload["status"] == "waiting_input"
    assert run.status == "waiting_input"
    assert run.checkpoint_ref == payload["checkpoint_id"]
    assert run.module_data["interaction"]["interaction_type"] == "approve_plan"


async def test_initiation_research_result_tool_persists_validated_structured_output(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "PRJ-research-result"
    (project_dir / ".mira").mkdir(parents=True)
    (project_dir / ".mira" / "project.json").write_text(
        json.dumps({"schema_version": 1, "id": "PRJ-research-result"}),
        encoding="utf-8",
    )
    service = WorkflowService(JsonWorkflowRepository(project_dir, "PRJ-research-result"))
    created = service.create_run(
        command_id="cmd-create-research-result",
        module_type="initiation",
        enqueue=True,
    )
    run_id = created.run_id or ""
    service.claim_run(run_id, lease_owner="agent:test")

    tool = InitiationResearchResultTool()
    tool.set_project_context("PRJ-research-result", str(project_dir))
    tool.set_workflow_run_context(run_id)
    response = json.loads(
        await tool.execute(
            survey="Current evidence is promising but heterogeneous.",
            references=[
                {
                    "id": "ref-1",
                    "title": "AI fibrosis staging",
                    "authors": "Example et al.",
                    "year": "2025",
                    "venue": "Radiology",
                    "url": "https://doi.org/10.1000/example",
                    "summary": "A multicenter validation study.",
                    "relevance": "Directly evaluates the target task.",
                }
            ],
            notes=["External validation remains limited."],
            gaps=["No prospective head-to-head comparison."],
            directions=[
                {
                    "id": "direction-1",
                    "title": "Prospective multimodal validation",
                    "rationale": "Addresses external validity.",
                    "tradeoffs": "Higher recruitment cost.",
                    "next_step": "Define sites and endpoints.",
                }
            ],
        )
    )

    run = service.repository.load_run(run_id)
    result = run.module_data["research_result"]
    assert response["status"] == "recorded"
    assert result["schema_version"] == 1
    assert result["references"][0]["title"] == "AI fibrosis staging"
    assert result["gaps"] == ["No prospective head-to-head comparison."]
    assert result["directions"][0]["id"] == "direction-1"

    with pytest.raises(WorkflowError, match="research_result does not match"):
        service.command_run(
            run_id,
            command_id="invalid-research-result",
            command_type="record_research_result",
            expected_revision=run.revision,
            payload={
                "research_result": {
                    "schema_version": 1,
                    "survey": "Invalid because the reference has no title.",
                    "references": [{"id": "broken"}],
                    "notes": [],
                    "gaps": [],
                    "directions": [],
                }
            },
        )


async def test_native_artifact_and_completion_tools_publish_run_outputs(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "PRJ-output-tool"
    (project_dir / ".mira").mkdir(parents=True)
    (project_dir / ".mira" / "project.json").write_text(
        json.dumps({"schema_version": 1, "id": "PRJ-output-tool"}),
        encoding="utf-8",
    )
    (project_dir / "report.md").write_text("# Final report\n", encoding="utf-8")
    service = WorkflowService(JsonWorkflowRepository(project_dir, "PRJ-output-tool"))
    created = service.create_run(
        command_id="cmd-create-output-tool",
        module_type="delivery",
        enqueue=True,
    )
    run_id = created.run_id or ""
    service.claim_run(run_id, lease_owner="agent:test")

    artifact_tool = WorkflowArtifactTool()
    artifact_tool.set_project_context("PRJ-output-tool", str(project_dir))
    artifact_tool.set_workflow_run_context(run_id)
    published = json.loads(
        await artifact_tool.execute(
            source_path="report.md",
            artifact_type="final_report",
            label="Final report",
            media_type="text/markdown",
        )
    )

    completion_tool = WorkflowCompletionTool()
    completion_tool.set_project_context("PRJ-output-tool", str(project_dir))
    completion_tool.set_workflow_run_context(run_id)
    result = json.loads(
        await completion_tool.execute(
            summary="Report delivered",
            output_artifact_ids=[published["artifact_id"]],
            suggested_handoffs=[],
        )
    )

    run = service.repository.load_run(run_id)
    assert result["status"] == "completed"
    assert run.status == "completed"
    assert run.outputs == [published["artifact_id"]]
