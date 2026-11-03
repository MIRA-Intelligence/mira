"""Pre-modular Projects (task_plan.json only) open as completed, imported ModuleRuns."""

from __future__ import annotations

import json
from pathlib import Path

from mira_engine.workflows import JsonWorkflowRepository, WorkflowService
from mira_engine.workflows.legacy import LegacyProjectImporter, legacy_import_done


def _legacy_plan() -> dict:
    return {
        "schema_version": 1,
        "title": "PRJ-0001: demo",
        "core_question": "Does X improve Y?",
        "status": "completed",
        "started_at": "2026-05-04T04:22:00Z",
        "contract_version": 2,
        "automation_policy": {"max_experiments": 3, "goals": ["r > 0.8"]},
        "research": {
            "survey": "X is known to help Y.",
            "notes": ["note one", "note two"],
            "references": [
                {"id": "R1", "title": "Paper", "url": "https://doi.org/10.1/x ; PubMed PMID: 1"},
                {"title": "", "url": "https://example.org/untitled"},
                "bare string reference",
            ],
        },
        "plan": {"phase": "approved", "draft": "1. run it", "answers": []},
        "experiments": [
            {
                "id": "Exp001", "title": "baseline", "status": "completed",
                "results": {"metrics": {"r": float("nan"), "mae": 1.5}, "findings": {"a": 1}},
                "post_mortem": {"five_whys": "because", "residual_analysis": ["x"]},
                "snapshot": {"unknown": True}, "started_at": "2026-05-04",
            },
            {"id": "Exp002", "title": "interrupted", "status": "running"},
            {"id": "Exp003", "title": "never ran", "status": "pending"},
            {"id": "Exp001.a", "title": "branch", "status": "completed"},
        ],
        "result": {
            "summary": "Wrote the report.", "output_path": "result/report.md", "output_type": "report",
        },
    }


def _project(tmp_path: Path, plan: dict | str | None = None) -> Path:
    project = tmp_path / "PRJ-0001"
    (project / "result").mkdir(parents=True)
    (project / "result" / "report.md").write_text("# Report\n", encoding="utf-8")
    raw = json.dumps(plan if plan is not None else _legacy_plan()) if not isinstance(plan, str) else plan
    (project / "task_plan.json").write_text(raw, encoding="utf-8")
    return project


def _service(project: Path) -> WorkflowService:
    return WorkflowService(JsonWorkflowRepository(project, project.name))


def test_each_populated_stage_becomes_a_completed_imported_run(tmp_path: Path) -> None:
    project = _project(tmp_path)
    before = (project / "task_plan.json").read_bytes()

    workflow, runs = _service(project).snapshot()

    assert [run.module_type for run in runs] == ["initiation", "planning", "experiment", "delivery"]
    assert {run.status for run in runs} == {"completed"}
    assert all(run.created_by.kind == "external_import" for run in runs)
    assert [run.created_at for run in runs] == sorted(run.created_at for run in runs)
    assert workflow.selected_run_id == runs[-1].run_id
    assert (project / "task_plan.json").read_bytes() == before
    assert (project / ".mira" / "legacy" / "task_plan.json").read_bytes() == before


def test_initiation_run_carries_a_valid_research_result_and_a_direction(tmp_path: Path) -> None:
    project = _project(tmp_path)
    initiation = _service(project).snapshot()[1][0]
    result = initiation.module_data["research_result"]

    assert result["schema_version"] == 1
    assert [ref["id"] for ref in result["references"]] == ["R1", "R2", "R3"]
    assert result["references"][1]["title"] == "https://example.org/untitled"
    assert result["notes"] == ["note one", "note two"]
    assert result["directions"][0]["title"] == "Does X improve Y?"
    repository = JsonWorkflowRepository(project, project.name)
    assert {repository.load_artifact(i).artifact_type for i in initiation.outputs} == {
        "initiation_research_result", "research_report",
    }


def test_experiments_are_coerced_to_the_run_contract_not_dropped(tmp_path: Path) -> None:
    project = _project(tmp_path)
    experiment = _service(project).snapshot()[1][2]
    records = {item["id"]: item for item in experiment.module_data["experiments"]}

    assert set(records) == {"Exp001", "Exp002", "Exp003", "Exp001.a"}
    assert records["Exp001"]["results"]["metrics"] == {"r": None, "mae": 1.5}
    assert records["Exp001"]["results"]["findings"] == '{"a": 1}'
    assert records["Exp001"]["post_mortem"]["five_whys"] == ["because"]
    assert records["Exp001.a"]["parent"] == "Exp001"
    # A Run holds only terminal records: half-finished legacy work is not left "running".
    assert records["Exp002"]["status"] == "failed"
    assert records["Exp003"]["status"] == "skipped"
    assert experiment.module_data["automation_policy"]["maxExperiments"] == 3
    assert experiment.module_data["experiment_contract_version"] == 2


def test_delivery_publishes_the_existing_output_file(tmp_path: Path) -> None:
    project = _project(tmp_path)
    delivery = _service(project).snapshot()[1][3]
    repository = JsonWorkflowRepository(project, project.name)
    artifact = repository.load_artifact(delivery.outputs[0])

    assert artifact.artifact_type == "deliverable"
    assert repository.artifact_content_path(artifact).read_text(encoding="utf-8") == "# Report\n"
    assert delivery.module_data["completion_summary"] == "Wrote the report."


def test_runs_chain_through_artifacts_and_replay_as_events(tmp_path: Path) -> None:
    service = _service(_project(tmp_path))
    _, runs = service.snapshot()

    assert runs[0].inputs == []
    assert [item.artifact_id for item in runs[1].inputs] == runs[0].outputs
    events = service.list_events()
    assert [event.event_type for event in events] == ["run.imported"] * 4
    assert [event.project_sequence for event in events] == [1, 2, 3, 4]


def test_import_is_idempotent_and_leaves_new_runs_alone(tmp_path: Path) -> None:
    project = _project(tmp_path)
    service = _service(project)
    first = service.snapshot()[1]
    receipt = service.create_run(command_id="c1", module_type="initiation")
    again = _service(project).snapshot()[1]

    assert len(again) == len(first) + 1
    assert receipt.run_id in {run.run_id for run in again}
    assert not LegacyProjectImporter(JsonWorkflowRepository(project, project.name)).import_if_needed()


def test_create_run_imports_before_numbering_labels(tmp_path: Path) -> None:
    project = _project(tmp_path)
    service = _service(project)
    service.create_run(command_id="c1", module_type="experiment")

    labels = [run.label for run in service.snapshot()[1] if run.module_type == "experiment"]
    assert labels == ["Experiment (imported)", "Experiment 2"]


def test_projects_without_a_legacy_plan_are_untouched(tmp_path: Path) -> None:
    project = tmp_path / "fresh"
    project.mkdir()
    _, runs = _service(project).snapshot()

    assert runs == []
    assert not (project / ".mira" / "legacy_import.json").exists()


def test_unreadable_plan_does_not_block_the_project(tmp_path: Path) -> None:
    project = _project(tmp_path, "{ not json")
    _, runs = _service(project).snapshot()

    assert runs == []
    assert not legacy_import_done(JsonWorkflowRepository(project, project.name))


def test_plan_with_only_a_title_imports_nothing_but_completes(tmp_path: Path) -> None:
    project = _project(tmp_path, {"title": "empty", "status": "in_progress"})
    _, runs = _service(project).snapshot()

    assert runs == []
    assert legacy_import_done(JsonWorkflowRepository(project, project.name))


def test_crash_after_marker_is_cleaned_up_on_retry(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    repository = JsonWorkflowRepository(project, project.name)
    calls = {"n": 0}
    original = repository.save_run

    def flaky(run):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        original(run)

    monkeypatch.setattr(repository, "save_run", flaky)
    assert LegacyProjectImporter(repository).import_if_needed() is False
    assert not legacy_import_done(repository)

    monkeypatch.setattr(repository, "save_run", original)
    assert LegacyProjectImporter(repository).import_if_needed() is True
    assert [run.module_type for run in repository.list_runs()] == [
        "initiation", "planning", "experiment", "delivery",
    ]
    assert len(list(repository.artifacts_dir.glob("*/artifact.json"))) == 5


def test_legacy_experiments_link_to_their_run_without_duplicating(tmp_path: Path) -> None:
    from mira_engine.workflows.experiments import project_experiments
    from mira_engine.workflows.legacy import link_legacy_experiments

    project = _project(tmp_path)
    repository = JsonWorkflowRepository(project, project.name)
    _service(project).snapshot()
    plan = json.loads((project / "task_plan.json").read_text(encoding="utf-8"))

    remaining = link_legacy_experiments(plan, project_experiments(repository))

    experiment_run = next(run for run in repository.list_runs() if run.module_type == "experiment")
    assert all(item["workflow_run_id"] == experiment_run.run_id for item in plan["experiments"])
    assert [item["experiment_id"] for item in plan["experiments"]] == ["Exp001", "Exp002", "Exp003", "Exp001.a"]
    assert remaining == []


def test_imported_records_surface_when_the_legacy_file_lists_no_experiments(tmp_path: Path) -> None:
    from mira_engine.workflows.experiments import project_experiments
    from mira_engine.workflows.legacy import link_legacy_experiments

    project = _project(tmp_path)
    repository = JsonWorkflowRepository(project, project.name)
    _service(project).snapshot()

    records = link_legacy_experiments({"experiments": []}, project_experiments(repository))
    assert len(records) == 4


def test_imported_runs_carry_the_lineage_the_ui_scopes_by(tmp_path: Path) -> None:
    runs = {run.module_type: run for run in _service(_project(tmp_path)).snapshot()[1]}

    experiment = runs["experiment"].module_data
    assert experiment["parent_run_id"] == runs["initiation"].run_id
    assert experiment["upstream_selection"]["item_id"] == "legacy-core-question"
    assert runs["initiation"].module_data["research_result"]["directions"][0]["id"] == "legacy-core-question"
    delivery = runs["delivery"].module_data
    assert delivery["parent_run_id"] == runs["experiment"].run_id
    assert "item_id" not in delivery["upstream_selection"]
    assert "parent_run_id" not in runs["initiation"].module_data


def test_projects_imported_before_lineage_existed_are_repaired_once(tmp_path: Path) -> None:
    project = _project(tmp_path)
    repository = JsonWorkflowRepository(project, project.name)
    _service(project).snapshot()
    # Recreate what the first release wrote: no lineage, marker version 1.
    for run in repository.list_runs():
        run.module_data.pop("parent_run_id", None)
        run.module_data.pop("upstream_selection", None)
        repository.save_run(run)
    marker_path = project / ".mira" / "legacy_import.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker_path.write_text(json.dumps({**marker, "version": 1}), encoding="utf-8")

    runs = {run.module_type: run for run in _service(project).snapshot()[1]}

    assert runs["experiment"].module_data["parent_run_id"] == runs["initiation"].run_id
    assert runs["delivery"].module_data["parent_run_id"] == runs["experiment"].run_id
    assert json.loads(marker_path.read_text(encoding="utf-8"))["version"] == 2
