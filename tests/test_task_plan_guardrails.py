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
