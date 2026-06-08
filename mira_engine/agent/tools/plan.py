"""set_plan tool: drive interactive plan mode via task_plan.json.

In a research project the agent uses this tool to (1) ask the user structured
clarifying questions before designing experiments, (2) propose a draft
experiment plan for the user to approve, and (3) mark the plan approved once the
user agrees. The data is written into ``task_plan.json`` under the ``plan`` key,
which the UI reads (via ``GET /api/plan``) to render the interactive planning
stage. After emitting questions or a draft the agent must stop and wait for the
user's response (delivered on a later turn).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mira_engine.agent.tools.base import Tool, tool_parameters

PLAN_FILENAME = "task_plan.json"
_VALID_PHASES = {"questions", "draft", "approved"}
_VALID_KINDS = {"single", "multi", "text"}


@tool_parameters({
    "type": "object",
    "properties": {
        "phase": {
            "type": "string",
            "enum": ["questions", "draft", "approved"],
            "description": (
                "'questions' to ask clarifying questions before designing "
                "experiments; 'draft' to propose an experiment plan for the user "
                "to approve; 'approved' once the user has approved the plan."
            ),
        },
        "questions": {
            "type": "array",
            "description": "Structured clarifying questions (required when phase='questions').",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Stable question id, e.g. 'q1'."},
                    "prompt": {"type": "string", "description": "The question text shown to the user."},
                    "kind": {
                        "type": "string",
                        "enum": ["single", "multi", "text"],
                        "description": "'single' = pick one option, 'multi' = pick many, 'text' = free text.",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Choices for 'single'/'multi' questions.",
                    },
                    "rationale": {"type": "string", "description": "Why this matters (optional)."},
                },
                "required": ["id", "prompt", "kind"],
            },
        },
        "draft": {
            "type": "object",
            "description": "Proposed experiment plan (required when phase='draft').",
            "properties": {
                "summary": {"type": "string", "description": "Short overview of the proposed plan."},
                "experiments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "hypothesis": {"type": "string"},
                            "method": {"type": "string"},
                        },
                        "required": ["title"],
                    },
                },
            },
        },
    },
    "required": ["phase"],
})
class SetPlanTool(Tool):
    """Write the interactive plan block into the active project's task_plan.json."""

    def __init__(self, project_dir: str | None = None) -> None:
        self._project_dir = project_dir

    def set_project_dir(self, project_dir: str | None) -> None:
        """Bind the tool to the current message's project directory."""
        self._project_dir = project_dir

    @property
    def name(self) -> str:
        return "set_plan"

    @property
    def description(self) -> str:
        return (
            "Manage interactive plan mode for a research project. Call with "
            "phase='questions' to ask the user structured clarifying questions "
            "before designing experiments; phase='draft' to propose an experiment "
            "plan for the user to approve; phase='approved' after the user approves. "
            "Writes into task_plan.json under the 'plan' key. After calling with "
            "'questions' or 'draft', STOP and wait for the user's response."
        )

    async def execute(
        self,
        phase: str,
        questions: list[dict[str, Any]] | None = None,
        draft: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        if phase not in _VALID_PHASES:
            return f"Error: invalid phase '{phase}' (expected questions/draft/approved)"
        if not self._project_dir:
            return "Error: set_plan is only available inside a project"

        plan_path = Path(self._project_dir) / PLAN_FILENAME
        try:
            if plan_path.is_file():
                data = json.loads(plan_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            else:
                data = {}
        except (OSError, json.JSONDecodeError) as exc:
            return f"Error reading task_plan.json: {exc}"

        plan_block = data.get("plan")
        if not isinstance(plan_block, dict):
            plan_block = {}

        plan_block["phase"] = phase
        plan_block["updated_at"] = datetime.now(UTC).isoformat()
        if phase == "questions":
            normalized = self._normalize_questions(questions)
            if not normalized:
                return "Error: phase='questions' requires a non-empty 'questions' list"
            plan_block["questions"] = normalized
            # Re-asking starts a new planning round; prior answers/drafts belong
            # to the previous round and would otherwise make the UI look approved
            # or pre-answered.
            plan_block.pop("answers", None)
            plan_block.pop("draft", None)
            plan_block.pop("feedback", None)
        elif phase == "draft":
            if not isinstance(draft, dict) or not str(draft.get("summary", "")).strip():
                return "Error: phase='draft' requires a 'draft' object with a non-empty summary"
            plan_block["draft"] = self._normalize_draft(draft)
            plan_block.pop("feedback", None)

        data["plan"] = plan_block
        if not isinstance(data.get("schema_version"), int):
            data["schema_version"] = 1

        try:
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            return f"Error writing task_plan.json: {exc}"

        if phase == "questions":
            return (
                f"Saved {len(plan_block['questions'])} plan question(s). "
                "Now STOP this turn and wait for the user to answer in the UI."
            )
        if phase == "draft":
            count = len(plan_block["draft"].get("experiments", []))
            return (
                f"Saved plan draft with {count} experiment(s). "
                "Now STOP this turn and wait for the user to approve or request changes."
            )
        return "Plan marked approved. You may now create and run the experiments."

    @staticmethod
    def _normalize_questions(questions: Any) -> list[dict[str, Any]]:
        if not isinstance(questions, list):
            return []
        out: list[dict[str, Any]] = []
        for idx, question in enumerate(questions, start=1):
            if not isinstance(question, dict):
                continue
            prompt = question.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                continue
            raw_id = question.get("id")
            qid = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else f"q{idx}"
            kind = question.get("kind")
            if kind not in _VALID_KINDS:
                kind = "text"
            item: dict[str, Any] = {"id": qid, "prompt": prompt.strip(), "kind": kind}
            if kind in {"single", "multi"}:
                options = question.get("options")
                if isinstance(options, list):
                    item["options"] = [
                        str(opt).strip()
                        for opt in options
                        if isinstance(opt, (str, int, float)) and str(opt).strip()
                    ]
                if not item.get("options"):
                    # No usable options -> downgrade to free text.
                    item["kind"] = "text"
            rationale = question.get("rationale")
            if isinstance(rationale, str) and rationale.strip():
                item["rationale"] = rationale.strip()
            out.append(item)
        return out

    @staticmethod
    def _normalize_draft(draft: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {"summary": str(draft.get("summary", "")).strip()}
        experiments: list[dict[str, Any]] = []
        raw = draft.get("experiments")
        if isinstance(raw, list):
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                title = entry.get("title")
                if not isinstance(title, str) or not title.strip():
                    continue
                item: dict[str, Any] = {"title": title.strip()}
                for field in ("hypothesis", "method"):
                    value = entry.get(field)
                    if isinstance(value, str) and value.strip():
                        item[field] = value.strip()
                experiments.append(item)
        normalized["experiments"] = experiments
        return normalized
