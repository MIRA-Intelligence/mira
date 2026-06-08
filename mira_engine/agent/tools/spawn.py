"""Spawn tool for creating background subagents."""

from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mira_engine.agent.tools.base import Tool

if TYPE_CHECKING:
    from mira_engine.agent.subagent import SubagentManager


class SpawnTool(Tool):
    """Tool to spawn a subagent for background task execution."""

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._origin_channel = "cli"
        self._origin_chat_id = "direct"
        self._session_key = "cli:direct"
        self._project_id: str | None = None
        self._project_dir: Path | None = None
        self._runtime_origin_channel: ContextVar[str | None] = ContextVar(
            "spawn_origin_channel",
            default=None,
        )
        self._runtime_origin_chat_id: ContextVar[str | None] = ContextVar(
            "spawn_origin_chat_id",
            default=None,
        )
        self._runtime_session_key: ContextVar[str | None] = ContextVar(
            "spawn_session_key",
            default=None,
        )
        self._runtime_project_id: ContextVar[str | None] = ContextVar(
            "spawn_project_id",
            default=None,
        )
        self._runtime_project_dir: ContextVar[Path | None] = ContextVar(
            "spawn_project_dir",
            default=None,
        )

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the origin context for subagent announcements."""
        self._origin_channel = channel
        self._origin_chat_id = chat_id
        self._session_key = f"{channel}:{chat_id}"
        self._runtime_origin_channel.set(channel)
        self._runtime_origin_chat_id.set(chat_id)
        self._runtime_session_key.set(f"{channel}:{chat_id}")

    def set_session_key(self, session_key: str) -> None:
        """Set the scoped session key used for cancellation and routing."""

        self._session_key = session_key
        self._runtime_session_key.set(session_key)

    def set_project_context(self, project_id: str, project_dir: str) -> None:
        """Set the current project for spawned subagents."""

        self._project_id = project_id
        self._project_dir = Path(project_dir)
        self._runtime_project_id.set(project_id)
        self._runtime_project_dir.set(Path(project_dir))

    def clear_project_context(self) -> None:
        """Clear the current project for spawned subagents."""

        self._project_id = None
        self._project_dir = None
        self._runtime_project_id.set(None)
        self._runtime_project_dir.set(None)

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        return (
            "Spawn a subagent to handle a task in the background. "
            "Use this for complex or time-consuming tasks that can run independently. "
            "The subagent will complete the task and report back when done."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task for the subagent to complete",
                },
                "label": {
                    "type": "string",
                    "description": "Optional short label for the task (for display)",
                },
            },
            "required": ["task"],
        }

    async def execute(self, task: str, label: str | None = None, **kwargs: Any) -> str:
        """Spawn a subagent to execute the given task."""
        return await self._manager.spawn(
            task=task,
            label=label,
            origin_channel=self._runtime_origin_channel.get() or self._origin_channel,
            origin_chat_id=self._runtime_origin_chat_id.get() or self._origin_chat_id,
            session_key=self._runtime_session_key.get() or self._session_key,
            workspace=self._runtime_project_dir.get() or self._project_dir,
            project_id=self._runtime_project_id.get() or self._project_id,
        )
