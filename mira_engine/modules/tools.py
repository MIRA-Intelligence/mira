"""Agent-facing tools that emit validated workflow executor signals."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mira_engine.agent.tools.base import Tool
from mira_engine.workflows import JsonWorkflowRepository, WorkflowService
from mira_engine.workflows.models import new_id


class WorkflowInteractionTool(Tool):
    """Turn an AI decision point into durable Run state and a UI widget."""

    def __init__(self) -> None:
        self._project_id: str | None = None
        self._project_dir: Path | None = None
        self._run_id: str | None = None

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
        self._project_id = project_id
        self._project_dir = Path(project_dir)

    def clear_project_context(self) -> None:
        self._project_id = None
        self._project_dir = None
        self._run_id = None

    def set_workflow_run_context(self, run_id: str | None) -> None:
        self._run_id = run_id

    async def execute(
        self,
        *,
        interaction_type: str,
        title: str,
        prompt: str,
        choices: list[dict[str, Any]],
        allow_freeform: bool,
    ) -> str:
        if not self._project_id or self._project_dir is None or not self._run_id:
            return "Error: request_workflow_interaction is available only inside a ModuleRun"
        service = WorkflowService(
            JsonWorkflowRepository(self._project_dir, self._project_id)
        )
        run = service.repository.load_run(self._run_id)
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
            self._run_id,
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
                "checkpoint_id": receipt.event.payload.get("checkpoint_id") if receipt.event else None,
                "instruction": "End this turn and wait for the user's structured response.",
            },
            ensure_ascii=False,
        )
