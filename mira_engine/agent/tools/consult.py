"""Consult tool for synchronous role subagent calls (team profile).

Unlike ``spawn`` (fire-and-forget background work), ``consult`` runs a role
subagent inline and returns its answer immediately. This is what lets the
supervisor (the main loop) hold a bounded debate with the critic or hand a
discrete task to the student and read the result in the same turn. Passing the
returned ``session_id`` back continues the same conversation.
"""

from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mira_engine.agent.tools.base import Tool

if TYPE_CHECKING:
    from mira_engine.agent.subagent import SubagentManager

_VALID_ROLES = ("critic", "student", "supervisor")


class ConsultRoleTool(Tool):
    """Synchronously consult a team role and return its response inline."""

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._project_dir: Path | None = None
        self._runtime_project_dir: ContextVar[Path | None] = ContextVar(
            "consult_project_dir",
            default=None,
        )

    def set_project_context(self, project_id: str, project_dir: str) -> None:
        self._project_dir = Path(project_dir)
        self._runtime_project_dir.set(Path(project_dir))

    def clear_project_context(self) -> None:
        self._project_dir = None
        self._runtime_project_dir.set(None)

    @property
    def name(self) -> str:
        return "consult"

    @property
    def description(self) -> str:
        return (
            "Synchronously consult a teammate and get their reply in this turn. "
            "Roles: 'critic' (read-only reviewer; returns a [OKAY]/[REJECT] verdict "
            "on your plan's rigor), 'student' (implementer; executes a concrete, "
            "already-decided task and reports results). Use 'critic' to debate a plan "
            "to consensus before execution, then delegate the agreed work to 'student'. "
            "To continue an existing debate or task thread, pass back the 'session_id' "
            "returned by a previous consult so the teammate keeps full context."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "enum": list(_VALID_ROLES),
                    "description": "Which teammate to consult.",
                },
                "task": {
                    "type": "string",
                    "description": (
                        "The plan to review (for critic) or the concrete task to "
                        "implement (for student). Be specific and self-contained."
                    ),
                },
                "session_id": {
                    "type": "string",
                    "description": (
                        "Optional. Pass the session_id from a previous consult to "
                        "continue the same conversation (multi-turn debate)."
                    ),
                },
            },
            "required": ["role", "task"],
        }

    async def execute(
        self,
        role: str,
        task: str,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        role_normalized = (role or "").strip().lower()
        if role_normalized not in _VALID_ROLES:
            return (
                f"Error: unknown role '{role}'. Valid roles: {', '.join(_VALID_ROLES)}."
            )
        workspace = self._runtime_project_dir.get() or self._project_dir
        result, returned_session = await self._manager.consult(
            role=role_normalized,
            task=task,
            workspace=workspace,
            session_id=session_id,
        )
        return (
            f"[consult:{role_normalized} session_id={returned_session}]\n"
            f"{result}\n\n"
            f"(To continue this thread, call consult again with session_id="
            f"'{returned_session}'.)"
        )
