from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from mira_engine.modules.tools import WorkflowInteractionTool
from mira_engine.workflows import JsonWorkflowRepository, WorkflowError, WorkflowService
from mira_engine.workflows.auto_decide import (
    AutoDecider,
    pending_auto_decision,
    seconds_until,
)
from mira_engine.workflows.service import clear_default_event_listener, set_default_event_listener

CHOICES = [
    {"id": "small", "label": "Small pilot", "recommended": True},
    {"id": "full", "label": "Full cohort"},
]


def _service(tmp_path: Path, project_id: str = "PRJ-auto") -> WorkflowService:
    project_dir = tmp_path / project_id
    (project_dir / ".mira").mkdir(parents=True)
    (project_dir / ".mira" / "project.json").write_text(
        json.dumps({"schema_version": 1, "id": project_id}), encoding="utf-8"
    )
    return WorkflowService(JsonWorkflowRepository(project_dir, project_id))


def _running(service: WorkflowService, module_type: str = "experiment", **module_data: Any) -> str:
    created = service.create_run(
        command_id=f"create-{module_type}-{len(service.repository.list_runs())}",
        module_type=module_type,
        module_data=module_data or None,
        enqueue=True,
    )
    run_id = created.run_id or ""
    service.claim_run(run_id, lease_owner="agent:test")
    return run_id


async def _ask(
    service: WorkflowService,
    run_id: str,
    *,
    interaction_type: str = "clarify_requirements",
    choices: list[dict[str, Any]] | None = None,
) -> str:
    tool = WorkflowInteractionTool()
    tool.set_project_context(service.repository.project_id, str(service.repository.project_dir))
    tool.set_workflow_run_context(run_id)
    return await tool.execute(
        interaction_type=interaction_type,
        title="Which cohort?",
        prompt="Pick the cohort for the next experiment.",
        choices=CHOICES if choices is None else choices,
        allow_freeform=True,
    )


def _expire(service: WorkflowService, run_id: str) -> None:
    run = service.repository.load_run(run_id)
    past = datetime.now(UTC) - timedelta(seconds=1)
    run.module_data["interaction"]["auto_decide"]["deadline_at"] = past.isoformat().replace("+00:00", "Z")
    service.repository.save_run(run)


async def test_auto_mode_form_carries_the_recommendation_and_a_deadline(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run_id = _running(service)

    await _ask(service, run_id)

    interaction = service.repository.load_run(run_id).module_data["interaction"]
    assert [choice.get("recommended") for choice in interaction["choices"]] == [True, None]
    auto = interaction["auto_decide"]
    assert auto["selection"] == "small" and auto["label"] == "Small pilot" and auto["seconds"] == 10
    assert 8 < seconds_until(auto["deadline_at"]) <= 10


async def test_manual_mode_and_other_modules_never_time_out(tmp_path: Path) -> None:
    service = _service(tmp_path)
    manual = _running(service, experiment_execution_mode="manual")
    planning = _running(service, module_type="planning")

    await _ask(service, manual)
    await _ask(service, planning)

    for run_id in (manual, planning):
        interaction = service.repository.load_run(run_id).module_data["interaction"]
        assert "auto_decide" not in interaction
        assert interaction["choices"][0]["recommended"] is True  # still shown as recommended


@pytest.mark.parametrize(
    ("choices", "error"),
    [
        ([{"id": "a", "label": "A"}, {"id": "b", "label": "B"}], "mark exactly one choice recommended"),
        (
            [{"id": "a", "label": "A", "recommended": True}, {"id": "b", "label": "B", "recommended": True}],
            "at most one",
        ),
        ([], "auto mode"),
    ],
)
async def test_forms_need_exactly_one_recommendation(
    tmp_path: Path, choices: list[dict[str, Any]], error: str
) -> None:
    service = _service(tmp_path)
    run_id = _running(service)

    result = await _ask(service, run_id, choices=choices)

    assert result.startswith("Error:") and error in result
    assert service.repository.load_run(run_id).status == "running"


async def test_confirmation_gates_recommend_approve(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run_id = _running(service)

    await _ask(service, run_id, interaction_type="approve_experiment_plan", choices=[
        {"id": "approve", "label": "Approve", "value": "approve"},
        {"id": "revise", "label": "Revise", "value": "revise", "recommended": True},
    ])

    interaction = service.repository.load_run(run_id).module_data["interaction"]
    assert [choice.get("recommended") for choice in interaction["choices"]] == [True, None]
    assert interaction["auto_decide"]["selection"] == "approve"


async def test_cancel_stops_the_countdown_and_resume_restarts_it(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run_id = _running(service)
    await _ask(service, run_id)
    interaction_id = service.repository.load_run(run_id).module_data["interaction"]["interaction_id"]

    service.command_run(run_id, command_id="pause", command_type="pause")
    _expire(service, run_id)
    service.command_run(run_id, command_id="resume", command_type="resume")
    resumed = service.repository.load_run(run_id)
    assert seconds_until(resumed.module_data["interaction"]["auto_decide"]["deadline_at"]) > 8

    receipt = service.command_run(
        run_id, command_id="hold", command_type="cancel_auto_decision",
        payload={"interaction_id": interaction_id},
    )
    run = service.repository.load_run(run_id)
    assert receipt.event and receipt.event.event_type == "interaction.auto_decide_cancelled"
    assert run.status == "waiting_input"
    assert run.module_data["interaction"]["auto_decide"]["cancelled_at"]
    assert pending_auto_decision(run) is None


class _Turns:
    def __init__(self) -> None:
        self.started: list[tuple[str, str, dict[str, Any]]] = []

    async def __call__(self, project_id: str, run_id: str, decision: dict[str, Any]) -> None:
        self.started.append((project_id, run_id, decision))


async def test_fire_answers_with_the_recommendation_and_starts_the_next_turn(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run_id = _running(service)
    await _ask(service, run_id)
    interaction_id = service.repository.load_run(run_id).module_data["interaction"]["interaction_id"]
    turns = _Turns()
    decider = AutoDecider(lambda _pid: service, turns)

    assert await decider.fire("PRJ-auto", run_id, interaction_id) is False  # not due yet
    _expire(service, run_id)
    assert await decider.fire("PRJ-auto", run_id, interaction_id) is True
    decider.close()

    run = service.repository.load_run(run_id)
    assert run.status == "queued"
    assert run.module_data["interaction"]["response"] == {"selection": "small"}
    assert run.module_data["interaction"]["resolved_by"] == "auto"
    assert run.module_data["interaction_history"][-1]["resolved_by"] == "auto"
    assert [(pid, rid, d["selection"]) for pid, rid, d in turns.started] == [("PRJ-auto", run_id, "small")]


async def test_a_user_answer_wins_over_the_timer(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run_id = _running(service)
    await _ask(service, run_id)
    interaction_id = service.repository.load_run(run_id).module_data["interaction"]["interaction_id"]
    _expire(service, run_id)
    service.command_run(run_id, command_id="user", command_type="submit_interaction", payload={
        "interaction_id": interaction_id, "response": {"selection": "full"},
    })
    turns = _Turns()

    assert await AutoDecider(lambda _pid: service, turns).fire("PRJ-auto", run_id, interaction_id) is False

    run = service.repository.load_run(run_id)
    assert run.module_data["interaction"]["response"] == {"selection": "full"}
    assert "resolved_by" not in run.module_data["interaction"]
    assert turns.started == []
    with pytest.raises(WorkflowError):  # and the timer could not answer twice
        service.command_run(run_id, command_id="auto-again", command_type="submit_interaction", payload={
            "interaction_id": interaction_id, "response": {"selection": "small"}, "auto_decided": True,
        })


async def test_waits_for_the_asking_turn_to_end(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run_id = _running(service)
    await _ask(service, run_id)
    interaction_id = service.repository.load_run(run_id).module_data["interaction"]["interaction_id"]
    _expire(service, run_id)
    busy = {"value": True}
    turns = _Turns()
    decider = AutoDecider(lambda _pid: service, turns, is_busy=lambda _pid, _rid: busy["value"])

    assert await decider.fire("PRJ-auto", run_id, interaction_id) is False
    assert service.repository.load_run(run_id).status == "waiting_input"
    busy["value"] = False
    assert await decider.fire("PRJ-auto", run_id, interaction_id) is True
    decider.close()


async def test_events_and_restart_arm_timers_that_fire_without_a_ui(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run_id = _running(service)
    turns = _Turns()
    decider = AutoDecider(lambda _pid: service, turns)
    # The agent tool's own WorkflowService reports through the process-wide listener.
    set_default_event_listener(decider.observe)
    try:
        await _ask(service, run_id)  # interaction.requested arms a 10s timer
        await asyncio.sleep(0)
    finally:
        clear_default_event_listener(decider.observe)
    assert ("PRJ-auto", run_id) in decider._timers
    decider.close()

    _expire(service, run_id)
    restarted = AutoDecider(lambda _pid: service, turns)
    restarted.rearm(["PRJ-auto"])  # engine restart: the overdue form fires at once
    for _ in range(20):
        await asyncio.sleep(0.01)
        if turns.started:
            break
    restarted.close()

    assert [rid for _pid, rid, _d in turns.started] == [run_id]
    assert service.repository.load_run(run_id).status == "queued"
