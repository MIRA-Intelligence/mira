from __future__ import annotations

import json
from pathlib import Path

import pytest

from mira_engine.workflows import JsonWorkflowRepository, WorkflowError, WorkflowService


@pytest.fixture
def service(tmp_path: Path) -> WorkflowService:
    project_dir = tmp_path / "PRJ-test"
    meta_dir = project_dir / ".mira"
    meta_dir.mkdir(parents=True)
    (meta_dir / "project.json").write_text(
        json.dumps({"schema_version": 1, "id": "PRJ-test", "display_name": "Test"}),
        encoding="utf-8",
    )
    return WorkflowService(JsonWorkflowRepository(project_dir, "PRJ-test"))


def test_create_direct_entry_run_persists_per_run_snapshot_and_project_index(
    service: WorkflowService,
) -> None:
    receipt = service.create_run(
        command_id="cmd-create",
        module_type="delivery",
        module_data={"intent": "polish uploaded manuscript"},
    )

    workflow, runs = service.snapshot()
    assert receipt.disposition == "applied"
    assert len(runs) == 1
    assert runs[0].module_type == "delivery"
    assert runs[0].status == "created"
    assert runs[0].module_data["intent"] == "polish uploaded manuscript"
    assert workflow.selected_run_id == runs[0].run_id
    assert workflow.module_index[0].run_id == runs[0].run_id
    assert service.repository.module_path(runs[0].run_id).is_file()


def test_repeated_module_types_are_independent_runs(service: WorkflowService) -> None:
    first = service.create_run(command_id="cmd-1", module_type="planning")
    second = service.create_run(command_id="cmd-2", module_type="planning")

    runs = service.repository.list_runs()
    assert first.run_id != second.run_id
    assert [run.label for run in runs] == ["Planning 1", "Planning 2"]


def test_command_is_idempotent_and_revisions_are_checked(service: WorkflowService) -> None:
    created = service.create_run(command_id="cmd-create", module_type="experiment")
    applied = service.command_run(
        created.run_id or "",
        command_id="cmd-enqueue",
        command_type="enqueue",
        expected_revision=1,
    )
    duplicate = service.command_run(
        created.run_id or "",
        command_id="cmd-enqueue",
        command_type="enqueue",
        expected_revision=1,
    )

    assert applied.disposition == "accepted"
    assert duplicate.disposition == "duplicate"
    assert duplicate.project_sequence == applied.project_sequence
    assert len(service.list_events()) == 2

    with pytest.raises(WorkflowError, match="revision") as exc_info:
        service.command_run(
            created.run_id or "",
            command_id="cmd-pause",
            command_type="pause",
            expected_revision=1,
        )
    assert exc_info.value.code == "revision_conflict"


def test_pause_resume_and_cancel_follow_legal_transitions(service: WorkflowService) -> None:
    created = service.create_run(
        command_id="cmd-create", module_type="initiation", enqueue=True
    )
    run_id = created.run_id or ""

    paused = service.command_run(run_id, command_id="cmd-pause", command_type="pause")
    resumed = service.command_run(run_id, command_id="cmd-resume", command_type="resume")
    cancelled = service.command_run(run_id, command_id="cmd-cancel", command_type="cancel")

    assert paused.event and paused.event.payload["to"] == "paused"
    assert resumed.event and resumed.event.payload["to"] == "queued"
    assert cancelled.event and cancelled.event.payload["to"] == "cancelled"
    with pytest.raises(WorkflowError) as exc_info:
        service.command_run(run_id, command_id="cmd-again", command_type="enqueue")
    assert exc_info.value.code == "invalid_transition"


def test_events_replay_from_project_cursor(service: WorkflowService) -> None:
    service.create_run(command_id="cmd-1", module_type="initiation")
    service.create_run(command_id="cmd-2", module_type="delivery")

    replay = service.list_events(after=1)
    assert [event.project_sequence for event in replay] == [2]
    assert replay[0].event_type == "run.created"


def test_engine_claim_and_manual_completion_close_a_conversation_run(
    service: WorkflowService,
) -> None:
    created = service.create_run(
        command_id="cmd-create", module_type="planning", enqueue=True
    )
    run_id = created.run_id or ""
    claimed = service.claim_run(run_id, lease_owner="ui:test")
    assert claimed and claimed.event and claimed.event.event_type == "run.started"

    running = service.repository.load_run(run_id)
    assert running.status == "running"
    assert running.attempt.number == 1
    completed = service.command_run(
        run_id,
        command_id="cmd-complete",
        command_type="complete",
        expected_revision=running.revision,
        payload={"summary": "Plan approved in discussion", "outputs": []},
    )
    assert completed.event and completed.event.event_type == "run.completed"
    final = service.repository.load_run(run_id)
    assert final.status == "completed"
    assert final.module_data["completion_summary"] == "Plan approved in discussion"


def test_required_module_confirmation_blocks_completion_until_approved(
    service: WorkflowService,
) -> None:
    created = service.create_run(
        command_id="cmd-create-confirmed",
        module_type="experiment",
        module_data={
            "intent": "run a controlled experiment",
            "confirmation_policy": {"mode": "required"},
        },
        enqueue=True,
    )
    run_id = created.run_id or ""
    service.claim_run(run_id, lease_owner="executor:test")
    running = service.repository.load_run(run_id)

    with pytest.raises(WorkflowError) as exc_info:
        service.command_run(
            run_id,
            command_id="cmd-complete-before-confirmation",
            command_type="complete",
            expected_revision=running.revision,
            payload={"outputs": []},
        )
    assert exc_info.value.code == "confirmation_required"

    service.request_interaction(
        run_id,
        command_id="signal-confirmation",
        attempt_id=running.attempt.current_attempt_id,
        checkpoint_state={"phase": "brief_ready"},
        interaction={
            "interaction_id": "experiment-confirmation",
            "interaction_type": "approve_experiment_plan",
            "title": "Approve experiment brief",
            "prompt": "Approve this experiment brief?",
            "response_schema": {
                "type": "object",
                "required": ["selection"],
                "properties": {
                    "selection": {"type": "string"},
                    "notes": {"type": "string"},
                },
            },
            "choices": [
                {"id": "approve", "label": "Approve", "value": "approve"},
                {"id": "revise", "label": "Revise", "value": "revise"},
            ],
            "allow_freeform": True,
        },
    )
    service.command_run(
        run_id,
        command_id="cmd-approve-brief",
        command_type="submit_interaction",
        payload={
            "interaction_id": "experiment-confirmation",
            "response": {"selection": "approve"},
        },
    )
    queued = service.repository.load_run(run_id)
    assert queued.module_data["interaction_history"][0]["response"] == {"selection": "approve"}

    service.claim_run(run_id, lease_owner="executor:test-2")
    resumed = service.repository.load_run(run_id)
    service.command_run(
        run_id,
        command_id="cmd-complete-after-confirmation",
        command_type="complete",
        expected_revision=resumed.revision,
        payload={"outputs": [], "summary": "Experiment finished"},
    )
    assert service.repository.load_run(run_id).status == "completed"


def test_artifact_import_is_immutable_and_can_be_committed_as_run_output(
    service: WorkflowService,
) -> None:
    source = service.repository.project_dir / "report.md"
    source.write_text("# Result\n", encoding="utf-8")
    artifact, event = service.import_artifact(
        command_id="cmd-import",
        source_path="report.md",
        artifact_type="research_report",
        media_type="text/markdown",
    )
    assert event.event_type == "artifact.imported"
    assert artifact.sha256
    assert service.repository.artifact_content_path(artifact).read_text() == "# Result\n"

    created = service.create_run(
        command_id="cmd-create", module_type="delivery", enqueue=True
    )
    run_id = created.run_id or ""
    service.claim_run(run_id, lease_owner="ui:test")
    running = service.repository.load_run(run_id)
    service.command_run(
        run_id,
        command_id="cmd-complete",
        command_type="complete",
        expected_revision=running.revision,
        payload={"outputs": [artifact.artifact_id]},
    )
    assert service.repository.load_run(run_id).outputs == [artifact.artifact_id]


def test_artifact_import_rejects_path_outside_project(service: WorkflowService) -> None:
    with pytest.raises(WorkflowError) as exc_info:
        service.import_artifact(
            command_id="cmd-import",
            source_path="../outside.txt",
            artifact_type="file",
        )
    assert exc_info.value.code == "invalid_source_path"


def test_recovery_repairs_project_watermark_and_missing_event_from_run_snapshot(
    service: WorkflowService,
) -> None:
    created = service.create_run(command_id="cmd-create", module_type="initiation")
    project_path = service.repository.project_path
    project = json.loads(project_path.read_text(encoding="utf-8"))
    project["workflow"]["last_project_sequence"] = 0
    project["workflow"]["revision"] = 0
    project_path.write_text(json.dumps(project), encoding="utf-8")

    recovered = service.ensure_project()
    assert recovered.last_project_sequence == 1
    assert recovered.revision == 1

    # Simulate snapshot durable but its event append lost at crash time.
    service.repository.events_path.write_text("", encoding="utf-8")
    repaired = service.ensure_project()
    events = service.list_events()
    assert repaired.last_project_sequence == 1
    assert len(events) == 1
    assert events[0].event_id == created.event.event_id
    assert events[0].payload["recovered_from_snapshot"] is True


def test_replay_ignores_malformed_trailing_event_line(service: WorkflowService) -> None:
    service.create_run(command_id="cmd-create", module_type="planning")
    with service.repository.events_path.open("a", encoding="utf-8") as handle:
        handle.write('{"event_id":')
    assert [event.project_sequence for event in service.list_events()] == [1]


def test_interaction_checkpoints_then_resumes_in_a_new_attempt(
    service: WorkflowService,
) -> None:
    created = service.create_run(
        command_id="cmd-create-interaction", module_type="initiation", enqueue=True
    )
    run_id = created.run_id or ""
    service.claim_run(run_id, lease_owner="executor:test")
    running = service.repository.load_run(run_id)
    first_attempt_id = running.attempt.current_attempt_id

    requested = service.request_interaction(
        run_id,
        command_id="signal-interaction",
        attempt_id=first_attempt_id,
        checkpoint_state={"phase": "directions_ready"},
        conversation_cursor=12,
        interaction={
            "interaction_id": "interaction-direction",
            "interaction_type": "choose_direction",
            "title": "Choose a direction",
            "prompt": "Which direction should be developed?",
            "response_schema": {
                "type": "object",
                "required": ["direction_id"],
                "properties": {"direction_id": {"type": "string"}},
                "additionalProperties": False,
            },
            "choices": [{"id": "direction-a", "label": "Direction A"}],
            "allow_freeform": False,
        },
    )
    waiting = service.repository.load_run(run_id)
    assert requested.event and requested.event.event_type == "interaction.requested"
    assert waiting.status == "waiting_input"
    assert waiting.checkpoint_ref
    checkpoint = service.repository.load_checkpoint(run_id, waiting.checkpoint_ref)
    assert checkpoint.state == {"phase": "directions_ready"}
    assert checkpoint.conversation_cursor == 12
    assert waiting.attempts[0].outcome == "waiting_input"
    assert waiting.attempts[0].ended_at

    with pytest.raises(WorkflowError) as exc_info:
        service.command_run(
            run_id,
            command_id="cmd-invalid-answer",
            command_type="submit_interaction",
            payload={"interaction_id": "interaction-direction", "response": {}},
        )
    assert exc_info.value.code == "interaction_response_invalid"

    with pytest.raises(WorkflowError) as exc_info:
        service.command_run(
            run_id,
            command_id="cmd-wrong-answer-type",
            command_type="submit_interaction",
            payload={
                "interaction_id": "interaction-direction",
                "response": {"direction_id": 7},
            },
        )
    assert exc_info.value.code == "interaction_response_invalid"

    service.command_run(
        run_id,
        command_id="cmd-answer",
        command_type="submit_interaction",
        payload={
            "interaction_id": "interaction-direction",
            "response": {"direction_id": "direction-a"},
        },
    )
    service.claim_run(run_id, lease_owner="executor:test-2")
    resumed = service.repository.load_run(run_id)
    assert resumed.status == "running"
    assert resumed.attempt.number == 2
    assert resumed.attempt.current_attempt_id != first_attempt_id
    assert resumed.attempts[-1].resumed_from_checkpoint_id == checkpoint.checkpoint_id


@pytest.mark.parametrize("interruption", ["pause", "fail"])
def test_unanswered_interaction_survives_control_and_blocks_completion(
    service: WorkflowService, interruption: str,
) -> None:
    run_id = service.create_run(command_id="create-question", module_type="planning", enqueue=True).run_id or ""
    service.claim_run(run_id, lease_owner="test")
    service.request_interaction(run_id, command_id="ask", interaction={
        "interaction_id": "question-1", "interaction_type": "clarify_requirements",
        "title": "Output?", "prompt": "What should we produce?",
        "choices": [], "allow_freeform": True,
        "response_schema": {"type": "object", "required": ["selection"]},
    })
    with pytest.raises(WorkflowError, match="answer the pending interaction"):
        service.command_run(run_id, command_id="premature-complete", command_type="complete")
    service.command_run(run_id, command_id="interrupt", command_type=interruption)
    service.command_run(run_id, command_id="continue", command_type="resume" if interruption == "pause" else "retry")
    assert service.repository.load_run(run_id).status == "waiting_input"
    with pytest.raises(WorkflowError):
        service.claim_run(run_id, lease_owner="bypass")
    service.command_run(run_id, command_id="answer", command_type="submit_interaction", payload={
        "interaction_id": "question-1", "response": {"selection": "Please suggest a focus"},
    })
    service.claim_run(run_id, lease_owner="next")
    service.request_interaction(run_id, command_id="ask-again", interaction={
        "interaction_id": "question-2", "interaction_type": "resolve_conflict",
        "title": "Resource conflict", "prompt": "Choose an alternative",
        "response_schema": {"type": "object", "required": ["selection"]},
    })
    with pytest.raises(WorkflowError) as exc:
        service.command_run(run_id, command_id="stale-answer", command_type="submit_interaction", payload={
            "interaction_id": "question-1", "response": {"selection": "Old answer"},
        })
    assert exc.value.code == "interaction_not_found"
    assert service.repository.load_run(run_id).module_data["interaction_history"][0]["prompt"] == "What should we produce?"


@pytest.mark.parametrize(("kind", "freeform", "response"), [
    ("clarify_requirements", True, {"selection": "   "}),
    ("clarify_requirements", False, {"selection": "not-offered"}),
    ("approve_plan", True, {"selection": "looks interesting"}),
    ("approve_plan", True, {"selection": "revise", "notes": "  "}),
])
def test_interaction_rejects_empty_or_ambiguous_answers(
    service: WorkflowService, kind: str, freeform: bool, response: dict,
) -> None:
    run_id = service.create_run(command_id="create", module_type="planning", enqueue=True).run_id or ""
    service.claim_run(run_id, lease_owner="test")
    service.request_interaction(run_id, command_id="ask", interaction={
        "interaction_id": "question", "interaction_type": kind,
        "title": "Confirm", "prompt": "Confirm a choice", "allow_freeform": freeform,
        "choices": [{"id": "approve", "label": "Approve"}, {"id": "revise", "label": "Revise"}],
        "response_schema": {"type": "object", "required": ["selection"]},
    })
    with pytest.raises(WorkflowError) as exc:
        service.command_run(run_id, command_id="invalid", command_type="submit_interaction", payload={
            "interaction_id": "question", "response": response,
        })
    assert exc.value.code == "interaction_response_invalid"
    assert service.repository.load_run(run_id).status == "waiting_input"


def test_pending_pause_is_acknowledged_at_a_durable_checkpoint(
    service: WorkflowService,
) -> None:
    created = service.create_run(
        command_id="cmd-create-pause", module_type="experiment", enqueue=True
    )
    run_id = created.run_id or ""
    service.claim_run(run_id, lease_owner="executor:test")
    running = service.repository.load_run(run_id)
    service.command_run(run_id, command_id="cmd-pause-running", command_type="pause")
    service.acknowledge_control(
        run_id,
        command_id="signal-pause-ack",
        control="pause",
        attempt_id=running.attempt.current_attempt_id,
        checkpoint_state={"completed_steps": ["prepare"]},
    )
    paused = service.repository.load_run(run_id)
    assert paused.status == "paused"
    assert paused.pending_control is None
    assert paused.attempt.lease_owner is None
    assert paused.checkpoint_ref


def test_new_executor_instance_pauses_before_recovering_an_uncertain_running_attempt(
    service: WorkflowService,
) -> None:
    created = service.create_run(
        command_id="cmd-create-recovery", module_type="delivery", enqueue=True
    )
    run_id = created.run_id or ""
    service.claim_run(run_id, lease_owner="ui:old-instance:user")
    before = service.repository.load_run(run_id)
    old_attempt_id = before.attempt.current_attempt_id

    recovered = service.claim_run(run_id, lease_owner="ui:new-instance:user")
    after = service.repository.load_run(run_id)
    assert recovered and recovered.event and recovered.event.event_type == "run.recovery_required"
    assert after.status == "paused"
    assert after.attempt.number == 1
    assert after.attempt.current_attempt_id == old_attempt_id
    assert after.attempts[0].outcome == "paused"
    checkpoint = service.repository.load_checkpoint(run_id, after.checkpoint_ref or "")
    assert checkpoint.state["recovery_required"] is True

    service.command_run(run_id, command_id="cmd-resume-recovery", command_type="resume")
    service.claim_run(run_id, lease_owner="ui:new-instance:user")
    resumed = service.repository.load_run(run_id)
    assert resumed.attempt.number == 2
    assert resumed.attempt.current_attempt_id != old_attempt_id
    assert resumed.attempts[-1].resumed_from_checkpoint_id == after.checkpoint_ref


def test_auto_handoff_creates_a_queued_downstream_run_atomically(
    service: WorkflowService,
) -> None:
    service.ensure_project()
    workflow = service.repository.load_workflow()
    service.command_project(
        command_id="cmd-policy",
        command_type="set_handoff_policy",
        expected_revision=workflow.revision,
        payload={"handoff_policy": {"mode": "auto", "rules": []}},
    )
    created = service.create_run(
        command_id="cmd-source", module_type="planning", enqueue=True
    )
    run_id = created.run_id or ""
    service.claim_run(run_id, lease_owner="executor:test")
    running = service.repository.load_run(run_id)
    receipt = service.command_run(
        run_id,
        command_id="cmd-complete-auto",
        command_type="complete",
        expected_revision=running.revision,
        payload={
            "outputs": [],
            "suggested_handoffs": [{"target_module_type": "experiment"}],
        },
    )
    runs = service.repository.list_runs()
    assert receipt.event and receipt.event.event_type == "run.completed"
    assert len(runs) == 2
    child = next(run for run in runs if run.run_id != run_id)
    assert child.module_type == "experiment"
    assert child.status == "queued"
    assert child.module_data["parent_run_id"] == run_id
    assert child.module_data["confirmation_policy"] == {"mode": "required"}
    assert child.module_data["handoff_not_before"]
    with pytest.raises(WorkflowError) as exc_info:
        service.claim_run(child.run_id, lease_owner="executor:too-early")
    assert exc_info.value.code == "handoff_grace_active"
    assert [event.event_type for event in service.list_events()][-2:] == [
        "run.completed",
        "run.auto_handoff_created",
    ]


def _approval_gate(service: WorkflowService) -> str:
    created = service.create_run(
        command_id="cmd-create-gated",
        module_type="initiation",
        module_data={"confirmation_policy": {"mode": "required"}},
        enqueue=True,
    )
    run_id = created.run_id or ""
    service.claim_run(run_id, lease_owner="ui:test")
    service.request_interaction(
        run_id,
        command_id="signal-approval",
        interaction={
            "interaction_id": "interaction-approval",
            "interaction_type": "approve_research_scope",
            "title": "Approve the research brief",
            "prompt": "Brief: review lung cancer staging literature.",
            "response_schema": {"type": "object", "required": ["selection"]},
            "choices": [{"value": "approve"}, {"value": "revise"}],
            "allow_freeform": True,
        },
    )
    return run_id


def test_chat_message_answers_an_approval_gate_as_a_revision(
    service: WorkflowService,
) -> None:
    run_id = _approval_gate(service)

    applied = service.prepare_for_chat(run_id, text="Also cover small-cell carcinoma")

    run = service.repository.load_run(run_id)
    assert applied == ["answer_in_chat"]
    assert run.status == "queued"
    assert run.module_data["interaction"]["response"] == {
        "selection": "revise",
        "notes": "Also cover small-cell carcinoma",
        "source": "chat",
    }
    service.claim_run(run_id, lease_owner="ui:test")
    with pytest.raises(WorkflowError) as exc_info:
        service.command_run(
            run_id,
            command_id="cmd-complete-unapproved",
            command_type="complete",
            payload={"outputs": []},
        )
    # Typing in the chat must never count as approving the brief.
    assert exc_info.value.code == "confirmation_required"


def test_chat_message_answers_a_choice_interaction(service: WorkflowService) -> None:
    created = service.create_run(command_id="cmd-create", module_type="initiation", enqueue=True)
    run_id = created.run_id or ""
    service.claim_run(run_id, lease_owner="ui:test")
    service.request_interaction(
        run_id,
        command_id="signal-direction",
        interaction={
            "interaction_id": "interaction-direction",
            "interaction_type": "choose_direction",
            "title": "Choose a direction",
            "prompt": "Which direction?",
            "response_schema": {"type": "object", "required": ["direction_id"]},
            "choices": [{"id": "a", "label": "A"}],
            "allow_freeform": False,
        },
    )

    assert service.prepare_for_chat(run_id, text="What is the difference between them?") == [
        "answer_in_chat"
    ]
    run = service.repository.load_run(run_id)
    assert run.status == "queued"
    assert run.module_data["interaction"]["response"] == {
        "chat_reply": "What is the difference between them?",
        "source": "chat",
    }


def test_chat_message_on_a_completed_run_keeps_it_completed(service: WorkflowService) -> None:
    (service.repository.project_dir / "report.md").write_text("# Result\n", encoding="utf-8")
    artifact, _ = service.import_artifact(
        command_id="cmd-import",
        source_path="report.md",
        artifact_type="research_report",
        media_type="text/markdown",
    )
    created = service.create_run(command_id="cmd-create", module_type="initiation", enqueue=True)
    run_id = created.run_id or ""
    service.claim_run(run_id, lease_owner="ui:test")
    service.command_run(
        run_id,
        command_id="cmd-complete",
        command_type="complete",
        payload={"outputs": [artifact.artifact_id]},
    )
    selection = {"run_id": run_id, "item_id": "direction-2", "artifact_ids": [artifact.artifact_id]}

    applied = service.prepare_for_chat(run_id, text="Polish direction 2", selection=selection)

    run = service.repository.load_run(run_id)
    assert applied == ["request_revision"]
    # The next-step actions depend on `completed`; an unrelated chat must not hide them.
    assert run.status == "completed"
    assert run.outputs == [artifact.artifact_id]
    assert run.last_transition and run.last_transition.event_type == "run.revision_requested"
    request = run.module_data["revision_requests"][-1]
    assert request["text"] == "Polish direction 2"
    assert request["selection"] == selection
    assert request["previous_outputs"] == [artifact.artifact_id]
    with pytest.raises(WorkflowError) as exc_info:
        service.claim_run(run_id, lease_owner="ui:test")
    assert exc_info.value.code == "run_not_queued"


def test_revising_outputs_keeps_the_run_completed_and_skips_handoffs(
    service: WorkflowService,
) -> None:
    service.ensure_project()
    workflow = service.repository.load_workflow()
    service.command_project(
        command_id="cmd-policy",
        command_type="set_handoff_policy",
        expected_revision=workflow.revision,
        payload={"handoff_policy": {"mode": "auto", "rules": []}},
    )
    created = service.create_run(command_id="cmd-source", module_type="planning", enqueue=True)
    run_id = created.run_id or ""
    service.claim_run(run_id, lease_owner="ui:test")
    service.command_run(
        run_id,
        command_id="cmd-complete",
        command_type="complete",
        payload={"outputs": [], "suggested_handoffs": [{"target_module_type": "experiment"}]},
    )
    assert len(service.repository.list_runs()) == 2
    (service.repository.project_dir / "plan.md").write_text("# Plan v2\n", encoding="utf-8")
    revised, _ = service.import_artifact(command_id="cmd-import", source_path="plan.md", artifact_type="plan")

    service.prepare_for_chat(run_id, text="Tighten the risks section")
    service.command_run(
        run_id,
        command_id="cmd-revise",
        command_type="revise_outputs",
        payload={"outputs": [revised.artifact_id], "summary": "Risks tightened"},
    )

    run = service.repository.load_run(run_id)
    assert run.status == "completed"
    assert run.outputs == [revised.artifact_id]
    assert run.module_data["completion_summary"] == "Risks tightened"
    assert len(service.repository.list_runs()) == 2
    with pytest.raises(WorkflowError) as exc_info:
        service.command_run(
            run_id, command_id="cmd-bad", command_type="revise_outputs", payload={"outputs": ["missing"]}
        )
    assert exc_info.value.code == "artifact_not_found"


@pytest.mark.parametrize(("interruption", "expected"), [("pause", ["resume"]), ("fail", ["retry"])])
def test_chat_message_continues_a_paused_or_failed_run(
    service: WorkflowService, interruption: str, expected: list[str]
) -> None:
    created = service.create_run(command_id="cmd-create", module_type="planning", enqueue=True)
    run_id = created.run_id or ""
    service.command_run(run_id, command_id="cmd-interrupt", command_type=interruption)

    assert service.prepare_for_chat(run_id, text="Please continue") == expected
    assert service.repository.load_run(run_id).status == "queued"


def test_chat_message_leaves_queued_and_cancelled_runs_alone(service: WorkflowService) -> None:
    created = service.create_run(command_id="cmd-create", module_type="planning", enqueue=True)
    run_id = created.run_id or ""
    assert service.prepare_for_chat(run_id, text="hi") == []
    service.command_run(run_id, command_id="cmd-cancel", command_type="cancel")
    assert service.prepare_for_chat(run_id, text="hi") == []
    assert service.repository.load_run(run_id).status == "cancelled"
