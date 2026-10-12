"""Workflow application service and lifecycle authority."""

from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .confirmations import confirmation_approved, confirmation_required
from .experiments import ExperimentRecord, experiment_boundary_decision, manual_experiments
from .input_paths import normalize_input_paths
from .journals import normalize_journal_selection
from .models import (
    MODULE_TYPES,
    Actor,
    AppliedCommand,
    Artifact,
    ArtifactInput,
    AttemptRecord,
    Checkpoint,
    CommandReceipt,
    HandoffPolicy,
    InitiationResearchResult,
    InteractionRequest,
    LiteratureSearchRecord,
    ModuleRun,
    ProjectWorkflow,
    Transition,
    WorkflowEvent,
    new_id,
    utc_now,
)
from .repository import JsonWorkflowRepository


class WorkflowError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 400, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


EventListener = Callable[[WorkflowEvent], None]


class WorkflowService:
    @staticmethod
    def _validate_input_paths(module_type: str, data: dict[str, Any]) -> dict[str, Any]:
        if module_type != "experiment":
            raise WorkflowError("invalid_input_paths", "external read grants belong only to experiment Runs", status=422)
        try:
            return {**data, "external_read_paths": normalize_input_paths(data["external_read_paths"])}
        except (OSError, ValueError, RuntimeError) as exc:
            raise WorkflowError("invalid_input_paths", str(exc), status=422) from exc

    def __init__(self, repository: JsonWorkflowRepository, *, on_event: EventListener | None = None):
        self.repository = repository
        self.on_event = on_event

    def ensure_project(self) -> ProjectWorkflow:
        return self.repository.ensure_workflow()

    def snapshot(self) -> tuple[ProjectWorkflow, list[ModuleRun]]:
        workflow = self.repository.ensure_workflow()
        return workflow, self.repository.list_runs()

    def list_events(self, *, after: int = 0, limit: int = 100) -> list[WorkflowEvent]:
        return self.repository.read_events(after=max(after, 0), limit=min(max(limit, 1), 500))

    def import_artifact(
        self,
        *,
        command_id: str,
        source_path: str,
        artifact_type: str,
        label: str | None = None,
        media_type: str = "application/octet-stream",
        derived_from: list[str] | None = None,
    ) -> tuple[Artifact, WorkflowEvent]:
        if not command_id:
            raise WorkflowError("command_id_required", "command_id is required", status=422)
        source = (self.repository.project_dir / source_path).resolve()
        try:
            source.relative_to(self.repository.project_dir)
        except ValueError as exc:
            raise WorkflowError("invalid_source_path", "source_path escapes Project", status=422) from exc
        if not source.is_file():
            raise WorkflowError("source_not_found", f"file not found: {source_path}", status=404)
        content = source.read_bytes()
        with self.repository.transaction():
            workflow = self._load_active_workflow()
            duplicate = self.repository.find_command_event(command_id)
            if duplicate:
                artifact_id = duplicate.payload.get("artifact_id")
                if not isinstance(artifact_id, str):
                    raise WorkflowError("command_id_conflict", "command was not an artifact import", status=409)
                return self.repository.load_artifact(artifact_id), duplicate
            for source_id in derived_from or []:
                try:
                    self.repository.load_artifact(source_id)
                except FileNotFoundError as exc:
                    raise WorkflowError("artifact_not_found", str(exc), status=404) from exc
            artifact = Artifact(
                artifact_id=new_id(),
                artifact_type=artifact_type.strip() or "file",
                label=(label or source.name).strip() or source.name,
                media_type=media_type,
                byte_size=0,
                sha256="",
                payload_ref="",
                created_by=Actor(kind="external_import"),
                derived_from=derived_from or [],
            )
            workflow.revision += 1
            workflow.last_project_sequence += 1
            event = WorkflowEvent(
                event_id=new_id(),
                project_id=self.repository.project_id,
                project_sequence=workflow.last_project_sequence,
                event_type="artifact.imported",
                occurred_at=artifact.created_at,
                project_revision=workflow.revision,
                command_id=command_id,
                payload={
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "label": artifact.label,
                },
            )
            self.repository.publish_artifact(artifact, content, source.suffix)
            self.repository.append_event(event)
            self.repository.save_workflow(workflow)
        self._publish(event)
        return artifact, event

    def create_run(
        self,
        *,
        command_id: str,
        module_type: str,
        label: str | None = None,
        inputs: list[dict[str, Any]] | None = None,
        module_data: dict[str, Any] | None = None,
        created_by: dict[str, Any] | None = None,
        enqueue: bool = False,
        expected_revision: int | None = None,
    ) -> CommandReceipt:
        if module_type not in MODULE_TYPES:
            raise WorkflowError("invalid_module_type", f"unsupported module_type: {module_type}", status=422)
        if not command_id:
            raise WorkflowError("command_id_required", "command_id is required", status=422)
        if module_type == "experiment":
            module_data = dict(module_data or {})
            module_data.setdefault("experiment_execution_mode", "auto")
            if module_data["experiment_execution_mode"] not in ("auto", "manual"):
                raise WorkflowError("invalid_execution_mode", "experiment_execution_mode must be auto or manual", status=422)
        with self.repository.transaction():
            workflow = self._load_active_workflow()
            duplicate = self._duplicate_receipt(command_id, workflow)
            if duplicate:
                return duplicate
            if module_data and ("target_journal" in module_data or "journal_profile_version" in module_data):
                try:
                    module_data = normalize_journal_selection(module_type, module_data)
                except ValueError as exc:
                    raise WorkflowError("invalid_journal_selection", str(exc), status=422) from exc
            if module_data and "external_read_paths" in module_data:
                module_data = self._validate_input_paths(module_type, module_data)
            self._check_revision(workflow.revision, expected_revision, "project")
            runs = self.repository.list_runs()
            ordinal = 1 + sum(run.module_type == module_type for run in runs)
            run_id = new_id()
            now = utc_now()
            status = "queued" if enqueue else "created"
            event_id = new_id()
            validated_inputs = [ArtifactInput.model_validate(item) for item in (inputs or [])]
            for item in validated_inputs:
                try:
                    self.repository.load_artifact(item.artifact_id)
                except FileNotFoundError as exc:
                    raise WorkflowError("artifact_not_found", str(exc), status=404) from exc
            run = ModuleRun(
                run_id=run_id,
                project_id=self.repository.project_id,
                module_type=module_type,
                label=(label or f"{module_type.replace('_', ' ').title()} {ordinal}").strip(),
                status=status,
                created_at=now,
                updated_at=now,
                created_by=Actor.model_validate(created_by or {"kind": "user"}),
                inputs=validated_inputs,
                conversation_key=f"ui:{self.repository.project_id}:run:{run_id}",
                module_data=module_data or {},
                last_project_sequence=workflow.last_project_sequence + 1,
                last_run_sequence=1,
                last_transition=Transition(
                    event_id=event_id,
                    event_type="run.queued" if enqueue else "run.created",
                    **{"from": None},
                    to=status,
                    occurred_at=now,
                ),
                applied_commands=[AppliedCommand(command_id=command_id, command_type="create_run", result_revision=1)],
            )
            workflow.revision += 1
            workflow.last_project_sequence += 1
            workflow.selected_run_id = run_id
            event = self._event(
                workflow,
                event_id=event_id,
                event_type="run.queued" if enqueue else "run.created",
                command_id=command_id,
                run=run,
                payload={"module_type": module_type, "label": run.label, "status": status},
            )
            self.repository.save_run(run)
            self.repository.append_event(event)
            self.repository.refresh_index(workflow)
            self.repository.save_workflow(workflow)
        self._publish(event)
        return CommandReceipt(
            command_id=command_id,
            disposition="accepted" if enqueue else "applied",
            project_revision=workflow.revision,
            run_id=run.run_id,
            run_revision=run.revision,
            project_sequence=event.project_sequence,
            event=event,
        )

    def command_run(
        self,
        run_id: str,
        *,
        command_id: str,
        command_type: str,
        expected_revision: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> CommandReceipt:
        if not command_id:
            raise WorkflowError("command_id_required", "command_id is required", status=422)
        payload = payload or {}
        published_events: list[WorkflowEvent] = []
        with self.repository.transaction():
            workflow = self._load_active_workflow()
            duplicate = self._duplicate_receipt(command_id, workflow, run_id=run_id)
            if duplicate:
                return duplicate
            try:
                run = self.repository.load_run(run_id)
            except FileNotFoundError as exc:
                raise WorkflowError("run_not_found", str(exc), status=404) from exc
            self._check_revision(run.revision, expected_revision, "run")
            from_status = run.status
            next_status, event_type, disposition = self._transition(run, command_type, payload)
            now = utc_now()
            event_id = new_id()
            workflow.revision += 1
            workflow.last_project_sequence += 1
            run.revision += 1
            run.last_project_sequence = workflow.last_project_sequence
            run.last_run_sequence += 1
            run.updated_at = now
            run.status = next_status
            run.last_transition = Transition(
                event_id=event_id,
                event_type=event_type,
                **{"from": from_status},
                to=next_status,
                occurred_at=now,
            )
            run.applied_commands = (
                run.applied_commands
                + [AppliedCommand(command_id=command_id, command_type=command_type, result_revision=run.revision)]
            )[-100:]
            event = self._event(
                workflow,
                event_id=event_id,
                event_type=event_type,
                command_id=command_id,
                run=run,
                payload={"from": from_status, "to": next_status, **payload},
            )
            self.repository.save_run(run)
            self.repository.append_event(event)
            published_events.append(event)
            if event_type == "run.completed" and workflow.handoff_policy.mode == "auto":
                published_events.extend(
                    self._create_auto_handoffs_locked(
                        workflow,
                        source_run=run,
                        parent_command_id=command_id,
                    )
                )
            self.repository.refresh_index(workflow)
            self.repository.save_workflow(workflow)
        for published_event in published_events:
            self._publish(published_event)
        return CommandReceipt(
            command_id=command_id,
            disposition=disposition,
            project_revision=event.project_revision,
            run_id=run.run_id,
            run_revision=run.revision,
            project_sequence=event.project_sequence,
            event=event,
        )

    def claim_run(
        self,
        run_id: str,
        *,
        lease_owner: str,
        executor_name: str | None = None,
        executor_version: str = "1.0.0",
        agent_profile: str = "research",
    ) -> CommandReceipt | None:
        """Claim a queued Run for an engine-owned execution adapter."""
        with self.repository.transaction():
            workflow = self._load_active_workflow()
            try:
                run = self.repository.load_run(run_id)
            except FileNotFoundError as exc:
                raise WorkflowError("run_not_found", str(exc), status=404) from exc
            if run.status == "running" and run.attempt.lease_owner == lease_owner:
                return None
            recovering = run.status == "running"
            if run.status != "queued" and not recovering:
                raise WorkflowError(
                    "run_not_queued",
                    f"Run must be queued before execution; current status is {run.status}",
                    status=409,
                )
            not_before = run.module_data.get("handoff_not_before")
            if run.status == "queued" and isinstance(not_before, str):
                try:
                    handoff_time = datetime.fromisoformat(not_before.replace("Z", "+00:00"))
                except ValueError:
                    handoff_time = None
                if handoff_time is not None and datetime.now(UTC) < handoff_time:
                    raise WorkflowError(
                        "handoff_grace_active",
                        "automatic handoff is still inside its interrupt grace period",
                        status=409,
                        details={"not_before": not_before},
                    )
            now = utc_now()
            event_id = new_id()
            previous_attempt_id = run.attempt.current_attempt_id
            resolved_executor_name = executor_name or f"mira.{run.module_type}"
            resolved_agent_profile = (
                agent_profile if agent_profile in {"research", "engineer", "team"} else "research"
            )
            workflow.revision += 1
            workflow.last_project_sequence += 1
            run.revision += 1
            run.updated_at = now
            run.last_project_sequence = workflow.last_project_sequence
            run.last_run_sequence += 1

            if recovering:
                if not previous_attempt_id:
                    raise WorkflowError(
                        "attempt_missing", "running Run has no active attempt", status=409
                    )
                checkpoint = Checkpoint(
                    checkpoint_id=new_id(),
                    project_id=self.repository.project_id,
                    run_id=run.run_id,
                    attempt_id=previous_attempt_id,
                    reason="recovery",
                    created_at=now,
                    executor_name=(
                        run.attempts[-1].executor_name
                        if run.attempts
                        else resolved_executor_name
                    ),
                    executor_version=(
                        run.attempts[-1].executor_version
                        if run.attempts
                        else executor_version
                    ),
                    state={
                        "recovery_required": True,
                        "previous_lease_owner": run.attempt.lease_owner,
                    },
                )
                run.checkpoint_ref = checkpoint.checkpoint_id
                self._close_attempt(run, "paused", now)
                run.status = "paused"
                run.attempt.lease_owner = None
                run.attempt.lease_expires_at = None
                run.module_data["recovery"] = {
                    "code": "executor_restarted",
                    "message": "Previous execution ended without a safe-boundary acknowledgement.",
                    "checkpoint_id": checkpoint.checkpoint_id,
                }
                command_id = f"executor-recovery:{new_id()}"
                run.last_transition = Transition(
                    event_id=event_id,
                    event_type="run.recovery_required",
                    **{"from": "running"},
                    to="paused",
                    occurred_at=now,
                )
                run.applied_commands = (
                    run.applied_commands
                    + [AppliedCommand(command_id=command_id, command_type="executor_recovery", result_revision=run.revision)]
                )[-100:]
                event = self._event(
                    workflow,
                    event_id=event_id,
                    event_type="run.recovery_required",
                    command_id=command_id,
                    run=run,
                    payload={
                        "from": "running",
                        "to": "paused",
                        "recovered_from_attempt_id": previous_attempt_id,
                        "checkpoint_id": checkpoint.checkpoint_id,
                        "requires_user_resume": True,
                    },
                )
                self.repository.save_checkpoint(checkpoint)
                disposition = "applied"
            else:
                attempt_id = new_id()
                run.status = "running"
                run.attempt.current_attempt_id = attempt_id
                run.attempt.number += 1
                run.attempt.lease_owner = lease_owner
                run.attempts.append(
                    AttemptRecord(
                        attempt_id=attempt_id,
                        number=run.attempt.number,
                        lease_owner=lease_owner,
                        started_at=now,
                        executor_name=resolved_executor_name,
                        executor_version=executor_version,
                        agent_profile=resolved_agent_profile,
                        resumed_from_checkpoint_id=run.checkpoint_ref,
                    )
                )
                run.last_transition = Transition(
                    event_id=event_id,
                    event_type="run.started",
                    **{"from": "queued"},
                    to="running",
                    occurred_at=now,
                )
                command_id = f"executor-claim:{attempt_id}"
                run.applied_commands = (
                    run.applied_commands
                    + [AppliedCommand(command_id=command_id, command_type="executor_claim", result_revision=run.revision)]
                )[-100:]
                event = self._event(
                    workflow,
                    event_id=event_id,
                    event_type="run.started",
                    command_id=command_id,
                    run=run,
                    payload={"from": "queued", "to": "running", "attempt_id": attempt_id},
                )
                disposition = "accepted"
            self.repository.save_run(run)
            self.repository.append_event(event)
            self.repository.refresh_index(workflow)
            self.repository.save_workflow(workflow)
        self._publish(event)
        return CommandReceipt(
            command_id=command_id,
            disposition=disposition,
            project_revision=workflow.revision,
            run_id=run.run_id,
            run_revision=run.revision,
            project_sequence=event.project_sequence,
            event=event,
        )

    def request_interaction(
        self,
        run_id: str,
        *,
        command_id: str,
        interaction: dict[str, Any],
        checkpoint_state: dict[str, Any] | None = None,
        conversation_cursor: str | int | None = None,
        attempt_id: str | None = None,
        executor_name: str | None = None,
        executor_version: str | None = None,
    ) -> CommandReceipt:
        """Commit a resumable boundary and wait for one typed user response."""
        try:
            request = InteractionRequest.model_validate(interaction)
        except ValidationError:
            raise
        return self._checkpoint_transition(
            run_id,
            command_id=command_id,
            reason="interaction",
            target_status="waiting_input",
            event_type="interaction.requested",
            checkpoint_state=checkpoint_state,
            conversation_cursor=conversation_cursor,
            attempt_id=attempt_id,
            executor_name=executor_name,
            executor_version=executor_version,
            payload={"interaction": request.model_dump(mode="json")},
        )

    def acknowledge_control(
        self,
        run_id: str,
        *,
        command_id: str,
        control: str,
        checkpoint_state: dict[str, Any] | None = None,
        conversation_cursor: str | int | None = None,
        attempt_id: str | None = None,
        executor_name: str | None = None,
        executor_version: str | None = None,
    ) -> CommandReceipt:
        if control not in {"pause", "cancel"}:
            raise WorkflowError("invalid_control", "control must be pause or cancel", status=422)
        return self._checkpoint_transition(
            run_id,
            command_id=command_id,
            reason=control,
            target_status="paused" if control == "pause" else "cancelled",
            event_type=f"run.{control}_acknowledged",
            checkpoint_state=checkpoint_state,
            conversation_cursor=conversation_cursor,
            attempt_id=attempt_id,
            executor_name=executor_name,
            executor_version=executor_version,
            require_pending_control=control,
        )

    def command_project(
        self,
        *,
        command_id: str,
        command_type: str,
        expected_revision: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> CommandReceipt:
        if not command_id:
            raise WorkflowError("command_id_required", "command_id is required", status=422)
        payload = payload or {}
        with self.repository.transaction():
            workflow = self.repository.load_workflow()
            duplicate = self._duplicate_receipt(command_id, workflow)
            if duplicate:
                return duplicate
            self._check_revision(workflow.revision, expected_revision, "project")
            if command_type == "set_handoff_policy":
                policy_payload = payload.get("handoff_policy", payload)
                policy = HandoffPolicy.model_validate(policy_payload)
                if policy.interrupt_grace_seconds < 0:
                    raise WorkflowError(
                        "invalid_handoff_policy",
                        "interrupt_grace_seconds cannot be negative",
                        status=422,
                    )
                workflow.handoff_policy = policy
                event_type = "project.handoff_policy_updated"
                event_payload = {"handoff_policy": policy.model_dump(mode="json")}
            elif command_type == "archive" and workflow.status == "active":
                workflow.status = "archived"
                event_type = "project.archived"
                event_payload = {}
            elif command_type == "restore" and workflow.status == "archived":
                workflow.status = "active"
                event_type = "project.restored"
                event_payload = {}
            else:
                raise WorkflowError(
                    "invalid_transition",
                    f"cannot apply {command_type!r} while Project is {workflow.status!r}",
                    status=409,
                )
            now = utc_now()
            workflow.revision += 1
            workflow.last_project_sequence += 1
            event = WorkflowEvent(
                event_id=new_id(),
                project_id=self.repository.project_id,
                project_sequence=workflow.last_project_sequence,
                event_type=event_type,
                occurred_at=now,
                project_revision=workflow.revision,
                command_id=command_id,
                payload=event_payload,
            )
            self.repository.append_event(event)
            self.repository.save_workflow(workflow)
        self._publish(event)
        return CommandReceipt(
            command_id=command_id,
            disposition="applied",
            project_revision=workflow.revision,
            project_sequence=event.project_sequence,
            event=event,
        )

    def _checkpoint_transition(
        self,
        run_id: str,
        *,
        command_id: str,
        reason: str,
        target_status: str,
        event_type: str,
        checkpoint_state: dict[str, Any] | None,
        conversation_cursor: str | int | None,
        attempt_id: str | None,
        executor_name: str | None,
        executor_version: str | None,
        payload: dict[str, Any] | None = None,
        require_pending_control: str | None = None,
    ) -> CommandReceipt:
        if not command_id:
            raise WorkflowError("command_id_required", "command_id is required", status=422)
        with self.repository.transaction():
            workflow = self._load_active_workflow()
            duplicate = self._duplicate_receipt(command_id, workflow, run_id=run_id)
            if duplicate:
                return duplicate
            try:
                run = self.repository.load_run(run_id)
            except FileNotFoundError as exc:
                raise WorkflowError("run_not_found", str(exc), status=404) from exc
            if run.status != "running":
                raise WorkflowError(
                    "run_not_running", f"Run must be running; current status is {run.status}", status=409
                )
            active_attempt_id = run.attempt.current_attempt_id
            if not active_attempt_id or (attempt_id and attempt_id != active_attempt_id):
                raise WorkflowError("attempt_mismatch", "signal does not belong to the active attempt", status=409)
            if require_pending_control and run.pending_control != require_pending_control:
                raise WorkflowError(
                    "control_not_pending", f"{require_pending_control} is not pending", status=409
                )
            now = utc_now()
            attempt_record = next(
                (
                    candidate
                    for candidate in reversed(run.attempts)
                    if candidate.attempt_id == active_attempt_id
                ),
                None,
            )
            checkpoint = Checkpoint(
                checkpoint_id=new_id(),
                project_id=self.repository.project_id,
                run_id=run.run_id,
                attempt_id=active_attempt_id,
                reason=reason,
                created_at=now,
                executor_name=(
                    executor_name
                    or (
                        attempt_record.executor_name
                        if attempt_record
                        else f"mira.{run.module_type}"
                    )
                ),
                executor_version=(
                    executor_version
                    or (attempt_record.executor_version if attempt_record else "1.0.0")
                ),
                conversation_cursor=conversation_cursor,
                state=checkpoint_state or {},
            )
            from_status = run.status
            event_id = new_id()
            workflow.revision += 1
            workflow.last_project_sequence += 1
            run.revision += 1
            run.status = target_status
            run.updated_at = now
            run.checkpoint_ref = checkpoint.checkpoint_id
            run.last_project_sequence = workflow.last_project_sequence
            run.last_run_sequence += 1
            run.pending_control = None
            run.attempt.lease_owner = None
            run.attempt.lease_expires_at = None
            self._close_attempt(run, target_status, now)
            if payload and isinstance(payload.get("interaction"), dict):
                run.module_data["interaction"] = payload["interaction"]
            run.last_transition = Transition(
                event_id=event_id,
                event_type=event_type,
                **{"from": from_status},
                to=target_status,
                occurred_at=now,
            )
            run.applied_commands = (
                run.applied_commands
                + [AppliedCommand(command_id=command_id, command_type=event_type, result_revision=run.revision)]
            )[-100:]
            event_payload = {
                "from": from_status,
                "to": target_status,
                "checkpoint_id": checkpoint.checkpoint_id,
                **(payload or {}),
            }
            event = self._event(
                workflow,
                event_id=event_id,
                event_type=event_type,
                command_id=command_id,
                run=run,
                payload=event_payload,
            )
            self.repository.save_checkpoint(checkpoint)
            self.repository.save_run(run)
            self.repository.append_event(event)
            self.repository.refresh_index(workflow)
            self.repository.save_workflow(workflow)
        self._publish(event)
        return CommandReceipt(
            command_id=command_id,
            disposition="accepted" if target_status == "waiting_input" else "applied",
            project_revision=workflow.revision,
            run_id=run.run_id,
            run_revision=run.revision,
            project_sequence=event.project_sequence,
            event=event,
        )

    def _transition(self, run: ModuleRun, command: str, payload: dict[str, Any]) -> tuple[str, str, str]:
        status = run.status
        if command == "record_experiment" and status in {"running", "completed"}:
            if run.module_type != "experiment":
                raise WorkflowError("invalid_module", "experiment records require an experiment Run", status=409)
            raw = payload.get("experiment")
            records = list(run.module_data.get("experiments") or [])
            previous = next((item for item in records if isinstance(raw, dict) and item["id"] == raw.get("id")), {})
            try:
                if not isinstance(raw, dict):
                    raise ValueError("experiment must be an object")
                record = ExperimentRecord.model_validate({**previous, **raw}).model_dump(mode="json")
            except (ValidationError, ValueError) as exc:
                raise WorkflowError("invalid_experiment", str(exc), status=422) from exc
            if status == "completed" and record["status"] not in {"completed", "failed", "skipped"}:
                raise WorkflowError("invalid_experiment", "a completed Run accepts only terminal experiment records", status=409)
            if record["status"] != "pending" and confirmation_required(run) and not confirmation_approved(run):
                raise WorkflowError("confirmation_required", "approve the experiment scope first", status=409)
            policy = run.module_data.get("automation_policy") or {}
            limit = policy.get("maxExperiments") if isinstance(policy, dict) else None
            if not previous and isinstance(limit, int) and limit > 0 and len(records) >= limit:
                raise WorkflowError("experiment_limit", "maxExperiments limit reached", status=409)
            paths = record["results"].get("artifacts", [])
            if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
                raise WorkflowError("invalid_experiment", "results.artifacts must be an array of project-relative paths", status=422)
            paths = [*paths, *(ref[key] for ref in record["evidence_refs"] for key in ("artifact", "path") if key in ref)]
            for path in paths:
                if not isinstance(path, str):
                    raise WorkflowError("invalid_experiment", "artifact paths must be strings", status=422)
                if Path(path).is_absolute() or not (self.repository.project_dir / path).resolve().is_relative_to(self.repository.project_dir.resolve()):
                    raise WorkflowError("invalid_experiment", "artifact path escapes Project", status=422)
            if status == "running" and manual_experiments(run):
                if previous.get("status") == "running" and record["status"] == "pending":
                    raise WorkflowError("invalid_experiment", "finish or skip the active experiment before resetting it", status=409)
                if record["status"] == "running" and previous.get("status") != "running":
                    if any(item["status"] == "running" for item in records):
                        raise WorkflowError("experiment_in_progress", "manual mode permits only one active experiment", status=409)
                    if run.module_data.get("_manual_experiment_boundary"):
                        if experiment_boundary_decision(run) != "continue":
                            raise WorkflowError("experiment_confirmation_required", "confirm continuation before starting another experiment", status=409)
                        run.module_data.pop("_manual_experiment_boundary")
                if record["status"] in {"completed", "failed"} and previous.get("status") not in {"running", "completed", "failed"}:
                    raise WorkflowError("experiment_not_started", "register running before executing a manual experiment", status=409)
                if previous.get("status") == "running" and record["status"] in {"completed", "failed", "skipped"}:
                    run.module_data["_manual_experiment_boundary"] = {
                        "interaction_id": new_id(), "experiment_id": record["id"], "title": record["title"],
                    }
            # Replace in place: retries and progress updates do not add duplicates.
            run.module_data["experiments"] = [
                record if item["id"] == record["id"] else item for item in records
            ] if previous else [*records, record]
            return status, "run.experiment_recorded", "applied"
        if command == "update_draft" and status == "created":
            proposed = {**run.module_data, **(payload.get("module_data") or {})}
            try:
                proposed = normalize_journal_selection(run.module_type, proposed)
            except ValueError as exc:
                raise WorkflowError("invalid_journal_selection", str(exc), status=422) from exc
            if proposed.get("target_journal"):
                payload = dict(payload, module_data={
                    **(payload.get("module_data") or {}),
                    "journal_profile_version": proposed["journal_profile_version"],
                })
            if "external_read_paths" in (payload.get("module_data") or {}):
                payload = dict(payload, module_data=self._validate_input_paths(run.module_type, payload["module_data"]))
            mode = (payload.get("module_data") or {}).get("experiment_execution_mode", "auto")
            if run.module_type == "experiment" and mode not in ("auto", "manual"):
                raise WorkflowError("invalid_execution_mode", "experiment_execution_mode must be auto or manual", status=422)
            run.module_data.update(payload.get("module_data") or {})
            return "created", "run.draft_updated", "applied"
        if command == "enqueue" and status == "created":
            return "queued", "run.queued", "accepted"
        if command == "record_research_result" and status == "running":
            if run.module_type != "initiation":
                raise WorkflowError(
                    "invalid_module",
                    "structured research results belong to initiation Runs",
                    status=409,
                )
            try:
                result = InitiationResearchResult.model_validate(payload.get("research_result"))
            except ValidationError as exc:
                raise WorkflowError(
                    "invalid_research_result",
                    "research_result does not match the initiation result contract",
                    status=422,
                    details={"errors": exc.errors(include_url=False)},
                ) from exc
            literature = run.module_data.get("literature_review")
            literature_enabled = (
                isinstance(literature, dict) and literature.get("enabled") is True
            )
            selected_sources = {
                str(source).strip().lower()
                for source in (literature.get("sources") or [])
                if isinstance(source, str) and source.strip()
            } if isinstance(literature, dict) else set()
            recorded_searches = run.module_data.get("research_searches")
            searches = recorded_searches if isinstance(recorded_searches, list) else []
            if literature_enabled and "pubmed" in selected_sources:
                completed_pubmed = [
                    search
                    for search in searches
                    if isinstance(search, dict)
                    and search.get("source") == "pubmed"
                    and search.get("status") == "completed"
                    and int(search.get("result_count") or 0) > 0
                ]
                if not completed_pubmed:
                    raise WorkflowError(
                        "literature_search_required",
                        "PubMed was selected but no successful PubMed search is recorded",
                        status=409,
                    )
                evidence_urls = {
                    url
                    for search in completed_pubmed
                    for url in (search.get("evidence_urls") or [])
                    if isinstance(url, str)
                }
                unverified_references = [
                    reference.id
                    for reference in result.references
                    if not reference.url or reference.url not in evidence_urls
                ]
                if unverified_references:
                    raise WorkflowError(
                        "unverified_literature_references",
                        "PubMed references must use URLs returned by this Run's searches",
                        status=409,
                        details={"reference_ids": unverified_references},
                    )
            if literature_enabled and not result.references:
                raise WorkflowError(
                    "literature_references_required",
                    "enabled literature research must record at least one verifiable reference",
                    status=409,
                )
            result.searches = [
                LiteratureSearchRecord.model_validate(search)
                for search in searches
                if isinstance(search, dict)
            ]
            run.module_data["research_result"] = result.model_dump(mode="json")
            return "running", "run.research_result_updated", "applied"
        if command == "record_literature_search" and status == "running":
            if run.module_type != "initiation":
                raise WorkflowError(
                    "invalid_module",
                    "literature search records belong to initiation Runs",
                    status=409,
                )
            try:
                search = LiteratureSearchRecord.model_validate(payload.get("search"))
            except ValidationError as exc:
                raise WorkflowError(
                    "invalid_literature_search",
                    "search does not match the literature-search record contract",
                    status=422,
                    details={"errors": exc.errors(include_url=False)},
                ) from exc
            searches = run.module_data.setdefault("research_searches", [])
            if not isinstance(searches, list):
                searches = []
                run.module_data["research_searches"] = searches
            searches.append(search.model_dump(mode="json"))
            return "running", "run.literature_search_recorded", "applied"
        if command == "pause" and status in {"queued", "waiting_input"}:
            return "paused", "run.paused", "applied"
        if command == "pause" and status == "running":
            run.pending_control = "pause"
            return "running", "run.pause_requested", "pending_safe_boundary"
        if command == "resume" and status == "paused":
            run.pending_control = None
            run.module_data.pop("recovery", None)
            interaction = run.module_data.get("interaction")
            if isinstance(interaction, dict) and interaction.get("response") is None:
                return "waiting_input", "run.waiting_input", "applied"
            return "queued", "run.queued", "accepted"
        if command == "cancel" and status not in {"completed", "cancelled"}:
            if status == "running":
                run.pending_control = "cancel"
                return "running", "run.cancel_requested", "pending_safe_boundary"
            return "cancelled", "run.cancelled", "applied"
        if command == "retry" and status == "failed":
            run.pending_control = None
            run.module_data.pop("failure", None)
            interaction = run.module_data.get("interaction")
            if isinstance(interaction, dict) and interaction.get("response") is None:
                return "waiting_input", "run.waiting_input", "applied"
            return "queued", "run.queued", "accepted"
        if command == "fail" and status in {"queued", "running", "waiting_input"}:
            code = payload.get("code")
            message = payload.get("message")
            run.module_data["failure"] = {
                "code": code if isinstance(code, str) and code else "execution_failed",
                "message": message
                if isinstance(message, str) and message
                else "Module execution failed",
                "retryable": bool(payload.get("retryable", True)),
            }
            run.pending_control = None
            run.attempt.lease_owner = None
            run.attempt.lease_expires_at = None
            self._close_attempt(run, "failed", utc_now())
            return "failed", "run.failed", "applied"
        if command == "complete" and status in {"running", "waiting_input", "paused"}:
            if manual_experiments(run) and run.module_data.get("_manual_experiment_boundary") and experiment_boundary_decision(run) is None:
                raise WorkflowError("experiment_confirmation_required", "answer the experiment continuation form before completing the Run", status=409)
            if any(item.get("status") in {"pending", "running"} for item in run.module_data.get("experiments", [])):
                raise WorkflowError("experiments_unfinished", "finish or explicitly skip registered experiments before completing the Run", status=409)
            interaction = run.module_data.get("interaction")
            if isinstance(interaction, dict) and interaction.get("response") is None:
                raise WorkflowError(
                    "interaction_required", "answer the pending interaction before completion", status=409
                )
            if confirmation_required(run) and not confirmation_approved(run):
                raise WorkflowError(
                    "confirmation_required",
                    "the module execution brief must be approved before completion",
                    status=409,
                )
            outputs = payload.get("outputs", [])
            if not isinstance(outputs, list) or not all(isinstance(item, str) for item in outputs):
                raise WorkflowError("invalid_outputs", "outputs must be an array of Artifact IDs", status=422)
            unique_outputs = list(dict.fromkeys(outputs))
            for artifact_id in unique_outputs:
                try:
                    self.repository.load_artifact(artifact_id)
                except FileNotFoundError as exc:
                    raise WorkflowError("artifact_not_found", str(exc), status=404) from exc
            run.outputs = unique_outputs
            summary = payload.get("summary")
            if isinstance(summary, str) and summary.strip():
                run.module_data["completion_summary"] = summary.strip()
            suggested_handoffs = payload.get("suggested_handoffs", [])
            if not isinstance(suggested_handoffs, list) or not all(
                isinstance(item, dict) for item in suggested_handoffs
            ):
                raise WorkflowError(
                    "invalid_handoffs", "suggested_handoffs must be an array of objects", status=422
                )
            for suggestion in suggested_handoffs:
                if suggestion.get("target_module_type") not in MODULE_TYPES:
                    raise WorkflowError(
                        "invalid_handoffs", "each handoff needs a supported target_module_type", status=422
                    )
                requested_inputs = suggestion.get("input_artifact_ids", unique_outputs)
                if not isinstance(requested_inputs, list) or not all(
                    isinstance(item, str) for item in requested_inputs
                ):
                    raise WorkflowError(
                        "invalid_handoffs",
                        "input_artifact_ids must be an array of Artifact IDs",
                        status=422,
                    )
                if not set(requested_inputs).issubset(unique_outputs):
                    raise WorkflowError(
                        "invalid_handoffs",
                        "handoff inputs must be outputs of the completed Run",
                        status=422,
                    )
            run.module_data["suggested_handoffs"] = suggested_handoffs
            run.pending_control = None
            run.attempt.lease_owner = None
            run.attempt.lease_expires_at = None
            self._close_attempt(run, "completed", utc_now())
            return "completed", "run.completed", "applied"
        if command == "submit_interaction" and status == "waiting_input":
            interaction = run.module_data.get("interaction")
            interaction_id = payload.get("interaction_id")
            if not isinstance(interaction, dict) or interaction.get("interaction_id") != interaction_id:
                raise WorkflowError("interaction_not_found", "active interaction does not match", status=409)
            if interaction.get("response") is not None:
                raise WorkflowError("interaction_already_resolved", "interaction already resolved", status=409)
            response = payload.get("response")
            self._validate_interaction_response(interaction.get("response_schema"), response)
            if isinstance(response, dict) and "selection" in response:
                selection = response["selection"]
                if not isinstance(selection, str) or not selection.strip():
                    raise WorkflowError(
                        "interaction_response_invalid", "selection must not be blank", status=422
                    )
                approval = str(interaction.get("interaction_type", "")).startswith("approve_")
                allowed = {
                    str(choice.get("value") or choice.get("id"))
                    for choice in interaction.get("choices", [])
                }
                if (approval and selection not in {"approve", "revise"}) or (
                    not interaction.get("allow_freeform", True) and selection not in allowed
                ):
                    raise WorkflowError(
                        "interaction_response_invalid", "select one of the offered choices", status=422
                    )
                if approval and selection == "revise" and not str(response.get("notes") or "").strip():
                    raise WorkflowError(
                        "interaction_response_invalid", "revision requires explanatory notes", status=422
                    )
            interaction["response"] = response
            interaction["resolved_at"] = utc_now()
            history = run.module_data.setdefault("interaction_history", [])
            if not isinstance(history, list):
                history = []
            run.module_data["interaction_history"] = [
                *history,
                copy.deepcopy(interaction),
            ][-100:]
            return "queued", "interaction.resolved", "accepted"
        raise WorkflowError(
            "invalid_transition",
            f"cannot apply {command!r} while Run is {status!r}",
            status=409,
            details={"command_type": command, "status": status},
        )

    def _create_auto_handoffs_locked(
        self,
        workflow: ProjectWorkflow,
        *,
        source_run: ModuleRun,
        parent_command_id: str,
    ) -> list[WorkflowEvent]:
        suggestions = source_run.module_data.get("suggested_handoffs")
        if not isinstance(suggestions, list):
            return []
        events: list[WorkflowEvent] = []
        for index, suggestion in enumerate(suggestions):
            if not isinstance(suggestion, dict):
                continue
            target = suggestion.get("target_module_type")
            if target not in MODULE_TYPES or not self._handoff_allowed(workflow, source_run.module_type, target):
                continue
            requested_inputs = suggestion.get("input_artifact_ids", source_run.outputs)
            if not isinstance(requested_inputs, list) or not all(
                isinstance(item, str) for item in requested_inputs
            ):
                raise WorkflowError(
                    "invalid_handoffs", "input_artifact_ids must be an array of Artifact IDs", status=422
                )
            allowed_outputs = set(source_run.outputs)
            if not set(requested_inputs).issubset(allowed_outputs):
                raise WorkflowError(
                    "invalid_handoffs",
                    "automatic handoff inputs must be outputs of the completed Run",
                    status=422,
                )
            for artifact_id in requested_inputs:
                self.repository.load_artifact(artifact_id)
            now = utc_now()
            run_id = new_id()
            event_id = new_id()
            ordinal = 1 + sum(
                run.module_type == target for run in self.repository.list_runs()
            )
            child = ModuleRun(
                run_id=run_id,
                project_id=self.repository.project_id,
                module_type=target,
                label=str(suggestion.get("label") or f"{target.title()} {ordinal}"),
                status="queued",
                created_at=now,
                updated_at=now,
                created_by=Actor(kind="system"),
                inputs=[ArtifactInput(artifact_id=item) for item in requested_inputs],
                conversation_key=f"ui:{self.repository.project_id}:run:{run_id}",
                module_data={
                    "parent_run_id": source_run.run_id,
                    "handoff_summary": suggestion.get("summary"),
                    "auto_handoff": True,
                    "confirmation_policy": {"mode": "required"},
                    "handoff_not_before": (
                        datetime.now(UTC)
                        + timedelta(seconds=workflow.handoff_policy.interrupt_grace_seconds)
                    ).isoformat().replace("+00:00", "Z"),
                },
                last_project_sequence=workflow.last_project_sequence + 1,
                last_run_sequence=1,
                last_transition=Transition(
                    event_id=event_id,
                    event_type="run.auto_handoff_created",
                    **{"from": None},
                    to="queued",
                    occurred_at=now,
                ),
                applied_commands=[
                    AppliedCommand(
                        command_id=f"{parent_command_id}:handoff:{index}",
                        command_type="auto_handoff",
                        result_revision=1,
                    )
                ],
            )
            workflow.revision += 1
            workflow.last_project_sequence += 1
            workflow.selected_run_id = child.run_id
            event = self._event(
                workflow,
                event_id=event_id,
                event_type="run.auto_handoff_created",
                command_id=f"{parent_command_id}:handoff:{index}",
                run=child,
                payload={
                    "module_type": target,
                    "status": "queued",
                    "source_run_id": source_run.run_id,
                    "input_artifact_ids": requested_inputs,
                },
            )
            self.repository.save_run(child)
            self.repository.append_event(event)
            events.append(event)
        return events

    @staticmethod
    def _handoff_allowed(workflow: ProjectWorkflow, source: str, target: str) -> bool:
        if not workflow.handoff_policy.rules:
            return True
        for rule in workflow.handoff_policy.rules:
            rule_source = rule.get("source_module_type", rule.get("from"))
            rule_target = rule.get("target_module_type", rule.get("to"))
            if rule_source in {None, "*", source} and rule_target in {None, "*", target}:
                return bool(rule.get("enabled", True))
        return False

    @staticmethod
    def _close_attempt(run: ModuleRun, outcome: str, ended_at: str) -> None:
        current_id = run.attempt.current_attempt_id
        if not current_id:
            return
        for attempt in reversed(run.attempts):
            if attempt.attempt_id == current_id and attempt.ended_at is None:
                attempt.ended_at = ended_at
                attempt.outcome = outcome
                return

    @staticmethod
    def _validate_interaction_response(schema: Any, response: Any) -> None:
        if not isinstance(schema, dict):
            raise WorkflowError("invalid_interaction_schema", "interaction response schema is invalid", status=500)
        errors = WorkflowService._schema_errors(schema, response)
        if errors:
            raise WorkflowError(
                "interaction_response_invalid",
                "; ".join(errors),
                status=422,
            )

    @staticmethod
    def _schema_errors(schema: dict[str, Any], value: Any, path: str = "response") -> list[str]:
        expected_type = schema.get("type")
        type_map = {
            "object": dict,
            "array": list,
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
        }
        expected_python = type_map.get(expected_type)
        if expected_python is not None and (
            not isinstance(value, expected_python)
            or expected_type in {"number", "integer"} and isinstance(value, bool)
        ):
            return [f"{path} must match schema type {expected_type}"]
        errors: list[str] = []
        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{path} must be one of {schema['enum']}")
        if expected_type == "string" and isinstance(value, str):
            if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
                errors.append(f"{path} exceeds maxLength {schema['maxLength']}")
            if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
                errors.append(f"{path} is shorter than minLength {schema['minLength']}")
        if expected_type == "object" and isinstance(value, dict):
            missing = [key for key in schema.get("required", []) if key not in value]
            if missing:
                errors.append(f"{path} is missing required fields: {', '.join(missing)}")
            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False and isinstance(properties, dict):
                unexpected = set(value) - set(properties)
                if unexpected:
                    errors.append(
                        f"{path} contains unexpected fields: {', '.join(sorted(unexpected))}"
                    )
            if isinstance(properties, dict):
                for key, item in value.items():
                    item_schema = properties.get(key)
                    if isinstance(item_schema, dict):
                        errors.extend(
                            WorkflowService._schema_errors(item_schema, item, f"{path}.{key}")
                        )
        if expected_type == "array" and isinstance(value, list) and isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                errors.extend(
                    WorkflowService._schema_errors(schema["items"], item, f"{path}[{index}]")
                )
        return errors

    def _load_active_workflow(self) -> ProjectWorkflow:
        project = self.repository.load_project_document()
        workflow = ProjectWorkflow.model_validate(project.get("workflow") or {})
        if workflow.status != "active":
            raise WorkflowError("project_archived", "workflow project is archived", status=423)
        return workflow

    @staticmethod
    def _check_revision(actual: int, expected: int | None, resource: str) -> None:
        if expected is not None and actual != expected:
            raise WorkflowError(
                "revision_conflict",
                f"{resource} revision is {actual}, expected {expected}",
                status=409,
                details={"actual_revision": actual, "expected_revision": expected},
            )

    def _duplicate_receipt(
        self, command_id: str, workflow: ProjectWorkflow, *, run_id: str | None = None
    ) -> CommandReceipt | None:
        event = self.repository.find_command_event(command_id)
        if event is None:
            return None
        if run_id is not None and event.run_id != run_id:
            raise WorkflowError("command_id_conflict", "command_id was used for another resource", status=409)
        return CommandReceipt(
            command_id=command_id,
            disposition="duplicate",
            project_revision=event.project_revision,
            run_id=event.run_id,
            run_revision=event.run_revision,
            project_sequence=event.project_sequence,
            event=event,
        )

    def _event(
        self,
        workflow: ProjectWorkflow,
        *,
        event_id: str,
        event_type: str,
        command_id: str,
        run: ModuleRun,
        payload: dict[str, Any],
    ) -> WorkflowEvent:
        return WorkflowEvent(
            event_id=event_id,
            project_id=self.repository.project_id,
            project_sequence=workflow.last_project_sequence,
            run_id=run.run_id,
            run_sequence=run.last_run_sequence,
            event_type=event_type,
            occurred_at=run.updated_at,
            project_revision=workflow.revision,
            run_revision=run.revision,
            command_id=command_id,
            payload=payload,
        )

    def _publish(self, event: WorkflowEvent) -> None:
        if self.on_event:
            self.on_event(event)


def error_payload(error: Exception) -> tuple[int, dict[str, Any]]:
    if isinstance(error, WorkflowError):
        return error.status, {
            "error": {
                "code": error.code,
                "message": str(error),
                "details": error.details,
            }
        }
    if isinstance(error, ValidationError):
        return 422, {"error": {"code": "validation_error", "message": str(error), "details": {}}}
    return 500, {"error": {"code": "internal_error", "message": "workflow request failed", "details": {}}}
