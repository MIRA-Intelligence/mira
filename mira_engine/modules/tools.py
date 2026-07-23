"""Agent-facing tools that emit validated workflow executor signals."""

from __future__ import annotations

import json
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from mira_engine.agent.tools.base import Tool
from mira_engine.workflows import JsonWorkflowRepository, WorkflowService
from mira_engine.workflows.models import new_id


class WorkflowInteractionTool(Tool):
    """Turn an AI decision point into durable Run state and a UI widget."""

    def __init__(self) -> None:
        self._context: ContextVar[tuple[str, Path, str | None] | None] = ContextVar(
            f"workflow_interaction_context_{id(self)}", default=None
        )

    @property
    def name(self) -> str:
        return "request_workflow_interaction"

    @property
    def description(self) -> str:
        return (
            "Pause the active MIRA ModuleRun at a durable checkpoint and ask the user "
            "one structured question. Use only when a user decision is required; after "
            "calling it, briefly say that the Run is waiting and end the turn."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "interaction_type": {
                    "type": "string",
                    "description": "Stable semantic type, such as choose_direction or approve_plan.",
                },
                "title": {"type": "string", "maxLength": 160},
                "prompt": {"type": "string", "maxLength": 4000},
                "choices": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "label": {"type": "string"},
                            "description": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["id", "label"],
                    },
                    "maxItems": 12,
                },
                "allow_freeform": {"type": "boolean"},
            },
            "required": ["interaction_type", "title", "prompt", "choices", "allow_freeform"],
        }

    def set_project_context(self, project_id: str, project_dir: str) -> None:
        current = self._context.get()
        run_id = current[2] if current else None
        self._context.set((project_id, Path(project_dir), run_id))

    def clear_project_context(self) -> None:
        self._context.set(None)

    def set_workflow_run_context(self, run_id: str | None) -> None:
        current = self._context.get()
        if current:
            self._context.set((current[0], current[1], run_id))

    async def execute(
        self,
        *,
        interaction_type: str,
        title: str,
        prompt: str,
        choices: list[dict[str, Any]],
        allow_freeform: bool,
    ) -> str:
        context = self._context.get()
        if context is None or not context[2]:
            return "Error: request_workflow_interaction is available only inside a ModuleRun"
        project_id, project_dir, run_id = context
        service = WorkflowService(JsonWorkflowRepository(project_dir, project_id))
        run = service.repository.load_run(run_id)
        if run.status != "running" or not run.attempt.current_attempt_id:
            return f"Error: ModuleRun is not actively running (status={run.status})"
        normalized_choices = []
        for choice in choices:
            normalized_choices.append(
                {
                    "id": str(choice["id"]),
                    "label": str(choice["label"]),
                    **(
                        {"description": str(choice["description"])}
                        if choice.get("description")
                        else {}
                    ),
                    "value": str(choice.get("value") or choice["id"]),
                }
            )
        interaction_id = new_id()
        receipt = service.request_interaction(
            run_id,
            command_id=f"executor-interaction:{interaction_id}",
            attempt_id=run.attempt.current_attempt_id,
            checkpoint_state={"interaction_type": interaction_type},
            interaction={
                "interaction_id": interaction_id,
                "interaction_type": interaction_type,
                "title": title,
                "prompt": prompt,
                "response_schema": {
                    "type": "object",
                    "required": ["selection"],
                    "properties": {
                        "selection": {"type": "string"},
                        "notes": {"type": "string", "maxLength": 4000},
                    },
                    "additionalProperties": False,
                },
                "choices": normalized_choices,
                "allow_freeform": allow_freeform,
            },
        )
        return json.dumps(
            {
                "status": "waiting_input",
                "interaction_id": interaction_id,
                "checkpoint_id": receipt.event.payload.get("checkpoint_id")
                if receipt.event
                else None,
                "instruction": "End this turn and wait for the user's structured response.",
            },
            ensure_ascii=False,
        )


class WorkflowArtifactTool(Tool):
    """Publish one Project-relative file as an immutable workflow Artifact."""

    def __init__(self) -> None:
        self._context: ContextVar[tuple[str, Path, str | None] | None] = ContextVar(
            f"workflow_artifact_context_{id(self)}", default=None
        )

    @property
    def name(self) -> str:
        return "publish_workflow_artifact"

    @property
    def description(self) -> str:
        return (
            "Publish a completed Project-relative file as an immutable Artifact "
            "owned by the active ModuleRun. Returns its Artifact ID."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "source_path": {"type": "string"},
                "artifact_type": {"type": "string"},
                "label": {"type": "string"},
                "media_type": {"type": "string"},
            },
            "required": ["source_path", "artifact_type", "label", "media_type"],
        }

    def set_project_context(self, project_id: str, project_dir: str) -> None:
        current = self._context.get()
        self._context.set((project_id, Path(project_dir), current[2] if current else None))

    def clear_project_context(self) -> None:
        self._context.set(None)

    def set_workflow_run_context(self, run_id: str | None) -> None:
        current = self._context.get()
        if current:
            self._context.set((current[0], current[1], run_id))

    async def execute(
        self,
        *,
        source_path: str,
        artifact_type: str,
        label: str,
        media_type: str,
    ) -> str:
        context = self._context.get()
        if context is None or not context[2]:
            return "Error: publish_workflow_artifact is available only inside a ModuleRun"
        project_id, project_dir, run_id = context
        service = WorkflowService(JsonWorkflowRepository(project_dir, project_id))
        run = service.repository.load_run(run_id)
        if run.status != "running":
            return f"Error: ModuleRun is not actively running (status={run.status})"
        artifact, event = service.import_artifact(
            command_id=f"executor-artifact:{new_id()}",
            source_path=source_path,
            artifact_type=artifact_type,
            label=label,
            media_type=media_type,
            derived_from=[item.artifact_id for item in run.inputs],
        )
        return json.dumps(
            {
                "artifact_id": artifact.artifact_id,
                "artifact_type": artifact.artifact_type,
                "label": artifact.label,
                "project_sequence": event.project_sequence,
            },
            ensure_ascii=False,
        )


class WorkflowCompletionTool(Tool):
    """Commit a native executor's final outputs and optional handoff."""

    def __init__(self) -> None:
        self._context: ContextVar[tuple[str, Path, str | None] | None] = ContextVar(
            f"workflow_completion_context_{id(self)}", default=None
        )

    @property
    def name(self) -> str:
        return "complete_workflow_run"

    @property
    def description(self) -> str:
        return (
            "Complete the active ModuleRun only after its requested outcome is "
            "finished. Pass immutable output Artifact IDs and explicit downstream "
            "suggestions."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "maxLength": 4000},
                "output_artifact_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "suggested_handoffs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "target_module_type": {
                                "type": "string",
                                "enum": [
                                    "initiation",
                                    "planning",
                                    "experiment",
                                    "delivery",
                                ],
                            },
                            "input_artifact_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "summary": {"type": "string"},
                        },
                        "required": [
                            "target_module_type",
                            "input_artifact_ids",
                        ],
                    },
                },
            },
            "required": [
                "summary",
                "output_artifact_ids",
                "suggested_handoffs",
            ],
        }

    def set_project_context(self, project_id: str, project_dir: str) -> None:
        current = self._context.get()
        self._context.set((project_id, Path(project_dir), current[2] if current else None))

    def clear_project_context(self) -> None:
        self._context.set(None)

    def set_workflow_run_context(self, run_id: str | None) -> None:
        current = self._context.get()
        if current:
            self._context.set((current[0], current[1], run_id))

    async def execute(
        self,
        *,
        summary: str,
        output_artifact_ids: list[str],
        suggested_handoffs: list[dict[str, Any]],
    ) -> str:
        context = self._context.get()
        if context is None or not context[2]:
            return "Error: complete_workflow_run is available only inside a ModuleRun"
        project_id, project_dir, run_id = context
        service = WorkflowService(JsonWorkflowRepository(project_dir, project_id))
        run = service.repository.load_run(run_id)
        receipt = service.command_run(
            run_id,
            command_id=f"executor-complete:{new_id()}",
            command_type="complete",
            expected_revision=run.revision,
            payload={
                "summary": summary,
                "outputs": output_artifact_ids,
                "suggested_handoffs": suggested_handoffs,
            },
        )
        return json.dumps(
            {
                "status": "completed",
                "run_id": run_id,
                "project_sequence": receipt.project_sequence,
                "instruction": "End this turn without more tool calls.",
            },
            ensure_ascii=False,
        )
