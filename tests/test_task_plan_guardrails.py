import json
from pathlib import Path

from medpilot.task_plan.guardrails import guard_task_plan_file


def test_guard_task_plan_auto_fixes_ids_and_recovers_metrics(tmp_path: Path) -> None:
    project_dir = tmp_path / "PRJ-0001"
    (project_dir / "experiments" / "exp001").mkdir(parents=True)
    (project_dir / "experiments" / "exp001" / "metrics.json").write_text(
        json.dumps({"overall_r2": 0.42}),
        encoding="utf-8",
    )
    (project_dir / "experiments" / "exp001" / "REPORT.md").write_text(
        "Recovered summary from markdown report.",
        encoding="utf-8",
    )
    (project_dir / "task_plan.json").write_text(
        json.dumps(
            {
                "title": "demo",
                "status": "in_progress",
                "experiments": [{"id": "exp1", "status": "completed"}],
            }
        ),
        encoding="utf-8",
    )

    result = guard_task_plan_file(project_dir, auto_fix=True)
    assert result["ok"] is True
    assert result["fixed"] is True
    assert result["blocking"] is False

    repaired = json.loads((project_dir / "task_plan.json").read_text(encoding="utf-8"))
    exp = repaired["experiments"][0]
    assert exp["id"] == "Exp001"
    assert exp["results"]["metrics"] == {"overall_r2": 0.42}
    assert "experiments/exp001/metrics.json" in exp["results"]["artifacts"]
    assert exp["conclusion"]
    assert repaired["schema_version"] == 1


def test_guard_task_plan_reports_blocking_for_invalid_json(tmp_path: Path) -> None:
    project_dir = tmp_path / "PRJ-0002"
    project_dir.mkdir(parents=True)
    (project_dir / "task_plan.json").write_text("{", encoding="utf-8")

    result = guard_task_plan_file(project_dir, auto_fix=True)
    assert result["ok"] is False
    assert result["blocking"] is True
    assert any("failed to parse task_plan.json" in issue for issue in result["issues"])


def test_guard_task_plan_auto_fixes_duplicate_experiment_ids(tmp_path: Path) -> None:
    project_dir = tmp_path / "PRJ-0003"
    project_dir.mkdir(parents=True)
    (project_dir / "task_plan.json").write_text(
        json.dumps(
            {
                "title": "duplicate ids",
                "status": "in_progress",
                "current_experiment": "Exp003",
                "experiments": [
                    {"id": "Exp003", "title": "first", "status": "completed", "conclusion": "done"},
                    {"id": "Exp003", "title": "second", "status": "pending"},
                    {"id": "Exp004", "title": "third", "status": "pending"},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = guard_task_plan_file(project_dir, auto_fix=True)
    assert result["ok"] is True
    assert result["fixed"] is True
    assert result["blocking"] is False

    repaired = json.loads((project_dir / "task_plan.json").read_text(encoding="utf-8"))
    ids = [exp["id"] for exp in repaired["experiments"]]
    assert ids == ["Exp003", "Exp004", "Exp005"]
    assert len(ids) == len(set(ids))


def test_guard_task_plan_research_profile_requires_evidence_fields(tmp_path: Path) -> None:
    project_dir = tmp_path / "PRJ-0100"
    (project_dir / ".medpilot").mkdir(parents=True)
    (project_dir / ".medpilot" / "project.json").write_text(
        json.dumps({"agent_profile": "research"}),
        encoding="utf-8",
    )
    (project_dir / "task_plan.json").write_text(
        json.dumps(
            {
                "title": "demo",
                "status": "in_progress",
                "experiments": [
                    {
                        "id": "Exp001",
                        "status": "completed",
                        "results": {"metrics": {"r2": 0.1}},
                        "conclusion": "Hypothesis was rejected based on this run.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = guard_task_plan_file(project_dir, auto_fix=False)
    assert result["ok"] is False
    assert result["blocking"] is True
    joined = "\n".join(result["issues"])
    assert "research profile missing required fields" in joined
    assert "hypothesis rejection requires fields" in joined


def test_guard_task_plan_engineer_profile_requires_reproducibility_fields(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "PRJ-0101"
    (project_dir / ".medpilot").mkdir(parents=True)
    (project_dir / ".medpilot" / "project.json").write_text(
        json.dumps({"agent_profile": "engineer"}),
        encoding="utf-8",
    )
    (project_dir / "task_plan.json").write_text(
        json.dumps(
            {
                "title": "demo",
                "status": "in_progress",
                "experiments": [
                    {
                        "id": "Exp001",
                        "status": "completed",
                        "results": {"metrics": {"r2": 0.2}},
                        "conclusion": "Implementation failed and hypothesis rejected.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = guard_task_plan_file(project_dir, auto_fix=False)
    assert result["ok"] is False
    assert result["blocking"] is True
    joined = "\n".join(result["issues"])
    assert "engineer profile missing required fields" in joined
    assert "hypothesis rejection requires fields" in joined


def test_guard_task_plan_default_profile_requires_evidence_refs_for_rejection(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "PRJ-0102"
    (project_dir / ".medpilot").mkdir(parents=True)
    (project_dir / ".medpilot" / "project.json").write_text(
        json.dumps({"agent_profile": "default"}),
        encoding="utf-8",
    )
    (project_dir / "task_plan.json").write_text(
        json.dumps(
            {
                "title": "demo",
                "status": "in_progress",
                "experiments": [
                    {
                        "id": "Exp001",
                        "status": "completed",
                        "results": {"metrics": {"r2": 0.3}},
                        "conclusion": "Hypothesis rejected due to metric collapse.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = guard_task_plan_file(project_dir, auto_fix=False)
    assert result["ok"] is False
    assert result["blocking"] is True
    assert any("hypothesis rejection requires fields" in issue for issue in result["issues"])


def test_guard_task_plan_default_profile_contract_v2_requires_core_fields(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "PRJ-0103"
    (project_dir / ".medpilot").mkdir(parents=True)
    (project_dir / ".medpilot" / "project.json").write_text(
        json.dumps({"agent_profile": "default", "contract_version": 2}),
        encoding="utf-8",
    )
    (project_dir / "task_plan.json").write_text(
        json.dumps(
            {
                "title": "demo",
                "status": "in_progress",
                "experiments": [
                    {
                        "id": "Exp001",
                        "status": "completed",
                        "results": {"metrics": {"r2": 0.4}},
                        "conclusion": "baseline done",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = guard_task_plan_file(project_dir, auto_fix=False)
    assert result["ok"] is False
    assert result["blocking"] is True
    assert any("default profile missing required fields" in issue for issue in result["issues"])
