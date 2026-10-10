from __future__ import annotations

import json
from pathlib import Path

import pytest

from mira_engine.modules.tools import WorkflowExperimentTool
from mira_engine.workflows import JsonWorkflowRepository, WorkflowError, WorkflowService
from mira_engine.workflows.experiments import project_experiments


def make_service(tmp_path: Path, module_type: str = "experiment", **data):
    service = WorkflowService(JsonWorkflowRepository(tmp_path, "test"))
    run_id = service.create_run(command_id="create", module_type=module_type, enqueue=True,
                                module_data=data).run_id
    service.claim_run(run_id, lease_owner="test")
    return service, run_id


def record(service, run_id, *, command_id="record", **changes):
    return service.command_run(run_id, command_id=command_id, command_type="record_experiment",
                               payload={"experiment": {"id": "Exp001", "title": "Baseline",
                                                        "status": "running", **changes}})


def test_upsert_progress_completion_and_projection(tmp_path):
    service, run_id = make_service(tmp_path, automation_policy={"maxExperiments": 1})
    record(service, run_id, hypothesis="Test a mechanism", method="Controlled comparison")
    receipt = record(service, run_id, command_id="finish", status="completed",
                     results={"metrics": {"mae": 0.7}, "artifacts": ["results.csv"]},
                     evidence_refs=[{"artifact": "plot.png"}])
    assert record(service, run_id, command_id="finish").disposition == "duplicate"
    run = service.repository.load_run(run_id)
    assert run.revision == receipt.run_revision
    assert len(run.module_data["experiments"]) == 1
    item = project_experiments(service.repository)[0]
    assert item["id"] == f"workflow:{run_id}:Exp001"
    assert item["hypothesis"] == "Test a mechanism"
    assert item["results"]["artifacts"] == ["results.csv"]  # refs must not mutate artifacts
    assert item["workflow_run_id"] == run_id
    assert not (tmp_path / "task_plan.json").exists()
    with pytest.raises(WorkflowError, match="maxExperiments"):
        record(service, run_id, command_id="extra", id="Exp002")
    service.command_run(run_id, command_id="complete", command_type="complete")


def test_cannot_complete_unfinished_experiment(tmp_path):
    service, run_id = make_service(tmp_path)
    record(service, run_id)
    with pytest.raises(WorkflowError, match="finish or explicitly skip"):
        service.command_run(run_id, command_id="complete", command_type="complete")


@pytest.mark.parametrize("path", ["../outside.csv", "/tmp/outside.csv"])
def test_artifact_path_cannot_escape_project(tmp_path, path):
    service, run_id = make_service(tmp_path)
    with pytest.raises(WorkflowError, match="escapes Project"):
        record(service, run_id, results={"artifacts": [path]})
    assert "experiments" not in service.repository.load_run(run_id).module_data


def test_records_do_not_bypass_approval(tmp_path):
    service, run_id = make_service(tmp_path, confirmation_policy={"mode": "required"})
    record(service, run_id, status="pending")
    with pytest.raises(WorkflowError, match="approve"):
        record(service, run_id, command_id="run")


@pytest.mark.parametrize("changes", [
    {"results": {"findings": {"not": "text"}}},
    {"results": {"metrics": []}},
    {"results": {"metrics": {"loss": float("nan")}}},
    {"post_mortem": {"five_whys": [{"why": "object"}]}},
])
def test_invalid_display_fields_rejected(tmp_path, changes):
    service, run_id = make_service(tmp_path)
    with pytest.raises(WorkflowError):
        record(service, run_id, **changes)
    assert "experiments" not in service.repository.load_run(run_id).module_data


def test_wrong_module_and_waiting_run_rejected(tmp_path):
    service, run_id = make_service(tmp_path, "initiation")
    with pytest.raises(WorkflowError, match="experiment Run"):
        record(service, run_id)
    service.request_interaction(run_id, command_id="question", interaction={
        "interaction_id": "data-question",
        "interaction_type": "clarify_requirements", "title": "Data?", "prompt": "Choose data",
        "response_schema": {"type": "string"},
    })
    with pytest.raises(WorkflowError):
        record(service, run_id, command_id="while-waiting")


def test_completed_history_can_be_backfilled_without_rerun(tmp_path):
    service, run_id = make_service(tmp_path)
    service.command_run(run_id, command_id="complete", command_type="complete",
                        payload={"summary": "Existing report"})
    before = service.repository.load_run(run_id)
    record(service, run_id, status="completed", conclusion="Recovered from existing evidence")
    after = service.repository.load_run(run_id)
    assert after.status == "completed"
    assert after.attempt == before.attempt
    assert after.outputs == before.outputs
    with pytest.raises(WorkflowError, match="terminal"):
        record(service, run_id, command_id="restart")


def test_unregistered_run_is_summary_not_artifact_experiments(tmp_path):
    service, run_id = make_service(tmp_path)
    source = tmp_path / "report.md"
    source.write_text("Report")
    artifact, _ = service.import_artifact(command_id="artifact", source_path="report.md", artifact_type="experiment_report")
    service.command_run(run_id, command_id="complete", command_type="complete",
                        payload={"summary": "Two simulations", "outputs": [artifact.artifact_id]})
    items = project_experiments(service.repository)
    assert len(items) == 1
    assert items[0]["experiment_id"] == "run-summary"
    assert items[0]["results"]["findings"] == "Two simulations"
    assert items[0]["results"]["artifacts"] == [artifact.payload_ref]


async def test_tool_records_only_in_active_experiment_context(tmp_path):
    tool = WorkflowExperimentTool()
    item = {"id": "Exp001", "title": "Baseline", "status": "running"}
    assert (await tool.execute(experiment=item)).startswith("Error:")
    service, run_id = make_service(tmp_path)
    tool.set_project_context("test", str(tmp_path))
    tool.set_workflow_run_context(run_id)
    result = json.loads(await tool.execute(experiment=item))
    assert result["status"] == "recorded"
    assert service.repository.load_run(run_id).module_data["experiments"][0]["id"] == "Exp001"
