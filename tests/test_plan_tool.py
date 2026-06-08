import json
from pathlib import Path

import pytest

from mira_engine.agent.tools.plan import SetPlanTool


def _read_plan(project_dir: Path) -> dict:
    return json.loads((project_dir / "task_plan.json").read_text(encoding="utf-8"))["plan"]


@pytest.mark.asyncio
async def test_set_plan_questions_start_new_round(tmp_path: Path) -> None:
    project_dir = tmp_path / "PRJ-PLAN"
    project_dir.mkdir()
    (project_dir / "task_plan.json").write_text(
        json.dumps(
            {
                "title": "demo",
                "plan": {
                    "phase": "approved",
                    "questions": [{"id": "q1", "prompt": "old", "kind": "text"}],
                    "answers": {"q1": "old answer"},
                    "draft": {"summary": "old draft", "experiments": [{"title": "old exp"}]},
                    "feedback": "old feedback",
                },
                "experiments": [],
            }
        ),
        encoding="utf-8",
    )

    result = await SetPlanTool(str(project_dir)).execute(
        phase="questions",
        questions=[{"id": "q1", "prompt": "new", "kind": "single", "options": ["A", "B"]}],
    )

    assert "Saved 1 plan question" in result
    plan = _read_plan(project_dir)
    assert plan["phase"] == "questions"
    assert plan["questions"][0]["prompt"] == "new"
    assert "answers" not in plan
    assert "draft" not in plan
    assert "feedback" not in plan
    assert isinstance(plan["updated_at"], str)


@pytest.mark.asyncio
async def test_set_plan_draft_keeps_answers_but_clears_feedback(tmp_path: Path) -> None:
    project_dir = tmp_path / "PRJ-PLAN"
    project_dir.mkdir()
    (project_dir / "task_plan.json").write_text(
        json.dumps(
            {
                "title": "demo",
                "plan": {
                    "phase": "questions",
                    "questions": [{"id": "q1", "prompt": "goal", "kind": "text"}],
                    "answers": {"q1": "new goal"},
                    "feedback": "revise this",
                },
                "experiments": [],
            }
        ),
        encoding="utf-8",
    )

    result = await SetPlanTool(str(project_dir)).execute(
        phase="draft",
        draft={
            "summary": "new draft",
            "experiments": [{"title": "Exp A", "hypothesis": "H", "method": "M"}],
        },
    )

    assert "Saved plan draft" in result
    plan = _read_plan(project_dir)
    assert plan["phase"] == "draft"
    assert plan["answers"] == {"q1": "new goal"}
    assert plan["draft"]["summary"] == "new draft"
    assert "feedback" not in plan
    assert isinstance(plan["updated_at"], str)
