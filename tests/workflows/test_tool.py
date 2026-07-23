from __future__ import annotations

import json
from pathlib import Path

from mira_engine.modules.tools import (
    WorkflowArtifactTool,
    WorkflowCompletionTool,
    WorkflowInteractionTool,
)
from mira_engine.workflows import JsonWorkflowRepository, WorkflowService


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
