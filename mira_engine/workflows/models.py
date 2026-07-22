"""Versioned workflow models shared by storage and transport adapters."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

WORKFLOW_CONTRACT_VERSION = 1
MODULE_TYPES = ("initiation", "planning", "experiment", "delivery")

ModuleType = Literal["initiation", "planning", "experiment", "delivery"]
RunStatus = Literal[
    "created",
    "queued",
    "running",
    "waiting_input",
    "paused",
    "completed",
    "failed",
    "cancelled",
]


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid4())


class WorkflowModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class Actor(WorkflowModel):
    kind: Literal["user", "system", "module_run", "external_import"] = "user"
    id: str | None = None


class ArtifactInput(WorkflowModel):
    artifact_id: str
    expected_type: str | None = None
    supported_schema: str | None = None


class AttemptState(WorkflowModel):
    current_attempt_id: str | None = None
    number: int = 0
    lease_owner: str | None = None
    lease_expires_at: str | None = None


class AttemptRecord(WorkflowModel):
    attempt_id: str
    number: int
    lease_owner: str
    started_at: str
    ended_at: str | None = None
    outcome: Literal["running", "waiting_input", "paused", "completed", "failed", "cancelled"] = "running"
    resumed_from_checkpoint_id: str | None = None


class Checkpoint(WorkflowModel):
    schema_version: int = 1
    checkpoint_contract_version: int = 1
    checkpoint_id: str
    project_id: str
    run_id: str
    attempt_id: str
    reason: Literal["interaction", "pause", "cancel", "safe_boundary", "completion", "recovery"]
    created_at: str = Field(default_factory=utc_now)
    executor_name: str = "mira.conversation"
    executor_version: str = "1"
    conversation_cursor: str | int | None = None
    state: dict[str, Any] = Field(default_factory=dict)


class AppliedCommand(WorkflowModel):
    command_id: str
    command_type: str
    result_revision: int


class Transition(WorkflowModel):
    event_id: str
    event_type: str
    from_status: str | None = Field(default=None, alias="from")
    to: str
    occurred_at: str


class ModuleRun(WorkflowModel):
    schema_version: int = 1
    workflow_contract_version: int = WORKFLOW_CONTRACT_VERSION
    run_id: str
    project_id: str
    module_type: ModuleType
    label: str
    status: RunStatus = "created"
    revision: int = 1
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    created_by: Actor = Field(default_factory=Actor)
    inputs: list[ArtifactInput] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    attempt: AttemptState = Field(default_factory=AttemptState)
    attempts: list[AttemptRecord] = Field(default_factory=list)
    pending_control: Literal["pause", "cancel"] | None = None
    conversation_key: str
    checkpoint_ref: str | None = None
    last_project_sequence: int = 0
    last_run_sequence: int = 0
    last_transition: Transition | None = None
    applied_commands: list[AppliedCommand] = Field(default_factory=list)
    module_data: dict[str, Any] = Field(default_factory=dict)


class ModuleIndexEntry(WorkflowModel):
    run_id: str
    module_type: ModuleType
    label: str
    status: RunStatus
    revision: int
    updated_at: str


class HandoffPolicy(WorkflowModel):
    mode: Literal["manual", "auto"] = "manual"
    interrupt_grace_seconds: int = 10
    rules: list[dict[str, Any]] = Field(default_factory=list)


class ProjectWorkflow(WorkflowModel):
    contract_version: int = WORKFLOW_CONTRACT_VERSION
    status: Literal["active", "archived"] = "active"
    revision: int = 0
    last_project_sequence: int = 0
    handoff_policy: HandoffPolicy = Field(default_factory=HandoffPolicy)
    selected_run_id: str | None = None
    module_index: list[ModuleIndexEntry] = Field(default_factory=list)


class WorkflowEvent(WorkflowModel):
    schema_version: int = 1
    message_type: Literal["workflow_event"] = "workflow_event"
    workflow_contract_version: int = WORKFLOW_CONTRACT_VERSION
    event_id: str
    project_id: str
    project_sequence: int
    run_id: str | None = None
    run_sequence: int | None = None
    event_type: str
    occurred_at: str
    project_revision: int
    run_revision: int | None = None
    command_id: str | None = None
    correlation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class Artifact(WorkflowModel):
    schema_version: int = 1
    artifact_id: str
    artifact_type: str
    artifact_schema_version: int = 1
    label: str
    media_type: str = "application/octet-stream"
    byte_size: int
    sha256: str
    payload_ref: str
    created_at: str = Field(default_factory=utc_now)
    created_by: Actor
    derived_from: list[str] = Field(default_factory=list)


class CommandReceipt(WorkflowModel):
    command_id: str
    disposition: Literal["applied", "accepted", "pending_safe_boundary", "duplicate"]
    project_revision: int
    run_id: str | None = None
    run_revision: int | None = None
    project_sequence: int
    event: WorkflowEvent | None = None


class InteractionRequest(WorkflowModel):
    interaction_id: str
    interaction_type: str
    title: str
    prompt: str
    response_schema: dict[str, Any]
    choices: list[dict[str, Any]] = Field(default_factory=list)
    allow_freeform: bool = True
