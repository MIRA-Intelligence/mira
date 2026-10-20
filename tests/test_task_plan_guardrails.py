import json
from pathlib import Path
from types import SimpleNamespace

from mira_engine.task_plan.guardrails import get_task_plan_contract, guard_task_plan_file


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


def test_guard_task_plan_recovers_experiment_results_json(tmp_path: Path) -> None:
    project_dir = tmp_path / "PRJ-0001B"
    (project_dir / "experiments" / "exp001").mkdir(parents=True)
    (project_dir / "experiments" / "exp001" / "results.json").write_text(
        json.dumps({"mean_r": 0.73, "mean_r2": 0.53, "best_transform": "log"}),
        encoding="utf-8",
    )
    (project_dir / "task_plan.json").write_text(
        json.dumps(
            {
                "title": "demo",
                "status": "in_progress",
                "current_experiment": "Exp001",
                "experiments": [{"id": "Exp001", "status": "running"}],
            }
        ),
        encoding="utf-8",
    )

    result = guard_task_plan_file(project_dir, auto_fix=True)
    assert result["ok"] is True
    assert result["fixed"] is True

    repaired = json.loads((project_dir / "task_plan.json").read_text(encoding="utf-8"))
    exp = repaired["experiments"][0]
    assert exp["status"] == "completed"
    assert exp["results"]["metrics"]["mean_r"] == 0.73
    assert "experiments/exp001/results.json" in exp["results"]["artifacts"]


def test_guard_task_plan_recovers_metrics_from_noncanonical_json(tmp_path: Path) -> None:
    project_dir = tmp_path / "PRJ-0001C"
    (project_dir / "analysis").mkdir(parents=True)
    (project_dir / "analysis" / "exp001_metrics_dump.json").write_text(
        json.dumps(
            {
                "base": {"mean_r": 0.70, "mean_r2": 0.50},
                "tuned": {"mean_r": 0.74, "mean_r2": 0.55},
            }
        ),
        encoding="utf-8",
    )
    (project_dir / "task_plan.json").write_text(
        json.dumps(
            {
                "title": "demo",
                "status": "in_progress",
                "experiments": [{"id": "Exp001", "status": "pending"}],
            }
        ),
        encoding="utf-8",
    )

    result = guard_task_plan_file(project_dir, auto_fix=True)
    assert result["ok"] is True
    assert result["fixed"] is True

    repaired = json.loads((project_dir / "task_plan.json").read_text(encoding="utf-8"))
    exp = repaired["experiments"][0]
    assert exp["status"] == "completed"
    assert exp["results"]["metrics"]["tuned"]["mean_r"] == 0.74
    assert "analysis/exp001_metrics_dump.json" in exp["results"]["artifacts"]


def test_guard_task_plan_strict_mode_requires_model_completion_for_recovered_experiment(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "PRJ-0001D"
    (project_dir / ".mira").mkdir(parents=True)
    (project_dir / ".mira" / "project.json").write_text(
        json.dumps({"agent_profile": "research", "contract_version": 2}),
        encoding="utf-8",
    )
    (project_dir / "experiments" / "exp001").mkdir(parents=True)
    (project_dir / "experiments" / "exp001" / "results.json").write_text(
        json.dumps({"mean_r": 0.71, "mean_r2": 0.52}),
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
                        "status": "running",
                        "question": "Q",
                        "hypothesis": "H",
                        "method": "M",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = guard_task_plan_file(project_dir, auto_fix=True)
    assert result["ok"] is False
    assert result["blocking"] is True

    repaired = json.loads((project_dir / "task_plan.json").read_text(encoding="utf-8"))
    exp = repaired["experiments"][0]
    assert exp["status"] == "completed"
    assert "theoretical_proof" not in exp
    assert "post_mortem" not in exp
    assert "evidence_refs" not in exp


def test_guard_task_plan_strict_mode_does_not_auto_fill_completed_fields(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "PRJ-0001F"
    (project_dir / ".mira").mkdir(parents=True)
    (project_dir / ".mira" / "project.json").write_text(
        json.dumps({"agent_profile": "research", "contract_version": 2}),
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
                        "question": "Q",
                        "hypothesis": "H",
                        "method": "M",
                        "results": {"metrics": {"mean_r": 0.7}},
                        "conclusion": "done",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = guard_task_plan_file(project_dir, auto_fix=True)
    assert result["ok"] is False
    assert result["blocking"] is True

    repaired = json.loads((project_dir / "task_plan.json").read_text(encoding="utf-8"))
    exp = repaired["experiments"][0]
    assert exp["status"] == "completed"
    assert "theoretical_proof" not in exp
    assert "post_mortem" not in exp
    assert "evidence_refs" not in exp


def test_guard_task_plan_strict_mode_does_not_promote_artifact_only_experiment(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "PRJ-0001G"
    (project_dir / ".mira").mkdir(parents=True)
    (project_dir / ".mira" / "project.json").write_text(
        json.dumps({"agent_profile": "research", "contract_version": 2}),
        encoding="utf-8",
    )
    (project_dir / "experiments" / "exp001").mkdir(parents=True)
    (project_dir / "experiments" / "exp001" / "training.log").write_text(
        "running...",
        encoding="utf-8",
    )
    (project_dir / "task_plan.json").write_text(
        json.dumps(
            {
                "title": "demo",
                "status": "in_progress",
                "experiments": [{"id": "Exp001", "status": "running"}],
            }
        ),
        encoding="utf-8",
    )

    result = guard_task_plan_file(project_dir, auto_fix=True)
    assert result["ok"] is True
    assert result["blocking"] is False
    repaired = json.loads((project_dir / "task_plan.json").read_text(encoding="utf-8"))
    exp = repaired["experiments"][0]
    assert exp["status"] == "running"
    assert "training.log" in " ".join(exp.get("results", {}).get("artifacts", []))


def test_guard_task_plan_strict_mode_rejects_guardrail_placeholder_fields(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "PRJ-0001H"
    (project_dir / ".mira").mkdir(parents=True)
    (project_dir / ".mira" / "project.json").write_text(
        json.dumps({"agent_profile": "research", "contract_version": 2}),
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
                        "question": "Q",
                        "hypothesis": "H",
                        "method": "M",
                        "results": {"metrics": {"mean_r": 0.8}},
                        "conclusion": "done",
                        "theoretical_proof": "Guardrail auto-fill: placeholder",
                        "isolation_test": {"control": "ok", "treatment": "ok", "isolated_variable": "ok"},
                        "post_mortem": {
                            "residual_analysis": "Guardrail auto-fill: placeholder",
                            "implementation_fidelity": "ok",
                            "five_whys": "ok",
                        },
                        "evidence_refs": [{"artifact": "task_plan.json"}],
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


def test_guard_task_plan_recovers_from_git_commit_when_no_metrics_json(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_dir = tmp_path / "PRJ-0001E"
    (project_dir / "data").mkdir(parents=True)
    (project_dir / "data" / "fitted_params.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (project_dir / "task_plan.json").write_text(
        json.dumps(
            {
                "title": "demo",
                "status": "in_progress",
                "experiments": [{"id": "Exp001", "status": "pending"}],
            }
        ),
        encoding="utf-8",
    )

    def fake_run(cmd, check, capture_output, text):  # noqa: ANN001
        if "log" in cmd:
            return SimpleNamespace(stdout="abc123\tExp001: recovered via git", returncode=0)
        if "show" in cmd:
            return SimpleNamespace(stdout="data/fitted_params.csv\n", returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr("mira_engine.task_plan.guardrails.subprocess.run", fake_run)

    result = guard_task_plan_file(project_dir, auto_fix=True)
    assert result["ok"] is True
    assert result["blocking"] is False

    repaired = json.loads((project_dir / "task_plan.json").read_text(encoding="utf-8"))
    exp = repaired["experiments"][0]
    assert exp["status"] == "completed"
    assert "data/fitted_params.csv" in exp["results"]["artifacts"]


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


def test_guard_task_plan_keeps_project_in_progress_without_final_result(tmp_path: Path) -> None:
    project_dir = tmp_path / "PRJ-0003A"
    project_dir.mkdir(parents=True)
    (project_dir / "task_plan.json").write_text(
        json.dumps(
            {
                "title": "completed too early",
                "status": "completed",
                "experiments": [
                    {
                        "id": "Exp001",
                        "title": "done",
                        "status": "completed",
                        "conclusion": "Finished analysis but no export was requested.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = guard_task_plan_file(project_dir, auto_fix=True)
    assert result["ok"] is True
    assert result["fixed"] is True

    repaired = json.loads((project_dir / "task_plan.json").read_text(encoding="utf-8"))
    assert repaired["status"] == "in_progress"


def test_guard_task_plan_reopens_completed_project_when_pending_work_exists(tmp_path: Path) -> None:
    project_dir = tmp_path / "PRJ-0003AA"
    project_dir.mkdir(parents=True)
    (project_dir / "task_plan.json").write_text(
        json.dumps(
            {
                "title": "replanned after export",
                "status": "completed",
                "current_experiment": "Exp001",
                "experiments": [
                    {"id": "Exp001", "title": "done", "status": "completed", "conclusion": "done"},
                    {"id": "Exp002", "title": "next", "status": "pending"},
                ],
                "result": {
                    "output_path": "result/report.md",
                    "output_type": "report",
                    "summary": "A prior export exists.",
                },
            }
        ),
        encoding="utf-8",
    )

    result = guard_task_plan_file(project_dir, auto_fix=True)
    assert result["ok"] is True
    assert result["fixed"] is True

    repaired = json.loads((project_dir / "task_plan.json").read_text(encoding="utf-8"))
    assert repaired["status"] == "in_progress"
    assert repaired["current_experiment"] == "Exp002"


def test_guard_task_plan_allows_existing_noncanonical_artifacts(tmp_path: Path) -> None:
    project_dir = tmp_path / "PRJ-0003B"
    (project_dir / "data").mkdir(parents=True)
    (project_dir / "data" / "extraction_report.json").write_text(
        json.dumps({"rows": 599}),
        encoding="utf-8",
    )
    (project_dir / "task_plan.json").write_text(
        json.dumps(
            {
                "title": "artifacts",
                "status": "in_progress",
                "experiments": [
                    {
                        "id": "Exp001",
                        "status": "completed",
                        "conclusion": "done",
                        "results": {
                            "metrics": {"r2": 0.4},
                            "artifacts": ["data/extraction_report.json"],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = guard_task_plan_file(project_dir, auto_fix=False)
    assert result["ok"] is True
    assert result["blocking"] is False


def test_guard_task_plan_allows_glob_artifact_paths_with_matching_files(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "PRJ-GLOB"
    out_dir = project_dir / "experiments" / "outputs" / "exp010"
    out_dir.mkdir(parents=True)
    for name in (
        "predictions_lovo_A_full.csv",
        "predictions_lovo_B1_full.csv",
        "predictions_lovo_B2_no_avail.csv",
    ):
        (out_dir / name).write_text("row\n", encoding="utf-8")
    (project_dir / "task_plan.json").write_text(
        json.dumps(
            {
                "title": "glob artifacts",
                "status": "in_progress",
                "experiments": [
                    {
                        "id": "Exp010",
                        "status": "completed",
                        "conclusion": "done",
                        "results": {
                            "metrics": {"auc": 0.9},
                            "artifacts": [
                                "experiments/outputs/exp010/predictions_lovo_*_*.csv"
                            ],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = guard_task_plan_file(project_dir, auto_fix=False)
    assert result["ok"] is True
    assert result["blocking"] is False


def test_guard_task_plan_missing_artifact_is_minor_and_tagged_not_blocking(
    tmp_path: Path,
) -> None:
    """A missing artifact reference is a minor issue: it must not halt the loop,
    and it is surfaced as a per-experiment ``guard_warnings`` marker."""
    project_dir = tmp_path / "PRJ-GLOB-MISS"
    (project_dir / "experiments" / "outputs" / "exp010").mkdir(parents=True)
    (project_dir / "task_plan.json").write_text(
        json.dumps(
            {
                "title": "glob artifacts missing",
                "status": "in_progress",
                "experiments": [
                    {
                        "id": "Exp010",
                        "status": "completed",
                        "conclusion": "done",
                        "results": {
                            "metrics": {"auc": 0.9},
                            "artifacts": [
                                "experiments/outputs/exp010/predictions_lovo_*_*.csv"
                            ],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = guard_task_plan_file(project_dir, auto_fix=True)
    assert result["blocking"] is False
    assert result["ok"] is True
    assert any("does not exist" in issue for issue in result["minor_issues"])
    assert result["severe_issues"] == []

    plan = json.loads((project_dir / "task_plan.json").read_text())
    exp = plan["experiments"][0]
    assert exp["guard_warnings"]
    assert any("does not exist" in warning for warning in exp["guard_warnings"])


def test_guard_task_plan_blocks_on_unsafe_artifact_path(tmp_path: Path) -> None:
    """A path that escapes the workspace is severe and must block."""
    project_dir = tmp_path / "PRJ-UNSAFE"
    project_dir.mkdir(parents=True)
    (project_dir / "task_plan.json").write_text(
        json.dumps(
            {
                "title": "unsafe artifact",
                "status": "in_progress",
                "experiments": [
                    {
                        "id": "Exp001",
                        "status": "completed",
                        "conclusion": "done",
                        "results": {
                            "metrics": {"auc": 0.9},
                            "artifacts": ["../../etc/passwd"],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = guard_task_plan_file(project_dir, auto_fix=True)
    assert result["blocking"] is True
    assert any("unsafe artifact path" in issue for issue in result["severe_issues"])


def test_guard_task_plan_clears_stale_guard_warnings_when_resolved(
    tmp_path: Path,
) -> None:
    """Once the artifact exists, the stale warning marker is removed."""
    project_dir = tmp_path / "PRJ-GLOB-RESOLVE"
    out_dir = project_dir / "experiments" / "outputs" / "exp010"
    out_dir.mkdir(parents=True)
    (project_dir / "task_plan.json").write_text(
        json.dumps(
            {
                "title": "resolve",
                "status": "in_progress",
                "experiments": [
                    {
                        "id": "Exp010",
                        "status": "completed",
                        "conclusion": "done",
                        "guard_warnings": ["artifact path does not exist 'x.csv'"],
                        "results": {
                            "metrics": {"auc": 0.9},
                            "artifacts": [
                                "experiments/outputs/exp010/predictions_lovo_*_*.csv"
                            ],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (out_dir / "predictions_lovo_A_full.csv").write_text("row\n", encoding="utf-8")

    guard_task_plan_file(project_dir, auto_fix=True)
    plan = json.loads((project_dir / "task_plan.json").read_text())
    assert "guard_warnings" not in plan["experiments"][0]


def test_guard_task_plan_research_profile_contract_v1_allows_missing_evidence_fields(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "PRJ-0100"
    (project_dir / ".mira").mkdir(parents=True)
    (project_dir / ".mira" / "project.json").write_text(
        json.dumps({"agent_profile": "research", "contract_version": 1}),
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
    assert result["ok"] is True
    assert result["blocking"] is False
    assert result["issues"] == []


def test_guard_task_plan_contract_v1_does_not_block_completion_warnings(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "PRJ-0100A"
    (project_dir / ".mira").mkdir(parents=True)
    (project_dir / ".mira" / "project.json").write_text(
        json.dumps({"agent_profile": "default", "contract_version": 1}),
        encoding="utf-8",
    )
    (project_dir / "task_plan.json").write_text(
        json.dumps(
            {
                "title": "demo",
                "status": "in_progress",
                "experiments": [{"id": "Exp001", "status": "completed"}],
            }
        ),
        encoding="utf-8",
    )

    result = guard_task_plan_file(project_dir, auto_fix=False)
    assert result["ok"] is True
    assert result["blocking"] is False
    assert result["blocking_issues"] == []
    assert any(
        "completed experiment missing results/conclusion" in issue
        for issue in result["issues"]
    )
    assert result["repairable_issues"] == result["issues"]


def test_guard_task_plan_research_profile_contract_v2_requires_evidence_fields(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "PRJ-0100B"
    (project_dir / ".mira").mkdir(parents=True)
    (project_dir / ".mira" / "project.json").write_text(
        json.dumps({"agent_profile": "research", "contract_version": 2}),
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
    assert result["fatal_issues"] == []
    assert result["repairable_issues"]
    joined = "\n".join(result["issues"])
    assert "research profile missing required fields" in joined
    assert "hypothesis rejection requires fields" in joined


def test_guard_task_plan_engineer_profile_requires_reproducibility_fields(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "PRJ-0101"
    (project_dir / ".mira").mkdir(parents=True)
    (project_dir / ".mira" / "project.json").write_text(
        json.dumps({"agent_profile": "engineer", "contract_version": 2}),
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
    (project_dir / ".mira").mkdir(parents=True)
    (project_dir / ".mira" / "project.json").write_text(
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
    (project_dir / ".mira").mkdir(parents=True)
    (project_dir / ".mira" / "project.json").write_text(
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


def test_get_task_plan_contract_for_research_profile() -> None:
    compat_contract = get_task_plan_contract(profile="research", contract_version=1)
    assert compat_contract["required_completed_fields"] == []
    assert compat_contract["required_falsify_fields"] == []

    contract = get_task_plan_contract(profile="research", contract_version=2)
    assert contract["profile"] == "research"
    assert contract["contract_version"] == 2
    assert "theoretical_proof" in contract["required_completed_fields"]
    assert "evidence_refs" in contract["required_falsify_fields"]
    assert "falsif" in contract["falsify_keywords"]


def test_get_task_plan_contract_default_profile_uses_contract_version() -> None:
    v1 = get_task_plan_contract(profile="default", contract_version=1)
    v2 = get_task_plan_contract(profile="default", contract_version=2)
    assert v1["required_completed_fields"] == []
    assert v1["required_falsify_fields"] == []
    assert v2["required_completed_fields"] == [
        "question",
        "hypothesis",
        "method",
        "results",
        "conclusion",
    ]
    assert v2["required_falsify_fields"] == ["evidence_refs"]
